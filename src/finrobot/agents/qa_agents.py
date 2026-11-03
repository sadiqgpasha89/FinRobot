"""The multi-agent chain-of-thought team: Router, Data-CoT, Concept-CoT, Thesis-CoT.

Model roles (one GPU slot): figures (gpt-oss) for Router and Data-CoT; narrative (gemma) for Concept and Thesis,
so a run swaps models once.
"""
from __future__ import annotations

import re
import time

from finrobot.agents.context import RunContext
from finrobot.agents.evaluator import EvalReport, evaluate
from finrobot.agents.function_agent import function_tools
from finrobot.agents.grounding import grounding_check
from finrobot.agents.math_agent import math_tools
from finrobot.llm.client import Tool, parse_json, run_tools
from finrobot.tools.metrics import period_label, shift
from finrobot.tools.registry import data_tools, search_passages

RULES = """Rules you must follow:
- Every figure you state comes from a tool result and is followed by that result's id in square brackets, like "$6.85 billion [F12]".
- Never calculate in your head. Use compute_metric or calc, then cite the id it returns.
- Copy numbers exactly as the tool's "display" field shows them. Do not round differently, do not invent numbers.
- A cause or quote taken from a document is cited with the passage id, like [S3]. Never cite an id you were not given.
- If the data needed is not available, say so plainly instead of guessing.
- Plain text, short bullet lines starting with "- ". No tables."""

ROUTER_SCHEMA = {"type": "object", "properties": {
    "company": {"type": "string"}, "data_question": {"type": "string"}, "concept_question": {"type": "string"},
    "thesis_question": {"type": "string"}},
    "required": ["company", "data_question", "concept_question", "thesis_question"]}

_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}


_WHY = re.compile(r"\b(why|what\s+(?:drove|caused|explains?)|drivers?|driven|reasons?|because|explain\w*|causes?|behind|story|thesis|outlook|how\s+come|"
                  r"contribut\w+)\b", re.I)


def wants_explanation(request: str) -> bool:
    """Only questions that ask for a cause or story need the Concept and Thesis agents; a figure question gets a figure."""
    return bool(_WHY.search(request))


def parse_period(text: str) -> tuple[int | None, int | None]:
    """Fiscal year / quarter named in the question, else (None, None). Done in code: a model will invent a year."""
    q = re.search(r"\bQ([1-4])\b", text, re.I)
    quarter = int(q.group(1)) if q else None
    if quarter is None:
        w = re.search(r"\b(first|second|third|fourth)[- ]quarter\b", text, re.I)
        quarter = _ORD[w.group(1).lower()] if w else None
    y = re.search(r"\b(?:FY\s?)?((?:19|20)\d{2})\b", text)
    return (int(y.group(1)) if y else None), quarter


def route(ctx_llm, request: str, forced_ticker: str | None = None) -> dict:
    """Split the user's question into the three sub-questions of the chain-of-thought flow."""
    system = ("You split an equity-research question into three sub-questions for specialist agents. "
              "data_question: the factual numbers (actuals, comparison, versus consensus if mentioned). "
              "concept_question: why it happened, based on the figures. "
              "thesis_question: how best to tell the story given the company and industry. "
              "company is the company name or ticker as written in the question. Never add dates, years or quarters that the question does not contain.")
    reply = ctx_llm.chat("figures", [{"role": "system", "content": system}, {"role": "user", "content": request}],
                         schema=ROUTER_SCHEMA, temperature=0)
    parsed = parse_json(reply.content) or {}
    parsed.setdefault("company", forced_ticker or "")
    years = lambda t: set(re.findall(r"(?:19|20)\d{2}", t or ""))
    for key, tmpl in (("data_question", "What are the key figures behind this question: {q}"),
                      ("concept_question", "Based on the figures, why did this happen: {q}"),
                      ("thesis_question", "Given the company and its industry, what is the best way to tell this story: {q}")):
        # a model will happily rewrite 'Q3 versus a year ago' as 'Q3 2024'; never let a year in that the user did not write
        if not parsed.get(key) or years(parsed[key]) - years(request):
            parsed[key] = tmpl.format(q=request)
    if forced_ticker:
        parsed["company"] = forced_ticker
    parsed["fiscal_year"], parsed["fiscal_quarter"] = parse_period(request)
    return parsed


def _lines_with_issues(text: str, rep: EvalReport) -> set[str]:
    return {i.where for i in rep.hard}


