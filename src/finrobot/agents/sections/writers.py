"""Section agents: each writes one report section from its own facts and retrieved passages only."""
from __future__ import annotations

import re
import time

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from finrobot.agents.context import RunContext
from finrobot.agents.evaluator import evaluate
from finrobot.agents.grounding import grounding_check
from finrobot.agents.qa_agents import checked, evidence_block
from finrobot.report.model import SectionData
from finrobot.tools.registry import search_passages
from finrobot.tools.tables import html_table

# title -> (what to write, retrieval queries, doc types, scope, target words)
BRIEFS: dict[str, tuple[str, list[str], list[str], str, int]] = {
    "Company Overview": (
        "what the company does, its main lines of business, customers and scale, and how it positions itself in its market",
        ["company business overview lines of business services customers", "competition and market position strategy"], ["10-K"], "any", 150),
    "Thesis and Drivers": (
        "the investment thesis as three labelled drivers: (1) revenue drivers, (2) margin expansion or pressure, (3) market share and growth capture. "
        "Each driver gets two or three sentences, tied to the figures and to what the company's documents say about it",
        ["revenue growth drivers pricing volume acquisitions", "margin improvement cost efficiency productivity", "growth strategy investments and market share"],
        ["8-K", "10-Q", "10-K"], "period", 200),
    "Investment Update": (
        "what happened in the latest quarter versus a year earlier and why: results, the main drivers the company names, and its outlook or guidance if stated",
        ["quarter results highlights revenue EBITDA margin drivers", "full year guidance outlook", "cost and expense drivers in the quarter"],
        ["8-K", "10-Q"], "period", 200),
    "Financial Summary": (
        "a concise read of the tables: the trend in revenue, EBITDA, margins, earnings and free cash flow over the last eight quarters and the fiscal years shown",
        [], [], "period", 110),
    "Valuation": (
        "the DCF result and how sensitive it is: value per share, how much of the value sits in the terminal value, the sensitivity range, the comparison with "
        "the share price if one is given, and the main caveat (the projection is a mechanical extrapolation, not a forecast opinion). Do not give a buy/sell rating",
        [], [], "period", 150),
    "Financial Projections": (
        "the projection: the assumptions used and where each came from, and the resulting revenue, EBITDA and cash flow path. State plainly that the defaults are "
        "mechanical (trailing rates) and can be overridden",
        [], [], "period", 130),
    "Credit and Cashflow": (
        "leverage, interest coverage, free cash flow generation and conversion, and capital intensity, with the direction of travel over the quarters shown",
        ["debt maturities liquidity credit facility", "cash flow capital expenditures and share repurchases dividends"], ["10-Q", "8-K"], "period", 130),
}

RULES = """Rules you must follow:
- Use ONLY the FACTS and PASSAGES below. Every figure you write is followed by the id of the fact it comes from, like "$6.85 billion [F12]". Copy values exactly as shown.
- Do not calculate anything. If a number you want is not in the FACTS, do not use it.
- A statement about causes, strategy, competitors, the industry or outlook must come from a PASSAGE and be cited like [S3]; otherwise leave it out.
- Never cite an id that is not listed. Never write placeholders like [F#].
- Reported results are history: write them in the past tense and never call them "projected" or "expected". Only the projection and valuation sections discuss projections.
- Write for an investor: plain, specific, no hype, no recommendation to buy or sell.
- Output paragraphs (and, where it helps, up to four short bullets starting with "- "). No headings, no tables."""


def facts_block(sd: SectionData) -> str:
    seen, lines = set(), []
    for f in sd.facts:
        if f.id not in seen:
            seen.add(f.id)
            lines.append(f"[{f.id}] {f.label} = {f.display}")
    return "\n".join(lines) or "(none)"


