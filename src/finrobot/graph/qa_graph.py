"""Question-answering graph: route -> Data-CoT -> Concept-CoT -> Thesis-CoT -> merge.

It short-circuits to the merge when Data-CoT cannot establish any verified figure, or when the question asks for a figure rather
than a cause. Run-scoped objects (the LLM gateway, the event sink, the RunContext holding the FactBook) travel in LangGraph's
``Runtime`` context, never in the state, so the state stays small and serialisable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from finrobot.agents.checked import NO_ANSWER
from finrobot.agents.context import RunContext, resolve_period
from finrobot.agents.evaluator import cites_in, evaluate
from finrobot.agents.qa_agents import concept_cot, data_cot, route, split_thesis, thesis_cot, wants_explanation
from finrobot.data import store
from finrobot.llm.gateway import LLMGateway


class QAState(TypedDict, total=False):
    request: str
    route: dict
    data: dict
    concept: dict
    thesis: dict
    result: dict


@dataclass
class QARuntime:
    llm: LLMGateway
    ticker: str | None
    emit: Callable[[dict], None]
    ctx: RunContext | None = None


def sources_block(ctx: RunContext, text: str) -> str:
    ids = list(dict.fromkeys(cites_in(text)))
    lines = []
    for i in ids:
        if i in ctx.fb.facts:
            f = ctx.fb.facts[i]
            lines.append(f"- **{i}** {f.label}: {f.display} — {f.source}")
        elif i in ctx.fb.passages:
            p = ctx.fb.passages[i]
            snippet = " ".join(p.text.split())[:170]
            lines.append(f"- **{i}** {p.doc} — “{snippet}…”")
    return "\n".join(lines)


def n_route(state: QAState, runtime: Runtime[QARuntime]) -> QAState:
    rt = runtime.context
    r = route(rt.llm, state["request"], rt.ticker)
    wanted = rt.ticker or r["company"]
    company = store.find_company(wanted)
    if company is None:
        raise LookupError(f"'{wanted}' is not ingested. Run: finrobot ingest <TICKER>" if wanted
                          else "Could not tell which company the question is about; pass a ticker")
    fy, fq = resolve_period(company["ticker"], r.get("fiscal_year"), r.get("fiscal_quarter"))
    rt.ctx = RunContext(company["ticker"], company["name"], rt.llm, fy=fy, fq=fq, emit=rt.emit)
    rt.emit({"type": "route", "ticker": rt.ctx.ticker, "period": rt.ctx.period,
             **{k: r[k] for k in ("data_question", "concept_question", "thesis_question")}})
    return {"route": {**r, "ticker": rt.ctx.ticker, "period": rt.ctx.period}}


def n_data(state: QAState, runtime: Runtime[QARuntime]) -> QAState:
    return {"data": data_cot(runtime.context.ctx, state["route"]["data_question"])}


def n_concept(state: QAState, runtime: Runtime[QARuntime]) -> QAState:
    return {"concept": concept_cot(runtime.context.ctx, state["route"]["concept_question"], state["data"]["text"])}


def n_thesis(state: QAState, runtime: Runtime[QARuntime]) -> QAState:
    return {"thesis": thesis_cot(runtime.context.ctx, state["route"]["thesis_question"], state["data"]["text"], state["concept"]["text"])}


def n_merge(state: QAState, runtime: Runtime[QARuntime]) -> QAState:
    ctx = runtime.context.ctx
    data, concept = state["data"], state.get("concept") or {"text": ""}
    thesis = state.get("thesis") or {"text": ""}
    parts = split_thesis(thesis["text"]) if thesis["text"] else {"ANSWER": "", "STORY": "", "WATCH": ""}
    md = []
    if not thesis["text"]:   # a figure question: the verified figures are the answer
        md.append("## Answer\n" + data["text"])
    else:
        if parts["ANSWER"] and NO_ANSWER not in parts["ANSWER"]:
            md.append("## Answer\n" + parts["ANSWER"])
        md.append("## Key figures\n" + data["text"])
    if concept["text"]:
        md.append("## Why it happened\n" + concept["text"])
    if parts["STORY"]:
        md.append("## What it means\n" + parts["STORY"])
    if parts["WATCH"]:
        md.append("## What to watch\n" + parts["WATCH"])
    body = "\n\n".join(md)
    src = sources_block(ctx, body)
    if src:
        body += "\n\n## Sources\n" + src
    rep = evaluate(body.split("\n\n## Sources")[0], ctx.fb)
    return {"result": {"answer_md": body, "ticker": ctx.ticker, "period": ctx.period, "eval": rep.summary(),
                       "artifacts": sorted(ctx.artifacts), "factbook": ctx.fb.to_dict(),
                       "agents": {k: {kk: vv for kk, vv in state[k].items() if kk != "text"} for k in ("data", "concept", "thesis") if k in state}}}


def after_data(state: QAState) -> str:
    if NO_ANSWER in state["data"]["text"] or not wants_explanation(state["request"]):
        return "merge"
    return "concept"


def build_qa_graph():
    g = StateGraph(QAState, context_schema=QARuntime)
    for name, fn in (("route", n_route), ("data", n_data), ("concept", n_concept), ("thesis", n_thesis), ("merge", n_merge)):
        g.add_node(name, fn)
    g.set_entry_point("route")
    g.add_edge("route", "data")
    g.add_conditional_edges("data", after_data, {"concept": "concept", "merge": "merge"})
    g.add_edge("concept", "thesis")
    g.add_edge("thesis", "merge")
    g.add_edge("merge", END)
    return g.compile()


def run_qa(request: str, ticker: str | None = None, emit: Callable[[dict], None] | None = None,
           llm: LLMGateway | None = None, config: dict | None = None) -> tuple[dict, RunContext]:
    """Answer one question. `config` is a LangChain config (callbacks, tags, metadata) from `observability.trace_run`."""
    rt = QARuntime(llm or LLMGateway(), ticker, emit or (lambda e: None))
    t0 = time.time()
    final = build_qa_graph().invoke({"request": request}, config={"recursion_limit": 30, **(config or {})}, context=rt)
    result = final["result"]
    result["seconds"] = round(time.time() - t0, 1)
    return result, rt.ctx
