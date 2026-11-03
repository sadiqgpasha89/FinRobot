"""FastAPI service. Jobs run on ONE worker thread: the GPU has a single model slot, so parallel runs cannot go faster."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from finrobot import runs
from finrobot.data import store

app = FastAPI(title="FinRobot", version="0.1.0")
_pool = ThreadPoolExecutor(max_workers=1)


class AskIn(BaseModel):
    question: str = Field(min_length=3)
    ticker: str | None = None


class ReportIn(BaseModel):
    ticker: str
    assumptions: dict[str, Any] | None = None   # decimals, e.g. {"wacc": 0.09, "rev_growth": [0.05, 0.05, 0.04, 0.04, 0.03]}
    peers: list[str] = []
    price: float | None = None


class IngestIn(BaseModel):
    ticker: str
    facts_only: bool = False


class PriceIn(BaseModel):
    ticker: str
    price: float = Field(gt=0)
    day: date | None = None


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/companies")
def companies() -> list[dict]:
    out = []
    for c in store.list_companies():
        lq = store.latest_quarter(c["ticker"])
        price = store.latest_price(c["ticker"])
        out.append({"ticker": c["ticker"], "name": c["name"], "latest_quarter": f"FY{lq[0]} Q{lq[1]}" if lq else None,
                    "price": price["close"] if price else None, "filings": len(store.list_filings(c["ticker"]))})
    return out


def _submit(mode: str, request: dict, fn) -> dict:
    run_id, emit = runs.new_run(mode, request)

    def job() -> None:
        try:
            result = fn(run_id, emit)
            runs.finish(run_id, "done", result)
        except Exception as e:  # surfaced to the client through the run record
            emit({"type": "error", "msg": str(e)})
            runs.finish(run_id, "failed", {"error": str(e)})

    _pool.submit(job)
    return {"run_id": run_id}


@app.post("/ask")
def ask(body: AskIn) -> dict:
    def fn(run_id: str, emit) -> dict:
        from finrobot.graph.qa_graph import run_qa
        result, ctx = run_qa(body.question, body.ticker, emit)
        result["artifacts"] = runs.save_artifacts(run_id, ctx.artifacts)
        (runs.run_dir(run_id) / "answer.md").write_text(result["answer_md"])
        return result
    return _submit("qa", body.model_dump(), fn)


@app.get("/reports/draft-assumptions")
def draft(ticker: str) -> dict:
    from finrobot.graph.report_graph import draft_assumptions
    try:
        return draft_assumptions(ticker)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.post("/reports")
def create_report(body: ReportIn) -> dict:
    def fn(run_id: str, emit) -> dict:
        from finrobot.graph.report_graph import run_report
        out = run_report(body.ticker, emit=emit, assumptions=body.assumptions, peers=[p.upper() for p in body.peers], price=body.price, run_id=run_id)
        return out
    return _submit("report", body.model_dump(), fn)


@app.post("/ingest")
def ingest(body: IngestIn) -> dict:
    def fn(run_id: str, emit) -> dict:
        from finrobot.data.ingest import ingest as run
        return run(body.ticker, 0 if body.facts_only else 1, 0 if body.facts_only else 3, 0 if body.facts_only else 3,
                   log=lambda m: emit({"type": "log", "msg": m}))
    return _submit("ingest", body.model_dump(), fn)


@app.post("/prices")
def set_price(body: PriceIn) -> dict:
    store.upsert_prices(body.ticker.upper(), [(body.day or date.today(), body.price)], "manual")
    return {"ok": True}


@app.get("/runs/{run_id}")
def get_run(run_id: str, full: bool = False) -> dict:
    r = store.get_run(run_id)
    if r is None:
        raise HTTPException(404, "unknown run")
    result = r["result"]
    if isinstance(result, dict) and not full:
        result = {k: v for k, v in result.items() if k != "factbook"}
    return {"id": r["id"], "mode": r["mode"], "status": r["status"], "events": r["events"], "result": result}


@app.get("/runs/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str):
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name) or not re.fullmatch(r"[A-Za-z0-9\-]+", run_id):
        raise HTTPException(400, "bad name")
    path = runs.run_dir(run_id) / name
    if not path.is_file():
        raise HTTPException(404, "no such artifact")
    return FileResponse(path, media_type="application/pdf" if name.endswith(".pdf") else None, filename=name if name.endswith(".pdf") else None)
