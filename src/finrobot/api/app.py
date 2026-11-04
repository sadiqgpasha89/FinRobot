"""FastAPI service.

Long jobs (questions, reports, ingests) run on a small worker pool: the default is ONE worker because a single local GPU has one model
slot, so parallel runs cannot go faster. `POST` endpoints return a run id immediately; clients poll `GET /runs/{id}`.

Production behaviour: API-key auth, request ids, JSON logs, Prometheus metrics at `/metrics`, a bounded queue (429 when full), run
reconciliation after a restart, and liveness (`/health`) vs readiness (`/ready`) probes.
"""
from __future__ import annotations

import hmac
import mimetypes
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Callable

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field, StringConstraints

from finrobot import __version__, runs, service
from finrobot.config import settings
from finrobot.data import objects, store
from finrobot.graph.report_graph import LISTS, SCALARS
from finrobot.llm.gateway import LLMGateway
from finrobot.log import get_logger, request_id_var, setup_logging
from finrobot.observability import init_observability, shutdown_observability

log = get_logger("api")

RUNS = Counter("finrobot_runs_total", "Runs finished", ["mode", "status"])
RUN_SECONDS = Histogram("finrobot_run_seconds", "Run wall time", ["mode"], buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 3600))
QUEUE_DEPTH = Gauge("finrobot_queue_depth", "Jobs queued or running")
HTTP_REQUESTS = Counter("finrobot_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_SECONDS = Histogram("finrobot_http_request_seconds", "HTTP latency", ["method", "path"])

Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.\-]{1,12}$")]
CompanyQuery = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9 .,&'\-]{1,80}$")]   # ingest accepts a name as well as a ticker


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    ticker: Ticker | None = None


class ReportIn(BaseModel):
    ticker: Ticker
    assumptions: dict[str, Any] | None = None   # decimals, e.g. {"wacc": 0.09, "rev_growth": [0.05, 0.05, 0.04, 0.04, 0.03]}
    peers: list[Ticker] = Field(default_factory=list)
    price: float | None = Field(None, gt=0)
    review: bool = False                         # pause after drafting the assumptions and wait for POST /runs/{id}/resume


class ResumeIn(BaseModel):
    assumptions: dict[str, Any] | None = None


class IngestIn(BaseModel):
    ticker: CompanyQuery
    facts_only: bool = False


class PriceIn(BaseModel):
    ticker: Ticker
    price: float = Field(gt=0)
    day: date | None = None


# --- auth ------------------------------------------------------------------------------------------
def require_key(request: Request) -> None:
    keys = settings.api_key_list
    if not keys:   # development only: production refuses to start without keys
        return
    supplied = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not supplied or not any(hmac.compare_digest(supplied.encode(), k.encode()) for k in keys):
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})


