"""Data tools offered to agents: financial series, document search, consensus."""
from __future__ import annotations

from datetime import timedelta

from finrobot.agents.context import RunContext
from finrobot.data import store
from finrobot.data.xbrl import CONCEPTS
from finrobot.llm.client import Tool
from finrobot.tools.metrics import MissingData, period_label, shift

METRICS = ", ".join(CONCEPTS)
DOC_TYPES = ["8-K", "10-Q", "10-K"]


def _row(f) -> dict:
    return {"id": f.id, "label": f.label, "display": f.display, "value": f.value, "unit": f.unit}


def search_passages(ctx: RunContext, query: str, doc_types: list[str] | None = None, k: int = 5, scope: str = "period") -> list[dict]:
    """Hybrid search; registers each hit as a passage (S#) in the FactBook. Scope 'period' = documents published for the focus quarter."""
    emb = ctx.llm.embed([f"search_query: {query}"])[0]
    forms = [d for d in (doc_types or []) if d in DOC_TYPES] or None
    k = min(max(int(k), 1), 8)
    window = None
    if scope != "any" and ctx.period_end:  # published after the focus quarter closed: its 10-Q and earnings release
        window = (ctx.period_end, ctx.period_end + timedelta(days=80))
    hits = store.search_chunks(ctx.ticker, query, emb, forms=forms, k=k, filed_between=window)
    if not hits and window:
        hits = store.search_chunks(ctx.ticker, query, emb, forms=forms, k=k)
    out = []
    for h in hits:
        doc = f"{h['form_type']} filed {h['filed']} · {h['section']}"
        p = ctx.fb.add_passage(h["id"], doc, h["text"])
        out.append({"id": p.id, "doc": doc, "text": h["text"][:900]})
    return out


def data_tools(ctx: RunContext) -> list[Tool]:
    def get_financials(metric: str, quarters: int = 8, annual: bool = False) -> dict:
        if metric not in CONCEPTS:
            return {"error": f"unknown metric '{metric}'. Available: {METRICS}"}
        series = ctx.metrics.series(metric, min(max(int(quarters), 1), 12), quarterly=not annual, up_to=ctx.period_end)
        return {"metric": metric, "newest_first": [_row(f) for f in series]} if series else {"error": f"no data for {metric}"}

    def search_docs(query: str, doc_types: list[str] | None = None, k: int = 5, scope: str = "period") -> dict:
        out = search_passages(ctx, query, doc_types, k, scope)
        return {"passages": out} if out else {"error": "no matching passages"}

    def get_consensus(metric: str, quarters_ago: int = 0) -> dict:
        fiscal_year, fiscal_quarter = shift(ctx.fy, ctx.fq, -int(quarters_ago))
        row = store.get_consensus(ctx.ticker, metric, fiscal_year, fiscal_quarter)
        if not row:
            return {"available": False, "note": "no consensus on file for this period; do not assume one"}
        unit = "USD" if metric in ("revenue", "ebitda", "operating_income", "net_income") else "USD/shares" if metric.startswith("eps") else "x"
        f = ctx.fb.add_fact(f"{ctx.ticker} consensus {metric} {period_label(fiscal_year, fiscal_quarter)}", row["mean"], unit,
                            f"consensus ({row['source']}, as of {row['as_of']})", basis="q")
        return {"available": True, **_row(f), "analysts": row["n_analysts"]}

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    return [
        Tool("get_financials", f"Reported financial series for the company, newest first. metric is one of: {METRICS}. "
             "Quarterly by default (discrete fiscal quarters, ending at the focus quarter); annual=true for fiscal years.",
             obj({"metric": {"type": "string"}, "quarters": {"type": "integer"}, "annual": {"type": "boolean"}}, ["metric"]), get_financials),
        Tool("search_docs", "Search the company's filings and earnings releases (hybrid semantic + keyword). Returns passages with ids S#. "
             "By default (scope='period') only documents published for the focus quarter are searched: its earnings release (8-K) and 10-Q. "
             "Use scope='any' for background such as the business description, risk factors or older periods. "
             "doc_types may restrict to 8-K (earnings press releases), 10-Q, 10-K.",
             obj({"query": {"type": "string"}, "doc_types": {"type": "array", "items": {"type": "string"}}, "k": {"type": "integer"},
                  "scope": {"type": "string", "enum": ["period", "any"]}}, ["query"]),
             search_docs),
        Tool("get_consensus", "Analyst consensus for a metric (revenue, ebitda, eps_diluted ...) for the focus quarter (quarters_ago=0) "
             "or an earlier one, if consensus has been loaded.",
             obj({"metric": {"type": "string"}, "quarters_ago": {"type": "integer"}}, ["metric"]), get_consensus),
    ]


__all__ = ["data_tools", "MissingData"]