def sanitize(text: str, rep: EvalReport) -> tuple[str, int]:
    """Drop lines that still carry unsupported or uncited figures. Returns (clean text, lines removed)."""
    bad = _lines_with_issues(text, rep)
    kept, removed = [], 0
    for line in text.splitlines():
        if line.strip() in bad:
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept).strip(), removed


def covers_focus(ctx: RunContext, text: str) -> bool:
    """At least one fact cited in `text` is for the focus period (real figures for the wrong quarter pass the numeric check)."""
    from finrobot.agents.evaluator import cites_in
    return any(ctx.period in ctx.fb.facts[i].label for i in cites_in(text) if i in ctx.fb.facts)


def checked(ctx: RunContext, role: str, system: str, user: str, tools: list[Tool], who: str, max_steps: int = 7,
            must_cover_focus: bool = False) -> dict:
    """Run a tool-using agent, evaluate its text, retry once with the issues, then strip anything still unverified."""
    ctx.event("agent_start", agent=who)
    t0 = time.time()
    text, calls = run_tools(ctx.llm, role, system, user, tools, max_steps=max_steps, on_event=lambda e: ctx.emit({**e, "agent": who}))
    rep = evaluate(text, ctx.fb)
    retried = False
    wrong_period = bool(text) and must_cover_focus and not covers_focus(ctx, text)
    if text and (not rep.ok or wrong_period):
        retried = True
        problems = [str(i) for i in rep.hard]
        if wrong_period:
            problems.append(f"none of your cited figures is for the focus period {ctx.period}; use quarters_ago=0 for it")
        ctx.event("retry", agent=who, issues=problems[:6])
        feedback = ("Your previous answer failed verification:\n" + "\n".join(f"- {i}" for i in problems[:8]) +
                    "\nRewrite it. Keep only figures that come from tool results, cite each with its id, and use calc/compute_metric "
                    "for anything derived. Remove statements you cannot support.\n\nPrevious answer:\n" + text)
        text2, calls2 = run_tools(ctx.llm, role, system, user + "\n\n" + feedback, tools, max_steps=max_steps,
                                  on_event=lambda e: ctx.emit({**e, "agent": who}))
        calls += calls2
        if text2:
            text, rep = text2, evaluate(text2, ctx.fb)
            wrong_period = must_cover_focus and not covers_focus(ctx, text)
    removed = 0
    if not rep.ok:
        text, removed = sanitize(text, rep)
        rep = evaluate(text, ctx.fb)
    if wrong_period:  # still about the wrong quarter after a retry: better no answer than a confident wrong one
        ctx.event("agent_done", agent=who, tool_calls=len(calls), retried=True, removed=0, seconds=round(time.time() - t0, 1), **{"eval": rep.summary()})
        return {"text": "_No verified answer could be produced for this part._", "eval": rep.summary(), "retried": True,
                "removed": 0, "tool_calls": len(calls), "wrong_period": True}
    ctx.event("agent_done", agent=who, tool_calls=len(calls), retried=retried, removed=removed, seconds=round(time.time() - t0, 1),
              **{"eval": rep.summary()})
    return {"text": text or "_No verified answer could be produced for this part._", "eval": rep.summary(),
            "retried": retried, "removed": removed, "tool_calls": len(calls)}


def _frame(ctx: RunContext, question: str) -> str:
    fy, fq = ctx.fy, ctx.fq
    names = {n: period_label(*shift(fy, fq, -n)) for n in (0, 1, 4)}
    return (f"Company: {ctx.name} ({ctx.ticker}). Data comes from SEC filings already ingested.\n"
            f"Periods are relative to the FOCUS QUARTER {names[0]}: quarters_ago=0 is {names[0]} ('the quarter'), "
            f"quarters_ago=1 is {names[1]} ('last quarter'), quarters_ago=4 is {names[4]} ('a year ago', year over year).\n"
            f"Question: {question}")


def data_cot(ctx: RunContext, question: str) -> dict:
    system = ("You are the Data-CoT agent of an equity research team. Establish the facts: the actual figure, its comparison "
              "(year over year and quarter over quarter), and the size of any miss versus consensus when consensus is on file. "
              "A change in a margin is stated in basis points: use compute_metric ebitda_margin_change_yoy / operating_margin_change_yoy, never a "
              "percent-of-a-percent. Leverage is compute_metric net_debt_to_ttm_ebitda: never divide a balance-sheet figure by one quarter's flow. Work step by step with the tools, then give 3-8 bullet lines of exact figures. Do NOT explain causes.\n" + RULES)
    tools = data_tools(ctx) + math_tools(ctx)
    return checked(ctx, "figures", system, _frame(ctx, question), tools, "Data-CoT", must_cover_focus=True)


