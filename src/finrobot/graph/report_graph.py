"""Report graph:  draft ─▶ model ─▶ write (one section per step, looping) ─▶ ground ─▶ render.

Durable by design. The state holds only small serialisable values (assumptions, prose, a FactBook snapshot); the heavy objects (the
RunContext, tables, charts) live in the run-scoped ``Runtime`` context and are rebuilt deterministically from the database when a run
resumes in a fresh process. With a checkpointer the graph therefore

* pauses at ``draft`` for human review of the valuation assumptions (``interrupt``) and continues with the reviewer's edits, and
* survives a crash or restart: the next ``resume`` continues from the last completed section instead of paying for the LLM calls again.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypedDict

import yaml
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from finrobot import runs
from finrobot.agents.context import RunContext, resolve_period
from finrobot.agents.evaluator import evaluate
from finrobot.agents.sections.writers import ground_all, strip_empty, write_risks, write_section
from finrobot.data import store
from finrobot.llm.gateway import LLMGateway
from finrobot.report.model import SECTIONS, ReportModel, build_model
from finrobot.report.render import format_check, render_html, to_pdf
from finrobot.tools.valuation import YEARS, Assumptions, default_assumptions

LISTS = ("rev_growth", "ebitda_margin")
SCALARS = ("da_pct", "capex_pct", "tax_rate", "nwc_pct_of_delta_rev", "wacc", "terminal_growth")


def apply_overrides(a: Assumptions, over: dict) -> Assumptions:
    """User overrides, in decimals (wacc: 0.09). A single number for a per-year list applies to every year."""
    unknown = set(over) - set(LISTS) - set(SCALARS)
    if unknown:
        raise ValueError(f"unknown assumption(s): {sorted(unknown)}. Allowed: {list(LISTS) + list(SCALARS)}")
    for k, v in over.items():
        if k in LISTS:
            v = [float(v)] * YEARS if isinstance(v, (int, float)) else [float(x) for x in v]
            if len(v) != YEARS:
                raise ValueError(f"{k} needs {YEARS} values (one per projected year) or a single number")
        else:
            v = float(v)
        setattr(a, k, v)
        a.basis[k] = "user override"
    if a.wacc <= a.terminal_growth:
        raise ValueError("wacc must be greater than terminal_growth")
    return a


def load_assumption_overrides(path: Path | None) -> dict:
    return (yaml.safe_load(Path(path).read_text()) or {}) if path else {}


class ReportState(TypedDict, total=False):
    ticker: str
    peers: list[str]
    price: float | None
    overrides: dict
    review: bool
    assumptions: dict
    warnings: list[str]
    section_idx: int
    prose: dict
    stats: dict
    extra_tables: dict
    risk_text: str
    factbook: dict
    result: dict


@dataclass
class ReportRuntime:
    llm: LLMGateway
    emit: Callable[[dict], None]
    run_id: str | None = None
    ctx: RunContext | None = None
    model: ReportModel | None = None
    started: float = 0.0


def get_ctx(state: ReportState, rt: ReportRuntime) -> RunContext:
    """The RunContext, built on first use (and after a resume in a new process) and refilled from the checkpointed FactBook."""
    if rt.ctx is None:
        company = store.find_company(state["ticker"])
        if company is None:
            raise LookupError(f"'{state['ticker']}' is not ingested. Run: finrobot ingest {state['ticker']}")
        fy, fq = resolve_period(company["ticker"], None, None)
        rt.ctx = RunContext(company["ticker"], company["name"], rt.llm, fy=fy, fq=fq, emit=rt.emit)
        rt.ctx.fb.load(state.get("factbook") or {})
    return rt.ctx


def get_model(state: ReportState, rt: ReportRuntime) -> ReportModel:
    """The ReportModel (tables, charts, DCF). Rebuilt from the saved assumptions after a resume; fact ids are stable because
    the FactBook deduplicates on (label, value, unit, source)."""
    ctx = get_ctx(state, rt)
    if rt.model is None:
        rt.model = build_model(ctx, Assumptions.from_dict(state["assumptions"]), state.get("peers"), state.get("price"))
    return rt.model


def n_draft(state: ReportState, runtime: Runtime[ReportRuntime]) -> ReportState:
    rt = runtime.context
    ctx = get_ctx(state, rt)
    rt.started = rt.started or time.time()
    a = default_assumptions(ctx.metrics, ctx.fy, ctx.fq)
    if state.get("overrides"):
        a = apply_overrides(a, state["overrides"])
    if state.get("review"):
        rt.emit({"type": "log", "msg": "waiting for assumption review"})
        draft = a.to_dict()
        payload = {"type": "review_assumptions", "ticker": ctx.ticker, "period": ctx.period, "assumptions": draft}
        # Execution stops here and the draft is handed to the caller; the value passed to Command(resume=...) comes back as `edits`.
        edits = interrupt(payload)
        while True:
            try:
                a = apply_overrides(Assumptions.from_dict(draft), edits or {})
                break
            except ValueError as e:
                # Validate here and ask again. Raising instead would be unrecoverable: LangGraph keeps the failed resume value
                # for this task, so a corrected resume would be ignored and the run could never continue.
                rt.emit({"type": "log", "msg": f"invalid assumptions, still waiting for review: {e}"})
                edits = interrupt({**payload, "error": str(e)})
    return {"assumptions": a.to_dict(), "factbook": ctx.fb.to_dict()}


def n_model(state: ReportState, runtime: Runtime[ReportRuntime]) -> ReportState:
    rt = runtime.context
    ctx = get_ctx(state, rt)
    rt.emit({"type": "log", "msg": f"{ctx.ticker} {ctx.period}: building tables, charts and valuation"})
    rt.model = None
    model = get_model(state, rt)
    for w in model.warnings:
        rt.emit({"type": "log", "msg": f"warning: {w}"})
    return {"warnings": model.warnings, "section_idx": 0, "prose": {}, "stats": {}, "extra_tables": {}, "risk_text": "",
            "factbook": ctx.fb.to_dict()}


def n_write(state: ReportState, runtime: Runtime[ReportRuntime]) -> ReportState:
    """Write ONE section, then loop: each completed section is a checkpoint."""
    rt = runtime.context
    ctx, model = get_ctx(state, rt), get_model(state, rt)
    idx = state["section_idx"]
    title = SECTIONS[idx]
    rt.emit({"type": "section", "name": title})
    prose, stats, extra = dict(state["prose"]), dict(state["stats"]), dict(state["extra_tables"])
    risk_text = state.get("risk_text", "")
    if title == "Risks and Mitigations":
        table, out = write_risks(ctx)
        extra[title] = [table] if table else []
        prose[title] = ""
        risk_text = out["text"]
        stats[title] = {"rows": out["rows"], "dropped": out["dropped"]}
    elif title in ("Valuation", "Financial Projections") and model.dcf_result is None:
        prose[title] = ""
    else:
        out = write_section(ctx, title, model.sections[title])
        prose[title] = out["text"] if "No verified answer" not in out["text"] else ""
        stats[title] = {"removed": out["removed"], "retried": out["retried"], "passages": out["passages"]}
    return {"section_idx": idx + 1, "prose": prose, "stats": stats, "extra_tables": extra, "risk_text": risk_text,
            "factbook": ctx.fb.to_dict()}


def after_write(state: ReportState) -> str:
    return "write" if state["section_idx"] < len(SECTIONS) else "ground"


def n_ground(state: ReportState, runtime: Runtime[ReportRuntime]) -> ReportState:
    ctx = get_ctx(state, runtime.context)
    # risks are rendered as a table of quoted rows, so grounding runs only on the written sections
    texts = {t: v for t, v in state["prose"].items() if v and t != "Risks and Mitigations"}
    grounded, removed = ground_all(ctx, texts)
    prose = {**state["prose"], **{t: strip_empty(v) for t, v in grounded.items()}}
    stats = {**state["stats"]}
    for t, r in removed.items():
        stats.setdefault(t, {})["grounding_removed"] = len(r)
    return {"prose": prose, "stats": stats, "factbook": ctx.fb.to_dict()}


def n_render(state: ReportState, runtime: Runtime[ReportRuntime]) -> ReportState:
    rt = runtime.context
    ctx, model = get_ctx(state, rt), get_model(state, rt)
    prose = state["prose"]
    rep = evaluate("\n".join([v for v in prose.values() if v] + [state.get("risk_text", "")]), ctx.fb)
    removed = sum(s.get("removed", 0) + s.get("grounding_removed", 0) + s.get("dropped", 0) for s in state["stats"].values())
    verification = {"status": "PASS" if rep.ok else "FAIL", "claims": rep.claims, "matched": rep.matched_facts,
                    "grounded": rep.grounded_passages, "removed": removed}
    html = render_html(ctx, model, prose, state["extra_tables"], verification, [state.get("risk_text", "")])
    pdf, pages = to_pdf(html)
    fmt = format_check(html, pdf, pages)
    pdf_name = f"report_{ctx.ticker}_{ctx.period.replace(' ', '_')}.pdf"
    arts = {pdf_name: pdf, "report.html": html.encode(), **ctx.artifacts}
    if rt.run_id:
        runs.save_artifacts(rt.run_id, arts)
        (runs.run_dir(rt.run_id) / "report_data.json").write_text(json.dumps(
            {"assumptions": model.assumptions.to_dict(), "dcf": model.dcf_result, "warnings": model.warnings, "stats": state["stats"],
             "factbook": ctx.fb.to_dict()}, indent=2, default=str))
    result: dict[str, Any] = {"ticker": ctx.ticker, "period": ctx.period, "pdf_name": pdf_name, "pages": pages, "eval": rep.summary(),
                              "format": fmt, "warnings": model.warnings, "stats": state["stats"], "verification": verification,
                              "assumptions": model.assumptions.to_dict(), "dcf": model.dcf_result,
                              "seconds": round(time.time() - (rt.started or time.time()), 1)}
    if not rt.run_id:
        result["pdf_bytes"] = pdf
    return {"result": result}


def build_report_graph(checkpointer: BaseCheckpointSaver | None = None):
    g = StateGraph(ReportState, context_schema=ReportRuntime)
    for name, fn in (("draft", n_draft), ("model", n_model), ("write", n_write), ("ground", n_ground), ("render", n_render)):
        g.add_node(name, fn)
    g.set_entry_point("draft")
    g.add_edge("draft", "model")
    g.add_edge("model", "write")
    g.add_conditional_edges("write", after_write, {"write": "write", "ground": "ground"})
    g.add_edge("ground", "render")
    g.add_edge("render", END)
    return g.compile(checkpointer=checkpointer)


def _thread(run_id: str | None, config: dict | None) -> dict:
    cfg = {"recursion_limit": 60, **(config or {})}
    if run_id:
        cfg["configurable"] = {**cfg.get("configurable", {}), "thread_id": run_id}
    return cfg


def _outcome(out: dict) -> dict:
    """The graph's return value as the caller sees it: the final result, or the draft awaiting review."""
    if out.get("__interrupt__"):
        payload = out["__interrupt__"][0].value
        return {"status": "awaiting_review", "review": payload}
    result = out["result"]
    if result.get("pdf_bytes") is None:
        result.pop("pdf_bytes", None)
    return result