# --- jobs ------------------------------------------------------------------------------------------
class JobRunner:
    """A bounded worker pool. `submit` returns False (-> HTTP 429) when the queue is full."""

    def __init__(self, workers: int, max_queue: int) -> None:
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="finrobot-job")
        self.max_pending = workers + max_queue
        self._pending = 0
        self._lock = threading.Lock()

    def submit(self, mode: str, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._pending >= self.max_pending:
                return False
            self._pending += 1
            QUEUE_DEPTH.set(self._pending)

        def wrapped() -> None:
            t0 = time.time()
            try:
                fn()
            finally:
                RUN_SECONDS.labels(mode).observe(time.time() - t0)
                with self._lock:
                    self._pending -= 1
                    QUEUE_DEPTH.set(self._pending)

        self.pool.submit(wrapped)
        return True

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def runner_of(request: Request) -> JobRunner:
    app = request.app
    if getattr(app.state, "runner", None) is None:
        app.state.runner = JobRunner(settings.max_workers, settings.max_queue)
    return app.state.runner


def llm_of(request: Request) -> LLMGateway:
    if getattr(request.app.state, "llm", None) is None:
        request.app.state.llm = LLMGateway()   # one gateway: one concurrency lane for the whole process
    return request.app.state.llm


def checkpointer_of(request: Request):
    if getattr(request.app.state, "checkpointer", None) is None:
        from finrobot.graph.checkpoint import get_checkpointer
        request.app.state.checkpointer = get_checkpointer()
    return request.app.state.checkpointer


def submit(request: Request, mode: str, request_body: dict, job: Callable[[str, Callable[[dict], None]], dict | None],
           run_id: str | None = None) -> dict:
    """Create the run record, queue the job, return the id. The record is written first so the client can poll at once."""
    if run_id is None:
        run_id, emit = runs.new_run(mode, request_body)
    else:
        emit = runs.make_emitter(run_id)
        store.set_run_status(run_id, "running")

    def work() -> None:
        result = service.run_job(run_id, emit, lambda: job(run_id, emit))
        RUNS.labels(mode, "failed" if result is None else result.get("status", "done")).inc()

    if not runner_of(request).submit(mode, work):
        runs.finish(run_id, "failed", {"error": "queue full"}, error="queue full")
        raise HTTPException(429, "too many queued jobs; retry later", headers={"Retry-After": "30"})
    return {"run_id": run_id}


def validate_overrides(over: dict | None) -> dict:
    over = over or {}
    unknown = set(over) - set(LISTS) - set(SCALARS)
    if unknown:
        raise HTTPException(422, f"unknown assumption(s): {sorted(unknown)}. Allowed: {list(LISTS) + list(SCALARS)}")
    return over


def require_company(ticker: str) -> dict:
    company = store.find_company(ticker)
    if company is None:
        raise HTTPException(404, f"'{ticker}' is not ingested. POST /ingest first")
    return company


# --- app -------------------------------------------------------------------------------------------
def _wait_for_db(timeout: float = 90.0) -> None:
    """Containers start in any order: wait for Postgres instead of crashing on the first refused connection."""
    deadline = time.time() + timeout
    while True:
        try:
            store.init_db()
            return
        except (psycopg.OperationalError, OSError) as e:
            if time.time() > deadline:
                raise
            log.warning("database not ready (%s); retrying", str(e).splitlines()[0] if str(e) else e)
            time.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level, settings.log_json)
    problems = settings.production_problems() if settings.environment == "production" else []
    if problems:
        raise RuntimeError("refusing to start in production: " + "; ".join(problems))
    if settings.environment != "production" and not settings.api_key_list:
        log.warning("FINROBOT_API_KEYS is not set: the API is unauthenticated. Bind it to localhost only.")
    init_observability()
    _wait_for_db()
    from finrobot.graph.checkpoint import get_checkpointer
    app.state.checkpointer = get_checkpointer()
    orphans = store.mark_orphaned_runs()
    if orphans:
        log.warning("marked %d run(s) interrupted by a restart: %s", len(orphans), ", ".join(orphans))
    app.state.runner = JobRunner(settings.max_workers, settings.max_queue)
    log.info("finrobot %s ready (env=%s, models: %s / %s)", __version__, settings.environment, settings.model_figures, settings.model_narrative)
    yield
    app.state.runner.shutdown()
    shutdown_observability()
    store.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="FinRobot", version=__version__, lifespan=lifespan)
    if settings.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST"],
                           allow_headers=["x-api-key", "authorization", "content-type"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        t0 = time.time()
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        finally:
            request_id_var.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")   # templated path, so run ids do not explode label cardinality
        HTTP_REQUESTS.labels(request.method, path, response.status_code).inc()
        HTTP_SECONDS.labels(request.method, path).observe(time.time() - t0)
        response.headers["x-request-id"] = rid
        return response

    open_routes = APIRouter()
    api = APIRouter(dependencies=[Depends(require_key)])

    @open_routes.get("/health")
    def health() -> dict:
        """Liveness: the process is up. Does not touch dependencies."""
        return {"ok": True, "version": __version__}

    @open_routes.get("/ready")
    def ready(request: Request, response: Response) -> dict:
        """Readiness: Postgres reachable, models present (or hosted). The object store is optional (local fallback)."""
        checks: dict[str, Any] = {"postgres": store.ping(), "object_store": "s3" if objects.available() else "local-fallback"}
        models = llm_of(request).health()
        checks["models"] = models
        ok = bool(checks["postgres"]) and bool(models.get("ok"))
        if not ok:
            response.status_code = 503
        return {"ok": ok, **checks}

    @api.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @api.get("/companies")
    def companies() -> list[dict]:
        out = []
        for c in store.list_companies():
            lq = store.latest_quarter(c["ticker"])
            price = store.latest_price(c["ticker"])
            out.append({"ticker": c["ticker"], "name": c["name"], "latest_quarter": f"FY{lq[0]} Q{lq[1]}" if lq else None,
                        "price": price["close"] if price else None, "filings": len(store.list_filings(c["ticker"]))})
        return out

    @api.post("/ask", status_code=202)
    def ask(body: AskIn, request: Request) -> dict:
        llm = llm_of(request)
        return submit(request, "qa", body.model_dump(),
                      lambda run_id, emit: service.execute_qa(run_id, emit, body.question, body.ticker, llm))

    @api.get("/reports/draft-assumptions")
    def draft(ticker: Ticker) -> dict:
        from finrobot.graph.report_graph import draft_assumptions
        try:
            return draft_assumptions(ticker)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    @api.post("/reports", status_code=202)
    def create_report(body: ReportIn, request: Request) -> dict:
        over = validate_overrides(body.assumptions)   # cheap checks first: a bad request must not cost a database round trip
        if len(body.peers) > settings.max_peers:
            raise HTTPException(422, f"at most {settings.max_peers} peers")
        require_company(body.ticker)
        llm, cp = llm_of(request), checkpointer_of(request)
        return submit(request, "report", body.model_dump(),
                      lambda run_id, emit: service.execute_report(run_id, emit, body.ticker, assumptions=over, peers=[p.upper() for p in body.peers],
                                                                  price=body.price, review=body.review, checkpointer=cp, llm=llm))

    @api.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, body: ResumeIn, request: Request) -> dict:
        """Continue a report that is `awaiting_review`, applying the reviewer's assumption edits (decimals)."""
        run = _get_run_or_404(run_id)
        if run["status"] != "awaiting_review":
            raise HTTPException(409, f"run is '{run['status']}', not awaiting_review")
        edits = validate_overrides(body.assumptions)
        llm, cp = llm_of(request), checkpointer_of(request)
        return submit(request, "report", {}, lambda rid, emit: service.execute_resume(rid, emit, edits, cp, llm=llm), run_id=run_id)

    @api.post("/runs/{run_id}/retry", status_code=202)
    def retry(run_id: str, request: Request) -> dict:
        """Continue an `interrupted` or `failed` report from its last checkpoint instead of starting over."""
        run = _get_run_or_404(run_id)
        if run["mode"] != "report" or run["status"] not in ("interrupted", "failed"):
            raise HTTPException(409, "only interrupted or failed report runs can be retried")
        llm, cp = llm_of(request), checkpointer_of(request)
        return submit(request, "report", {}, lambda rid, emit: service.execute_resume(rid, emit, None, cp, retry=True, llm=llm), run_id=run_id)

    @api.post("/ingest", status_code=202)
    def ingest(body: IngestIn, request: Request) -> dict:
        llm = llm_of(request)
        return submit(request, "ingest", body.model_dump(),
                      lambda run_id, emit: service.execute_ingest(run_id, emit, body.ticker, body.facts_only, llm))

    @api.post("/prices")
    def set_price(body: PriceIn) -> dict:
        require_company(body.ticker)
        store.upsert_prices(body.ticker.upper(), [(body.day or date.today(), body.price)], "manual")
        return {"ok": True}

    def _get_run_or_404(run_id: str, after: int = 0) -> dict:
        if not runs.RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(404, "unknown run")
        r = store.get_run(run_id, after)
        if r is None:
            raise HTTPException(404, "unknown run")
        return r

    @api.get("/runs")
    def list_runs(limit: int = Query(30, ge=1, le=200)) -> list[dict]:
        return [{"id": r["id"], "mode": r["mode"], "status": r["status"], "error": r["error"], "created_at": r["created_at"]}
                for r in store.list_runs(limit)]

    @api.get("/runs/{run_id}")
    def get_run(run_id: str, full: bool = False, after: int = 0) -> dict:
        r = _get_run_or_404(run_id, after)
        result = r["result"]
        if isinstance(result, dict) and not full:
            result = {k: v for k, v in result.items() if k != "factbook"}
        return {"id": r["id"], "mode": r["mode"], "status": r["status"], "events": r["events"], "result": result,
                "usage": r["usage"], "trace_id": r["trace_id"], "error": r["error"], "created_at": r["created_at"], "updated_at": r["updated_at"]}

    @api.get("/runs/{run_id}/artifacts/{name}")
    def artifact(run_id: str, name: str):
        path = runs.artifact_path(run_id, name)
        if path is None:
            raise HTTPException(404, "no such artifact")
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = {"X-Content-Type-Options": "nosniff"}
        if media == "text/html":   # generated from model text: never let it run script or load anything from the network
            headers["Content-Security-Policy"] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
        return FileResponse(path, media_type=media, filename=name if name.endswith(".pdf") else None, headers=headers)

    app.include_router(open_routes)
    app.include_router(api)
    return app


app = create_app()