DRIVER_QUERIES = ["what drove the change in revenue in the quarter: pricing, volume, acquisitions",
                  "operating margin and cost drivers: labor, fuel, maintenance, productivity",
                  "commodity prices and recycled materials impact",
                  "management commentary on the quarter results and outlook"]


def prefetch_evidence(ctx: RunContext, question: str, per_query: int = 3, cap: int = 9) -> list[dict]:
    """Retrieve the focus quarter's own documents up front. Relying on a small model to decide to search proved unreliable."""
    seen, out = set(), []
    for q in [question] + DRIVER_QUERIES:
        for hit in search_passages(ctx, q, ["8-K", "10-Q"], per_query, "period"):
            if hit["id"] not in seen:
                seen.add(hit["id"])
                out.append(hit)
    ctx.event("evidence", passages=len(out))
    return out[:cap]


def evidence_block(hits: list[dict], width: int = 700) -> str:
    return "\n\n".join(f"[{h['id']}] ({h['doc']})\n{' '.join(h['text'].split())[:width]}" for h in hits)


def concept_cot(ctx: RunContext, question: str, data_answer: str) -> dict:
    system = ("You are the Concept-CoT agent. Given the verified figures, explain WHY the result occurred, using only what the "
              "company's own documents for this quarter say. The relevant passages are provided below with their ids; you may also call "
              "search_docs for more. Output at most 6 bullets, ranked by importance, each in the form: '- <driver>: <effect, with the document's figure if it gives one> [S#]'. "
              "Only a driver that a retrieved passage actually states may be listed as a cause; if the documents do not explain the "
              "result, say that in one bullet. Mark anything you infer yourself with 'Inference:'. "
              "Do not restate the Data-CoT figures, do not show calculations, do not add headings or summaries.\n" + RULES)
    hits = prefetch_evidence(ctx, question)
    ctx.extra["evidence"] = hits
    user = (_frame(ctx, question) + "\n\nVerified figures from the Data-CoT agent (you may cite their ids):\n" + data_answer +
            "\n\nPassages from the company's documents for this quarter (cite their ids):\n" + evidence_block(hits))
    return checked(ctx, "narrative", system, user, data_tools(ctx) + math_tools(ctx), "Concept-CoT")


def thesis_cot(ctx: RunContext, question: str, data_answer: str, concept_answer: str) -> dict:
    system = ("You are the Thesis-CoT agent. Combine the facts and the causes into the answer an analyst would give a client. "
              "Output exactly three parts, each starting with its label on its own line:\n"
              "ANSWER: two or three sentences answering the question directly, with the key figures.\n"
              "STORY: a short paragraph on how this changes the investment thesis (revenue drivers, margin direction, "
              "market position) and what is temporary versus structural.\n"
              "WATCH: two or three bullets on what to monitor next quarter.\n"
              "Say nothing about peers, competitors, the industry or macro conditions unless a passage you retrieved with search_docs "
              "says it (cite it); otherwise leave it out. Only claim something is structural if a cited passage supports that; "
              "otherwise describe it as 'one quarter of evidence'. You may call make_chart to attach one useful trend chart.\n" + RULES)
    user = (_frame(ctx, question) + "\n\nFacts (Data-CoT):\n" + data_answer + "\n\nCauses (Concept-CoT):\n" + concept_answer +
            "\n\nPassages you may cite:\n" + evidence_block(ctx.extra.get("evidence", []), 450))
    out = checked(ctx, "narrative", system, user, data_tools(ctx) + math_tools(ctx) + function_tools(ctx), "Thesis-CoT", max_steps=6)
    t0 = time.time()
    out["text"], dropped = grounding_check(ctx, out["text"])
    out["grounding_removed"] = dropped
    ctx.event("grounding", agent="Thesis-CoT", removed=dropped, seconds=round(time.time() - t0, 1))
    return out


def split_thesis(text: str) -> dict[str, str]:
    out = {"ANSWER": "", "STORY": "", "WATCH": ""}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^\W*(ANSWER|STORY|WATCH)\W*:?\s*(.*)$", line.strip(), re.I)
        if m:
            cur = m.group(1).upper()
            out[cur] = m.group(2)
            continue
        if cur:
            out[cur] += "\n" + line
    if not any(v.strip() for v in out.values()):
        out["ANSWER"] = text
    return {k: v.strip() for k, v in out.items()}
