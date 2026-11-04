"""Generate → verify → repair, as a LangGraph subgraph.

    draft ──▶ verify ──ok──────────────────────────────▶ finish
                 │  └─ issues, first attempt ─▶ retry ─▶ draft (again, with the issues in the prompt)
                 └──── issues after the retry ─▶ sanitize ─▶ finish   (unverifiable lines are REMOVED, never softened)

`draft` is the ReAct tool loop. `verify` is the deterministic Evaluator: every unit-bearing number must tie to a registered fact or a
cited passage, and the cited facts must be for the period the text names. In Langfuse/LangSmith each attempt, each verdict and each
repair is its own span, so a "no verified answer" can be traced to the exact claim that failed.
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from finrobot.agents.context import RunContext
from finrobot.agents.evaluator import EvalReport, cites_in, evaluate
from finrobot.agents.react import run_react
from finrobot.llm.tools import Tool

NO_ANSWER = "_No verified answer could be produced for this part._"


def sanitize(text: str, rep: EvalReport) -> tuple[str, int]:
    """Drop lines that still carry unsupported or uncited figures. Returns (clean text, lines removed)."""
    bad = {i.where for i in rep.hard}
    kept, removed = [], 0
    for line in text.splitlines():
        if line.strip() in bad:
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept).strip(), removed


def covers_focus(ctx: RunContext, text: str) -> bool:
    """At least one fact cited in `text` is for the focus period (real figures for the wrong quarter pass the numeric check)."""
    return any(ctx.period in ctx.fb.facts[i].label for i in cites_in(text) if i in ctx.fb.facts)


class CheckedState(TypedDict, total=False):
    attempt: int
    t0: float
    text: str
    calls: list[dict]
    feedback: str
    rep: EvalReport
    wrong_period: bool
    retried: bool
    removed: int
    out: dict


def build_checked_graph(ctx: RunContext, role: str, system: str, user: str, tools: list[Tool], who: str, max_steps: int, must_cover_focus: bool):
    def draft(state: CheckedState) -> dict[str, Any]:
        attempt = state.get("attempt", 0)
        started = time.time()
        if attempt == 0:
            ctx.event("agent_start", agent=who)
        prompt = user if attempt == 0 else user + "\n\n" + state["feedback"]
        text, calls = run_react(ctx.llm, role, system, prompt, tools, max_steps=max_steps, on_event=lambda e: ctx.emit({**e, "agent": who}))
        if attempt == 0:
            return {"text": text, "calls": calls, "attempt": 1, "t0": started}
        # a retry that produced nothing keeps the first draft
        return {"text": text or state.get("text", ""), "calls": state.get("calls", []) + calls, "attempt": attempt + 1}

    def verify(state: CheckedState) -> dict[str, Any]:
        text = state.get("text", "")
        rep = evaluate(text, ctx.fb)
        wrong = bool(text) and must_cover_focus and not covers_focus(ctx, text)
        return {"rep": rep, "wrong_period": wrong}

    def after_verify(state: CheckedState) -> str:
        failing = bool(state.get("text")) and (not state["rep"].ok or state["wrong_period"])
        if failing and state["attempt"] == 1:
            return "retry"
        return "sanitize" if not state["rep"].ok else "finish"

    def retry(state: CheckedState) -> dict[str, Any]:
        problems = [str(i) for i in state["rep"].hard]
        if state["wrong_period"]:
            problems.append(f"none of your cited figures is for the focus period {ctx.period}; use quarters_ago=0 for it")
        ctx.event("retry", agent=who, issues=problems[:6])
        feedback = ("Your previous answer failed verification:\n" + "\n".join(f"- {i}" for i in problems[:8]) +
                    "\nRewrite it. Keep only figures that come from tool results, cite each with its id, and use calc/compute_metric "
                    "for anything derived. Remove statements you cannot support.\n\nPrevious answer:\n" + state["text"])
        return {"feedback": feedback, "retried": True}

    def sanitize_node(state: CheckedState) -> dict[str, Any]:
        text, removed = sanitize(state["text"], state["rep"])
        return {"text": text, "removed": removed, "rep": evaluate(text, ctx.fb)}

    def finish(state: CheckedState) -> dict[str, Any]:
        rep, text, calls = state["rep"], state.get("text", ""), state.get("calls", [])
        retried, removed = state.get("retried", False), state.get("removed", 0)
        seconds = round(time.time() - state.get("t0", time.time()), 1)
        if state["wrong_period"]:  # still about the wrong quarter after a retry: better no answer than a confident wrong one
            ctx.event("agent_done", agent=who, tool_calls=len(calls), retried=True, removed=0, seconds=seconds, **{"eval": rep.summary()})
            return {"out": {"text": NO_ANSWER, "eval": rep.summary(), "retried": True, "removed": 0, "tool_calls": len(calls), "wrong_period": True}}
        ctx.event("agent_done", agent=who, tool_calls=len(calls), retried=retried, removed=removed, seconds=seconds, **{"eval": rep.summary()})
        return {"out": {"text": text or NO_ANSWER, "eval": rep.summary(), "retried": retried, "removed": removed, "tool_calls": len(calls)}}

    g = StateGraph(CheckedState)
    for name, fn in (("draft", draft), ("verify", verify), ("retry", retry), ("sanitize", sanitize_node), ("finish", finish)):
        g.add_node(name, fn)
    g.set_entry_point("draft")
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", after_verify, {"retry": "retry", "sanitize": "sanitize", "finish": "finish"})
    g.add_edge("retry", "draft")
    g.add_edge("sanitize", "finish")
    g.add_edge("finish", END)
    # checkpointer=False: never persist this subgraph's (non-serialisable) state even when the parent graph has a checkpointer
    return g.compile(checkpointer=False)


def checked(ctx: RunContext, role: str, system: str, user: str, tools: list[Tool], who: str, max_steps: int = 7,
            must_cover_focus: bool = False) -> dict:
    """Run a tool-using agent, verify its text, retry once with the issues, then strip anything still unverified."""
    graph = build_checked_graph(ctx, role, system, user, tools, who, max_steps, must_cover_focus)
    final = graph.invoke({}, config={"run_name": f"agent:{who}", "tags": [f"agent:{who}"], "metadata": {"agent": who}, "recursion_limit": 40})
    return final["out"]
