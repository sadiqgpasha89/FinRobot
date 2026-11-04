"""Run execution shared by the CLI and the API: tracing, usage, scores, persistence and status in one place.

`execute_*` functions run one job to completion for an existing run id and return the result dict;
`run_job` wraps them with status handling so every run ends in exactly one terminal (or awaiting-review) state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from finrobot import runs
from finrobot.data import store
from finrobot.llm.gateway import LLMGateway
from finrobot.log import get_logger, run_id_var
from finrobot.observability import qa_scores, report_scores, trace_run

log = get_logger("service")
Emit = Callable[[dict], None]


def execute_qa(run_id: str, emit: Emit, question: str, ticker: str | None = None, llm: LLMGateway | None = None, flush: bool = False) -> dict:
    from finrobot.graph.qa_graph import run_qa
    with trace_run(run_id, "qa", {"question": question, "ticker": ticker}, metadata={"ticker": ticker or ""}, flush=flush) as tr:
        result, ctx = run_qa(question, ticker, emit, llm, config=tr.config)
        result["artifacts"] = runs.save_artifacts(run_id, ctx.artifacts)
        (runs.run_dir(run_id) / "answer.md").write_text(result["answer_md"])
        tr.set_output(result["answer_md"])
        tr.scores(qa_scores(result))
        result["usage"], result["trace_id"] = tr.usage.snapshot(), tr.trace_id
    return result


def execute_report(run_id: str, emit: Emit, ticker: str, *, assumptions: dict | None = None, assumptions_file: Path | None = None,
                   peers: list[str] | None = None, price: float | None = None, review: bool = False, checkpointer: Any = None,
                   llm: LLMGateway | None = None, flush: bool = False) -> dict:
    from finrobot.graph.report_graph import run_report
    with trace_run(run_id, "report", {"ticker": ticker, "peers": peers, "review": review}, metadata={"ticker": ticker}, flush=flush) as tr:
        out = run_report(ticker, emit=emit, assumptions_file=assumptions_file, assumptions=assumptions, peers=peers, price=price,
                         run_id=run_id, llm=llm, review=review, checkpointer=checkpointer, config=tr.config)
        return _finish_report(tr, out)


def execute_resume(run_id: str, emit: Emit, edits: dict | None, checkpointer: Any, *, retry: bool = False, llm: LLMGateway | None = None,
                   flush: bool = False) -> dict:
    from finrobot.graph.report_graph import resume_report
    with trace_run(run_id, "report", {"resume": True, "retry": retry}, flush=flush) as tr:
        out = resume_report(run_id, edits, checkpointer, emit=emit, llm=llm, config=tr.config, retry=retry)
        return _finish_report(tr, out)


def _finish_report(tr, out: dict) -> dict:
    if out.get("status") == "awaiting_review":
        return out
    tr.set_output({k: out.get(k) for k in ("pdf_name", "pages", "verification")})
    tr.scores(report_scores(out))
    out["usage"], out["trace_id"] = tr.usage.snapshot(), tr.trace_id
    return out


def execute_ingest(run_id: str, emit: Emit, ticker: str, facts_only: bool = False, llm: LLMGateway | None = None) -> dict:
    from finrobot.data.ingest import ingest
    return ingest(ticker, 0 if facts_only else 1, 0 if facts_only else 3, 0 if facts_only else 3,
                  log=lambda m: emit({"type": "log", "msg": m}), llm=llm)


def run_job(run_id: str, emit: Emit, fn: Callable[[], dict]) -> dict | None:
    """Run `fn`, then record the outcome: done / awaiting_review / failed. Returns the result, or None when it failed."""
    run_id_var.set(run_id)
    try:
        result = fn()
    except Exception as e:   # surfaced to the client through the run record
        log.exception("run %s failed", run_id)
        emit({"type": "error", "msg": str(e)})
        runs.finish(run_id, "failed", {"error": str(e)}, error=f"{type(e).__name__}: {e}")
        return None
    if result.get("status") == "awaiting_review":
        store.set_run_status(run_id, "awaiting_review")
        store.finish_run(run_id, "awaiting_review", result)
        emit({"type": "log", "msg": "assumptions ready for review"})
        return result
    usage = result.get("usage")
    runs.finish(run_id, "done", {k: v for k, v in result.items() if k != "pdf_bytes"}, usage)
    return result
