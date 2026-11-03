"""Report graph: prepare -> model (tables, charts, DCF) -> write sections -> grounding check -> render PDF -> format check."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, TypedDict

import yaml
from langgraph.graph import END, StateGraph

from finrobot import runs
from finrobot.agents.context import RunContext, resolve_period
from finrobot.agents.evaluator import evaluate
from finrobot.agents.sections.writers import BRIEFS, ground_all, strip_empty, write_risks, write_section
from finrobot.data import store
from finrobot.llm.client import OllamaLLM
from finrobot.report.model import SECTIONS, ReportModel, build_model
from finrobot.report.render import format_check, render_html, to_pdf
from finrobot.tools.valuation import Assumptions, YEARS, default_assumptions

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
    model: ReportModel
    prose: dict
    risk_text: str
    extra_tables: dict
    stats: dict
    result: dict


def run_report(ticker: str, emit: Callable[[dict], None] | None = None, assumptions_file: Path | None = None, assumptions: dict | None = None,
               peers: list[str] | None = None, price: float | None = None, run_id: str | None = None, llm: OllamaLLM | None = None) -> dict:
    emit = emit or (lambda e: None)
    llm = llm or OllamaLLM()
    company = store.find_company(ticker)
    if company is None:
        raise LookupError(f"'{ticker}' is not ingested. Run: finrobot ingest {ticker}")
    fy, fq = resolve_period(company["ticker"], None, None)
    ctx = RunContext(company["ticker"], company["name"], llm, fy=fy, fq=fq, emit=emit)
    over = {**load_assumption_overrides(assumptions_file), **(assumptions or {})}
    t_start = time.time()

    def n_model(state: ReportState) -> ReportState:
        emit({"type": "log", "msg": f"{ctx.ticker} {ctx.period}: building tables, charts and valuation"})
        a = default_assumptions(ctx.metrics, fy, fq)
        if over:
            a = apply_overrides(a, over)
        model = build_model(ctx, a, peers, price)
        for w in model.warnings:
            emit({"type": "log", "msg": f"warning: {w}"})
        return {"model": model}

    def n_write(state: ReportState) -> ReportState:
        model, prose, stats, extra, risk_text = state["model"], {}, {}, {}, ""
        for title in SECTIONS:
            emit({"type": "section", "name": title})
            if title == "Risks and Mitigations":
                table, out = write_risks(ctx)
                extra[title] = [table] if table else []
                prose[title] = ""
                risk_text = out["text"]
                stats[title] = {"rows": out["rows"], "dropped": out["dropped"]}
                continue
            if title in ("Valuation", "Financial Projections") and model.dcf_result is None:
                prose[title] = ""
                continue
            out = write_section(ctx, title, model.sections[title])
            prose[title] = out["text"] if "No verified answer" not in out["text"] else ""
            stats[title] = {"removed": out["removed"], "retried": out["retried"], "passages": out["passages"]}
        return {"prose": prose, "stats": stats, "extra_tables": extra, "risk_text": risk_text}

    def n_ground(state: ReportState) -> ReportState:
        # risks are rendered as a table of quoted rows, so grounding runs only on the written sections
        texts = {t: v for t, v in state["prose"].items() if v and t != "Risks and Mitigations"}
        grounded, removed = ground_all(ctx, texts)
        prose = {**state["prose"], **{t: strip_empty(v) for t, v in grounded.items()}}
        stats = {**state["stats"]}
        for t, r in removed.items():
            stats.setdefault(t, {})["grounding_removed"] = len(r)
        return {"prose": prose, "stats": stats}

    def n_render(state: ReportState) -> ReportState:
        model, prose = state["model"], state["prose"]
        rep = evaluate("\n".join([v for v in prose.values() if v] + [state.get("risk_text", "")]), ctx.fb)
        removed = sum(s.get("removed", 0) + s.get("grounding_removed", 0) + s.get("dropped", 0) for s in state["stats"].values())
        verification = {"status": "PASS" if rep.ok else "FAIL", "claims": rep.claims, "matched": rep.matched_facts,
                        "grounded": rep.grounded_passages, "removed": removed}
        html = render_html(ctx, model, prose, state["extra_tables"], verification, [state.get("risk_text", "")])
        pdf, pages = to_pdf(html)
        fmt = format_check(html, pdf, pages)
        pdf_name = f"report_{ctx.ticker}_{ctx.period.replace(' ', '_')}.pdf"
        arts = {pdf_name: pdf, "report.html": html.encode(), **ctx.artifacts}
        if run_id:
            runs.save_artifacts(run_id, arts)
            (runs.run_dir(run_id) / "report_data.json").write_text(json.dumps(
                {"assumptions": model.assumptions.to_dict(), "dcf": model.dcf_result, "warnings": model.warnings, "stats": state["stats"],
                 "factbook": ctx.fb.to_dict()}, indent=2, default=str))
        return {"result": {"ticker": ctx.ticker, "period": ctx.period, "pdf_name": pdf_name, "pages": pages, "eval": rep.summary(),
                           "format": fmt, "warnings": model.warnings, "stats": state["stats"], "verification": verification,
                           "assumptions": model.assumptions.to_dict(), "dcf": model.dcf_result, "seconds": round(time.time() - t_start, 1),
                           "pdf_bytes": pdf if not run_id else None}}

    g = StateGraph(ReportState)
    for name, fn in (("model", n_model), ("write", n_write), ("ground", n_ground), ("render", n_render)):
        g.add_node(name, fn)
    g.set_entry_point("model")
    g.add_edge("model", "write")
    g.add_edge("write", "ground")
    g.add_edge("ground", "render")
    g.add_edge("render", END)
    out = g.compile().invoke({})["result"]
    if out.get("pdf_bytes") is None:
        out.pop("pdf_bytes", None)
    return out


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
