"""The whole question-answering pipeline on a real database, with a scripted model standing in for the LLM.

The scripted replies behave like a competent, honest model: they read the tool results and cite the ids they were given. What is
being tested is everything around the model: routing, period resolution, tools, FactBook ids, verification, merge, persistence.
"""
import json
import re

from langchain_core.messages import HumanMessage, ToolMessage
from seed import RATIOS, TICKER, revenue, seed, seed_filings

from fakes import make_gateway, tool_call
from finrobot import runs, service
from finrobot.data import store
from finrobot.graph.qa_graph import run_qa

QUESTION = "Why did EBITDA change in Q3 versus a year ago?"
ROUTE = json.dumps({"company": TICKER, "data_question": "What were EBITDA and its change?", "concept_question": "Why did EBITDA change?",
                    "thesis_question": "How should the story be told?"})


def tool_results(messages):
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]


def data_answer(messages):
    cur, prior = tool_results(messages)
    return (f"- EBITDA in the quarter was {cur['display']} [{cur['id']}].\n- A year earlier it was {prior['display']} [{prior['id']}].")


def concept_answer(messages):
    human = next(m for m in reversed(messages) if isinstance(m, HumanMessage)).content
    passage = re.search(r"\[(S\d+)\]", human).group(1)
    return f"- Pricing and volume growth lifted results: the release cites collection-business growth [{passage}]."


def thesis_answer(messages):
    human = next(m for m in reversed(messages) if isinstance(m, HumanMessage)).content
    fid = re.search(r"\[(F\d+)\]", human).group(1)
    return f"ANSWER: EBITDA grew year over year [{fid}].\nSTORY: One quarter of evidence.\nWATCH:\n- Pricing against cost inflation."


def scripted():
    return make_gateway(
        figures=[ROUTE, tool_call("compute_metric", "c1", name="ebitda", quarters_ago=0), tool_call("compute_metric", "c2", name="ebitda", quarters_ago=4),
                 data_answer, '{"unsupported": []}'],
        narrative=[concept_answer, thesis_answer])


def test_a_why_question_runs_all_three_agents_and_every_figure_ties_to_the_database(clean):
    seed()
    seed_filings()
    gw, models = scripted()
    events = []
    result, ctx = run_qa(QUESTION, TICKER, events.append, gw)

    md = result["answer_md"]
    for heading in ("## Answer", "## Key figures", "## Why it happened", "## What it means", "## What to watch", "## Sources"):
        assert heading in md, heading
    assert (result["ticker"], result["period"]) == (TICKER, "FY2026 Q3") and result["eval"]["ok"] and result["eval"]["claims"] == 2

    # the cited EBITDA is operating income + D&A, recomputed here independently from the seeded inputs
    want_now = revenue(14) * (RATIOS["operating_income"] + RATIOS["d_and_a"])
    want_prior = revenue(10) * (RATIOS["operating_income"] + RATIOS["d_and_a"])
    ebitda = sorted(f.value for f in ctx.fb.facts.values() if "EBITDA" in f.label and not f.inputs == [])
    assert any(abs(v - want_now) < 1 for v in ebitda) and any(abs(v - want_prior) < 1 for v in ebitda)

    kinds = [(e["type"], e.get("agent")) for e in events]
    assert kinds[0][0] == "route" and ("agent_start", "Data-CoT") in kinds and ("agent_start", "Concept-CoT") in kinds and ("agent_start", "Thesis-CoT") in kinds
    assert [e["tool"] for e in events if e["type"] == "tool"] == ["compute_metric", "compute_metric"]
    assert models["figures"].script == [] and models["narrative"].script == []        # every scripted reply was consumed, none extra


def test_a_figure_question_skips_the_explanation_agents(clean):
    seed()
    seed_filings()
    gw, models = make_gateway(figures=[ROUTE, tool_call("compute_metric", "c1", name="revenue_growth_yoy", quarters_ago=0),
                                       lambda m: f"- Revenue grew {tool_results(m)[0]['display']} year over year [{tool_results(m)[0]['id']}]."])
    result, _ = run_qa("What was revenue growth in Q3 year over year?", TICKER, None, gw)
    assert "## Answer" in result["answer_md"] and "## Why it happened" not in result["answer_md"] and result["eval"]["ok"]
    assert models["narrative"].seen == []                                              # the narrative model was never called


def test_an_unverifiable_answer_is_withheld_not_softened(clean):
    seed()
    wrong = "- EBITDA was $9.99 billion [F1]."
    gw, _ = make_gateway(figures=[ROUTE, tool_call("compute_metric", "c1", name="ebitda", quarters_ago=0), wrong, wrong])
    result, _ = run_qa("What was EBITDA in Q3?", TICKER, None, gw)
    assert "9.99" not in result["answer_md"] and "No verified answer" in result["answer_md"]


def test_an_unknown_company_fails_with_an_actionable_message(clean):
    seed()
    gw, _ = make_gateway(figures=[json.dumps({"company": "Nonexistent Corp", "data_question": "a", "concept_question": "b", "thesis_question": "c"})])
    try:
        run_qa("What is Nonexistent Corp's revenue?", None, None, gw)
    except LookupError as e:
        assert "not ingested" in str(e) and "finrobot ingest" in str(e)
    else:
        raise AssertionError("expected LookupError")


def test_a_blank_company_is_not_resolved_to_an_arbitrary_one(clean):
    """Regression: an empty router answer became ILIKE '%%' and matched whichever company had the shortest name."""
    seed()
    gw, _ = make_gateway(figures=[json.dumps({"company": "", "data_question": "a", "concept_question": "b", "thesis_question": "c"})])
    try:
        run_qa("What was revenue?", None, None, gw)
    except LookupError as e:
        assert "which company" in str(e)
    else:
        raise AssertionError("expected LookupError")


def test_the_service_records_the_run_events_usage_and_artifacts(clean):
    seed()
    seed_filings()
    gw, _ = scripted()
    run_id, emit = runs.new_run("qa", {"question": QUESTION})
    result = service.run_job(run_id, emit, lambda: service.execute_qa(run_id, emit, QUESTION, TICKER, gw))
    assert result is not None
    row = store.get_run(run_id)
    assert row["status"] == "done" and row["result"]["eval"]["ok"] and row["usage"]["calls"] == 7           # route, data x3, grounding, concept, thesis
    assert {"route", "agent_start", "tool", "agent_done", "grounding"} <= {e["type"] for e in row["events"]}
    assert (runs.run_dir(run_id) / "answer.md").read_text() == result["answer_md"]
    assert set(row["usage"]["by_role"]) == {"figures", "narrative"}


def test_a_failing_run_is_recorded_as_failed_with_the_error(clean):
    seed()
    gw, _ = make_gateway(figures=[RuntimeError("model server exploded")])
    run_id, emit = runs.new_run("qa", {"question": QUESTION})
    assert service.run_job(run_id, emit, lambda: service.execute_qa(run_id, emit, QUESTION, TICKER, gw)) is None
    row = store.get_run(run_id)
    assert row["status"] == "failed" and "model server exploded" in row["error"] and row["events"][-1]["type"] == "error"
