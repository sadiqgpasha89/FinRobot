import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from fakes import make_gateway, tool_call
from finrobot.agents.react import MALFORMED_CALL, NO_MORE_TOOLS, run_react
from finrobot.llm.tools import Tool

OBJ = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def double(x: int) -> dict:
    return {"v": x * 2}


def broken(x: int) -> dict:
    raise ZeroDivisionError("nope")


TOOLS = [Tool("double", "doubles", OBJ, double), Tool("broken", "always fails", OBJ, broken)]


def run(figures, max_steps=6, tools=TOOLS):
    gw, models = make_gateway(figures=figures)
    events = []
    text, calls = run_react(gw, "figures", "system", "user", tools, max_steps=max_steps, on_event=events.append)
    return text, calls, events, models["figures"]


def test_a_tool_call_then_an_answer():
    text, calls, events, model = run([tool_call("double", x=21), "the answer is 42"])
    assert text == "the answer is 42"
    assert calls == [{"tool": "double", "args": {"x": 21}, "result": {"v": 42}}]
    assert events == [{"type": "tool", "tool": "double", "args": {"x": 21}}]
    tool_msgs = [m for m in model.seen[1]["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs and '"v": 42' in tool_msgs[0].content and tool_msgs[0].name == "double"
    assert model.seen[0]["tools"] == ["double", "broken"]


def test_a_failing_or_unknown_tool_returns_its_error_to_the_model():
    text, calls, _, model = run([tool_call("broken", x=1), tool_call("missing", "c2", x=1), "done"])
    assert text == "done"
    assert calls[0]["result"] == {"error": "ZeroDivisionError: nope"}
    assert calls[1]["result"] == {"error": "unknown tool missing"}


def test_bad_arguments_are_a_tool_error_not_a_crash():
    text, calls, _, _ = run([tool_call("double", y=1), "recovered"])
    assert text == "recovered" and "error" in calls[0]["result"]


def test_the_last_step_withholds_tools_and_asks_for_an_answer():
    text, _, _, model = run([tool_call("double", x=1), "forced final answer"], max_steps=2)
    assert text == "forced final answer"
    assert model.seen[1]["tools"] == []
    assert any(isinstance(m, HumanMessage) and m.content == NO_MORE_TOOLS for m in model.seen[1]["messages"])


def test_a_model_that_keeps_calling_tools_past_the_limit_yields_no_text():
    text, calls, _, _ = run([tool_call("double", x=1), tool_call("double", "c2", x=2)], max_steps=2)
    assert text == "" and len(calls) == 1


def test_a_malformed_tool_call_is_retried_with_a_nudge():
    text, calls, _, model = run([RuntimeError("error parsing tool call: raw='{x'"), tool_call("double", x=2), "four"])
    assert text == "four" and calls[0]["result"] == {"v": 4}
    assert any(isinstance(m, HumanMessage) and m.content == MALFORMED_CALL for m in model.seen[1]["messages"])


def test_other_provider_errors_propagate():
    with pytest.raises(RuntimeError, match="boom"):
        run([RuntimeError("boom")])


def test_without_tools_it_is_a_single_completion():
    text, calls, _, model = run(["just prose"], max_steps=1, tools=[])
    assert text == "just prose" and calls == [] and model.seen[0]["tools"] == []
