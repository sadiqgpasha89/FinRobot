import re

from langchain_core.messages import AIMessage, HumanMessage

from fakes import ScriptedChatModel, make_gateway
from finrobot import observability
from finrobot.config import settings
from finrobot.llm.usage import UsageTracker
from finrobot.observability import qa_scores, report_scores, trace_run


def test_tracing_is_a_noop_without_backends_but_still_counts_usage():
    with trace_run("20260101-000000-abcde", "qa", {"q": "x"}, metadata={"ticker": "WM"}) as tr:
        cfg = tr.config
        assert tr.trace_id is None and "run_id" not in cfg
        assert cfg["metadata"] == {"finrobot_run_id": "20260101-000000-abcde", "mode": "qa", "ticker": "WM"}
        assert cfg["run_name"] == "finrobot.qa" and "qa" in cfg["tags"]
        assert any(isinstance(c, UsageTracker) for c in cfg["callbacks"])
        tr.score("verification_ok", True)      # must not raise with no backend configured
        tr.set_output("answer")


def test_usage_tracker_sums_tokens_calls_and_latency_per_role():
    tracker = UsageTracker()
    reply = AIMessage("x", usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})
    model = ScriptedChatModel(script=[reply, reply])
    for role in ("figures", "narrative"):
        model.invoke([HumanMessage("q")], config={"callbacks": [tracker], "metadata": {"role": role, "model": "m"}})
    snap = tracker.snapshot()
    assert (snap["calls"], snap["input_tokens"], snap["output_tokens"]) == (2, 22, 14)
    assert set(snap["by_role"]) == {"figures", "narrative"} and snap["by_role"]["figures"]["input_tokens"] == 11


def test_usage_flows_through_the_gateway_and_a_nested_graph():
    from fakes import tool_call
    from finrobot.agents.react import run_react
    from finrobot.llm.tools import Tool
    gw, models = make_gateway(figures=[tool_call("t"), AIMessage("done", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})])
    with trace_run("20260101-000000-fffff", "qa") as tr:
        from langchain_core.runnables import RunnableLambda
        RunnableLambda(lambda _: run_react(gw, "figures", "s", "u", [Tool("t", "d", {"type": "object", "properties": {}}, lambda: {})])).invoke(
            None, config=tr.config)
        snap = tr.usage.snapshot()
    assert snap["calls"] == 2 and snap["by_role"]["figures"]["calls"] == 2   # callbacks reach the model calls inside the graph


def test_scores_summarise_verification_and_cleanup():
    qa = {"eval": {"ok": True, "claims": 10, "matched_facts": 7, "grounded_in_passages": 2},
          "agents": {"data": {"removed": 1, "retried": True}, "thesis": {"removed": 0, "grounding_removed": ["sentence one", "sentence two"]}}}   # QA keeps the removed sentences, reports a count
    s = qa_scores(qa)
    assert s == {"verification_ok": True, "figures_checked": 10, "figures_tied_ratio": 0.9, "lines_removed": 3, "agent_retries": 1}
    assert qa_scores({"eval": {"ok": False, "claims": 0}})["figures_tied_ratio"] == 1.0
    r = report_scores({"eval": {"ok": True, "claims": 5}, "format": {"ok": False}, "pages": 8,
                       "stats": {"A": {"removed": 1, "grounding_removed": 1}, "Risks": {"dropped": 2}}})
    assert r == {"verification_ok": True, "format_ok": False, "figures_checked": 5, "lines_removed": 4, "pages": 8}


def test_langfuse_receives_one_trace_per_run_with_nested_llm_spans_and_scores(monkeypatch):
    """The real Langfuse SDK, with spans captured in memory instead of sent: proves what we emit without needing a server."""
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test", base_url="http://127.0.0.1:9", span_exporter=exporter, flush_at=1)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(type(settings.langfuse_secret_key), "get_secret_value", lambda self: "sk-lf-test", raising=False)
    monkeypatch.setattr(observability, "_initialised", True)

    run_id = "20260101-000000-lf001"
    gw, _ = make_gateway(figures=["the answer"])
    with trace_run(run_id, "qa", {"question": "why?"}, metadata={"ticker": "WM"}) as tr:
        assert re.fullmatch(r"[0-9a-f]{32}", tr.trace_id) and tr.trace_id == client.create_trace_id(seed=run_id)   # derivable from the run id
        gw.chat_model("figures").invoke([HumanMessage("hi")], config=tr.config)
        tr.set_output("the answer")
        tr.score("verification_ok", True)
    client.flush()

    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "finrobot.qa.request")
    assert root.attributes["langfuse.observation.type"] == "chain"
    trace_ids = {format(s.context.trace_id, "032x") for s in spans}
    assert trace_ids == {tr.trace_id}                                                  # everything is in the one trace
    assert root.attributes["session.id"] == run_id                            # the FinRobot run id is the Langfuse session
    assert root.attributes["langfuse.trace.name"] == "finrobot.qa"
    llm = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
    assert llm and all(s.parent.span_id == root.context.span_id for s in llm)           # the model call nests under the run
    assert llm[0].attributes["session.id"] == run_id                           # session/tags propagate to child spans