def write_section(ctx: RunContext, title: str, sd: SectionData) -> dict:
    what, queries, forms, scope, words = BRIEFS[title]
    hits, seen = [], set()
    for q in queries:
        for h in search_passages(ctx, q, forms or None, 3, scope):
            if h["id"] not in seen:
                seen.add(h["id"])
                hits.append(h)
    hits = hits[:7]
    system = (f"You are a sell-side equity research analyst writing the '{title}' section of a report on {ctx.name} ({ctx.ticker}). "
              f"Write about {words} words covering: {what}.\n" + RULES)
    user = (f"Focus period: {ctx.period}.\n\nFACTS:\n{facts_block(sd)}\n\nPASSAGES:\n{evidence_block(hits, 600) if hits else '(none retrieved)'}"
            + ("\n\nContext: " + " ".join(sd.notes) if sd.notes else ""))
    out = checked(ctx, "narrative", system, user, [], title, max_steps=1)
    out["passages"] = len(hits)
    return out


class RiskRow(BaseModel):
    risk: str
    why_it_matters: str
    mitigation: str
    source: str


class RisksOut(BaseModel):
    risks: list[RiskRow]


RISK_QUERIES = ["principal risks to the business", "environmental and regulatory liability", "commodity price volatility recycled materials",
                "inflation labor costs and supply chain", "indebtedness interest rates and credit", "cybersecurity and technology disruption"]
MITIGATION_QUERIES = ["how we manage and mitigate risk hedging insurance diversification", "contingency plans controls and compliance programs"]


def write_risks(ctx: RunContext) -> tuple[str, dict]:
    """Structured risks table: each row cites the passage it came from; rows that fail verification are dropped."""
    hits, seen = [], set()
    for q in RISK_QUERIES + MITIGATION_QUERIES:
        for h in search_passages(ctx, q, ["10-K", "10-Q"], 2, "any"):
            if h["id"] not in seen:
                seen.add(h["id"])
                hits.append(h)
    hits = hits[:10]
    ctx.event("agent_start", agent="Risks and Mitigations")
    t0 = time.time()
    system = (f"You are an equity research analyst. From the PASSAGES, list the 5 most important risks to {ctx.name}. For each: risk (a short name), "
              "why_it_matters (one sentence), mitigation (one sentence describing how the company says it manages or offsets that risk, taken from the PASSAGES; "
              "if none is stated write exactly 'None disclosed in the retrieved filings'), and source (the passage id, like S3, that supports the risk). "
              "Use only what the passages say. Do not invent figures.")
    out = ctx.llm.structured("narrative", RisksOut, [SystemMessage(system), HumanMessage(f"PASSAGES:\n{evidence_block(hits, 700)}")], temperature=0.1)
    rows = [r.model_dump() for r in out.risks] if out is not None else []
    kept, dropped, prose = [], 0, []
    for r in rows[:6]:
        src = (r.get("source") or "").strip().strip("[]")
        if src not in ctx.fb.passages:
            dropped += 1
            continue
        text = f"{r.get('risk', '')}: {r.get('why_it_matters', '')} Mitigation: {r.get('mitigation', '')} [{src}]"
        if not evaluate(text, ctx.fb).ok:  # a figure not in the cited passage
            dropped += 1
            continue
        kept.append([r["risk"], f"{r['why_it_matters']} [{src}]", f"{r['mitigation']} [{src}]" if "None disclosed" not in r["mitigation"] else r["mitigation"]])
        prose.append(text)
    table = html_table(["Risk", "Why it matters", "Mitigation (per the company)"], kept, "Principal risks", text=True) if kept else ""
    ctx.event("agent_done", agent="Risks and Mitigations", tool_calls=0, retried=False, removed=dropped, seconds=round(time.time() - t0, 1),
              **{"eval": {"claims": len(rows), "ok": True}})
    return table, {"text": "\n".join(prose), "rows": len(kept), "dropped": dropped}


def ground_all(ctx: RunContext, texts: dict[str, str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """One grounding pass per section, run together so the figures model is swapped in only once."""
    out, removed = {}, {}
    for title, text in texts.items():
        t0 = time.time()
        out[title], removed[title] = grounding_check(ctx, text)
        ctx.event("grounding", agent=title, removed=removed[title], seconds=round(time.time() - t0, 1))
    return out, removed


def strip_empty(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()