def run_report(ticker: str, emit: Callable[[dict], None] | None = None, assumptions_file: Path | None = None, assumptions: dict | None = None,
               peers: list[str] | None = None, price: float | None = None, run_id: str | None = None, llm: LLMGateway | None = None, *,
               review: bool = False, checkpointer: BaseCheckpointSaver | None = None, config: dict | None = None) -> dict:
    """Generate a report. Returns the result dict, or {'status': 'awaiting_review', 'review': {...}} when `review` is set and a
    checkpointer is available (continue with `resume_report`)."""
    if review and checkpointer is None:
        raise ValueError("assumption review needs a checkpointer")
    if store.find_company(ticker) is None:
        raise LookupError(f"'{ticker}' is not ingested. Run: finrobot ingest {ticker}")
    rt = ReportRuntime(llm or LLMGateway(), emit or (lambda e: None), run_id)
    state = {"ticker": ticker, "peers": peers or [], "price": price, "review": review,
             "overrides": {**load_assumption_overrides(assumptions_file), **(assumptions or {})}}
    return _outcome(build_report_graph(checkpointer).invoke(state, _thread(run_id, config), context=rt))


def resume_report(run_id: str, edits: dict | None, checkpointer: BaseCheckpointSaver, emit: Callable[[dict], None] | None = None,
                  llm: LLMGateway | None = None, config: dict | None = None, retry: bool = False) -> dict:
    """Continue a checkpointed report: with the reviewer's edits (`retry=False`) or from the last completed step after a crash (`retry=True`)."""
    rt = ReportRuntime(llm or LLMGateway(), emit or (lambda e: None), run_id)
    graph = build_report_graph(checkpointer)
    cfg = _thread(run_id, config)
    if not graph.get_state(cfg).values:
        raise LookupError(f"no checkpoint for run {run_id}")
    return _outcome(graph.invoke(None if retry else Command(resume=edits or {}), cfg, context=rt))


def draft_assumptions(ticker: str) -> dict:
    """Default projection assumptions for a company, so a user can review and edit them before generating the report."""
    company = store.find_company(ticker)
    if company is None:
        raise LookupError(f"'{ticker}' is not ingested")
    fy, fq = resolve_period(company["ticker"], None, None)
    from finrobot.tools.factbook import FactBook
    from finrobot.tools.metrics import Metrics
    a = default_assumptions(Metrics(company["ticker"], FactBook(), store), fy, fq)
    return {"ticker": company["ticker"], "name": company["name"], "period": f"FY{fy} Q{fq}", "assumptions": a.to_dict()}
