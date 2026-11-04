"""The tool-calling loop as a LangGraph subgraph:  model ──tool calls?──▶ tools ──▶ model … ──▶ END.

Behaviours kept from the original hand-written loop, each one added after a real failure:
* on the last permitted step the tools are withheld and the model is told to answer (a model still mid-loop otherwise returns nothing);
* a malformed tool-call JSON error from the provider becomes a nudge to retry instead of crashing the run;
* a tool that raises returns its error to the model so it can adjust.
Each tool call runs through ``StructuredTool.invoke`` so it appears as a tool span in Langfuse and LangSmith.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from finrobot.llm.gateway import LLMGateway, text_of
from finrobot.llm.tools import Tool, render_result

NO_MORE_TOOLS = "No more tool calls. Write your final answer now, using only the tool results you already have."
MALFORMED_CALL = "Your last tool call was malformed JSON. Call the tool again with valid arguments."


class ReactState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    calls: Annotated[list[dict], operator.add]


def build_react_graph(llm: LLMGateway, role: str, tools: list[Tool], max_steps: int = 6, temperature: float = 0.2,
                      on_event: Callable[[dict], None] | None = None):
    lc_tools = {t.name: t.to_langchain() for t in tools}

    def model(state: ReactState) -> dict[str, Any]:
        steps = state["steps"]
        last = steps >= max_steps - 1
        nudge = [HumanMessage(NO_MORE_TOOLS)] if last else []
        try:
            reply = llm.invoke(role, state["messages"] + nudge, tools=None if (last or not tools) else tools, temperature=temperature)
        except Exception as e:
            if "error parsing tool call" not in str(e):
                raise
            return {"messages": [HumanMessage(MALFORMED_CALL)], "steps": steps + 1}
        return {"messages": [*nudge, reply], "steps": steps + 1}

    def run_tools(state: ReactState, config: RunnableConfig) -> dict[str, Any]:
        last: AIMessage = state["messages"][-1]  # type: ignore[assignment]
        out: list[ToolMessage] = []
        calls: list[dict] = []
        for tc in last.tool_calls:
            name, args = tc["name"], tc.get("args") or {}
            tool = lc_tools.get(name)
            if tool is None:
                result: Any = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = tool.invoke(args, config)
                except Exception as e:   # tool errors go back to the model so it can adjust
                    result = {"error": f"{type(e).__name__}: {e}"}
            calls.append({"tool": name, "args": args, "result": result})
            if on_event:
                on_event({"type": "tool", "tool": name, "args": args})
            out.append(ToolMessage(content=render_result(result), tool_call_id=tc.get("id") or name, name=name))
        return {"messages": out, "calls": calls}

    def after_model(state: ReactState) -> str:
        last = state["messages"][-1]
        if state["steps"] >= max_steps:
            return END
        if isinstance(last, AIMessage):
            return "tools" if last.tool_calls else END
        return "model"   # the last message is our "malformed tool call" nudge: ask again

    g = StateGraph(ReactState)
    g.add_node("model", model)
    g.add_node("tools", run_tools)
    g.set_entry_point("model")
    g.add_conditional_edges("model", after_model, {"tools": "tools", "model": "model", END: END})
    g.add_edge("tools", "model")
    return g.compile(checkpointer=False)   # never persist this loop's state, even inside a checkpointed parent graph


def run_react(llm: LLMGateway, role: str, system: str, user: str, tools: list[Tool], max_steps: int = 6,
              on_event: Callable[[dict], None] | None = None, temperature: float = 0.2) -> tuple[str, list[dict]]:
    """Let the model call tools until it answers in plain text. Returns (text, calls made)."""
    graph = build_react_graph(llm, role, tools, max_steps, temperature, on_event)
    final = graph.invoke({"messages": [SystemMessage(system), HumanMessage(user)], "steps": 0, "calls": []},
                         config={"recursion_limit": max_steps * 2 + 6, "run_name": "tool_loop"})
    last = final["messages"][-1]
    text = text_of(last).strip() if isinstance(last, AIMessage) and not last.tool_calls else ""
    return text, final["calls"]
