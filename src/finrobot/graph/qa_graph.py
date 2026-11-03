"""Question-answering graph: route -> Data-CoT -> Concept-CoT -> Thesis-CoT -> merge. It short-circuits to the merge when
Data-CoT cannot establish any verified figure, or when the question asks for a figure rather than a cause."""
from __future__ import annotations

import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from finrobot.agents.context import RunContext, resolve_period
from finrobot.agents.evaluator import cites_in, evaluate
from finrobot.agents.qa_agents import concept_cot, data_cot, route, split_thesis, thesis_cot, wants_explanation
from finrobot.data import store
from finrobot.llm.client import OllamaLLM

NO_ANSWER = "_No verified answer could be produced for this part._"


class QAState(TypedDict, total=False):
    request: str
    route: dict
    data: dict
    concept: dict
    thesis: dict
    result: dict


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


def build_qa_graph(ctx_holder: dict, llm: OllamaLLM, ticker: str | None, emit: Callable[[dict], None]):
    def n_route(state: QAState) -> QAState:
        r = route(llm, state["request"], ticker)
        company = store.find_company(ticker or r["company"])
        if company is None:
            raise LookupError(f"'{ticker or r['company']}' is not ingested. Run: finrobot ingest <TICKER>")
        fy, fq = resolve_period(company["ticker"], r.get("fiscal_year"), r.get("fiscal_quarter"))
        ctx = RunContext(company["ticker"], company["name"], llm, fy=fy, fq=fq, emit=emit)
        ctx_holder["ctx"] = ctx
        emit({"type": "route", "ticker": ctx.ticker, "period": ctx.period, **{k: r[k] for k in ("data_question", "concept_question", "thesis_question")}})
        return {"route": {**r, "ticker": ctx.ticker, "period": ctx.period}}

    def n_data(state: QAState) -> QAState:
        return {"data": data_cot(ctx_holder["ctx"], state["route"]["data_question"])}

    def n_concept(state: QAState) -> QAState:
        return {"concept": concept_cot(ctx_holder["ctx"], state["route"]["concept_question"], state["data"]["text"])}

    def n_thesis(state: QAState) -> QAState:
        return {"thesis": thesis_cot(ctx_holder["ctx"], state["route"]["thesis_question"], state["data"]["text"], state["concept"]["text"])}

    def n_merge(state: QAState) -> QAState:
        ctx: RunContext = ctx_holder["ctx"]
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

    g = StateGraph(QAState)
    g.add_node("route", n_route)
    g.add_node("data", n_data)
    g.add_node("concept", n_concept)
    g.add_node("thesis", n_thesis)
    g.add_node("merge", n_merge)
    g.set_entry_point("route")
    g.add_edge("route", "data")
    g.add_conditional_edges("data", after_data, {"concept": "concept", "merge": "merge"})
    g.add_edge("concept", "thesis")
    g.add_edge("thesis", "merge")
    g.add_edge("merge", END)
    return g.compile()


def run_qa(request: str, ticker: str | None = None, emit: Callable[[dict], None] | None = None,
           llm: OllamaLLM | None = None) -> tuple[dict, RunContext]:
    emit = emit or (lambda e: None)
    llm = llm or OllamaLLM()
    holder: dict[str, Any] = {}
    t0 = time.time()
    graph = build_qa_graph(holder, llm, ticker, emit)
    final = graph.invoke({"request": request})
    result = final["result"]
    result["seconds"] = round(time.time() - t0, 1)
    return result, holder["ctx"]
