"""The report graph on a real database: full run, human review of the assumptions, and resuming after a crash."""
import pytest
from seed import TICKER, revenue, seed, seed_filings

from fakes import make_gateway
from finrobot.graph.checkpoint import get_checkpointer, memory_checkpointer, reset_checkpointer
from finrobot.graph.report_graph import resume_report, run_report
from finrobot.report.model import SECTIONS

PROSE_SECTIONS = len(SECTIONS) - 1          # every section but the table-driven risks section is written by the narrative model
NARRATIVE_CALLS = len(SECTIONS)             # ... and risks make one structured call too


def narrative(messages):
    from langchain_core.messages import AIMessage
    system = messages[0].content
    if "most important risks" in system:
        return AIMessage('{"risks": []}')
    return AIMessage("- The company provides collection, recycling and disposal services to its customers.")


def gateway(fail_on_call: int | None = None):
    calls = {"n": 0}

    def reply(messages):
        calls["n"] += 1
        if fail_on_call and calls["n"] == fail_on_call:
            raise RuntimeError("model server crashed")
        return narrative(messages)
    gw, models = make_gateway(narrative=[], figures=[])
    models["narrative"].default = reply
    models["figures"].default = '{"unsupported": []}'
    return gw, models


@pytest.fixture(params=["memory", "postgres"])
def checkpointer(request, clean):
    reset_checkpointer()
    return memory_checkpointer() if request.param == "memory" else get_checkpointer()


@pytest.fixture
def company(clean):
    seed()
    seed_filings()


def test_a_full_report_ties_its_valuation_inputs_to_the_database(company, checkpointer):
    gw, models = gateway()
    events = []
    out = run_report(TICKER, events.append, run_id="rep-e2e-1", llm=gw, checkpointer=checkpointer)

    assert out["pdf_name"] == "report_TST_FY2026_Q3.pdf" and out["eval"]["ok"] and out["format"]["ok"], out["format"]
    from finrobot import runs
    pdf = runs.run_dir("rep-e2e-1") / out["pdf_name"]
    assert pdf.read_bytes().startswith(b"%PDF") and out["pages"] >= 3
    # TTM revenue is the sum of the last four seeded quarters, recomputed independently of the metrics code
    assert out["assumptions"]["base_revenue"] == pytest.approx(sum(revenue(i) for i in range(11, 15)), rel=1e-9)
    assert out["dcf"]["per_share"] > 0 and out["assumptions"]["wacc"] == 0.08
    assert len(models["narrative"].seen) == NARRATIVE_CALLS
    assert [e["name"] for e in events if e["type"] == "section"] == SECTIONS


def test_the_reviewer_can_edit_the_assumptions_and_an_invalid_edit_asks_again(company, checkpointer):
    gw, models = gateway()
    paused = run_report(TICKER, None, run_id="rep-review-1", llm=gw, checkpointer=checkpointer, review=True)
    assert paused["status"] == "awaiting_review"
    draft = paused["review"]
    assert draft["type"] == "review_assumptions" and draft["period"] == "FY2026 Q3" and draft["assumptions"]["wacc"] == 0.08
    assert models["narrative"].seen == []                                           # nothing expensive ran before the human looked

    # an invalid edit (WACC below terminal growth) pauses the run again with the reason, instead of failing or poisoning it
    again = resume_report("rep-review-1", {"wacc": 0.01}, checkpointer, llm=gw)
    assert again["status"] == "awaiting_review" and "wacc must be greater" in again["review"]["error"]
    assert models["narrative"].seen == []
    out = resume_report("rep-review-1", {"wacc": 0.09, "terminal_growth": 0.02, "rev_growth": 0.04}, checkpointer, llm=gw)
    a = out["assumptions"]
    assert (a["wacc"], a["terminal_growth"], a["rev_growth"]) == (0.09, 0.02, [0.04] * 5) and a["basis"]["wacc"] == "user override"
    assert out["dcf"]["wacc"] == 0.09 and out["eval"]["ok"]


def test_a_crashed_run_resumes_from_its_last_completed_section(company, checkpointer):
    crashing, models_a = gateway(fail_on_call=3)             # dies while writing the third section
    with pytest.raises(RuntimeError, match="crashed"):
        run_report(TICKER, None, run_id="rep-crash-1", llm=crashing, checkpointer=checkpointer)
    assert len(models_a["narrative"].seen) == 3

    fresh, models_b = gateway()                              # a new process: new gateway, no in-memory state
    out = resume_report("rep-crash-1", None, checkpointer, llm=fresh, retry=True)
    assert out["eval"]["ok"] and out["format"]["ok"] and out["pdf_name"].endswith(".pdf")
    assert len(models_b["narrative"].seen) == NARRATIVE_CALLS - 2          # the two finished sections were not paid for again
    assert [s for s in out["stats"]] and set(out["stats"]) >= {"Company Overview", "Thesis and Drivers", "Investment Update"}


def test_resuming_a_run_with_no_checkpoint_is_a_clear_error(company, checkpointer):
    with pytest.raises(LookupError, match="no checkpoint"):
        resume_report("never-started", None, checkpointer)


def test_review_needs_a_checkpointer_and_an_ingested_company(company):
    with pytest.raises(ValueError, match="checkpointer"):
        run_report(TICKER, review=True)
    with pytest.raises(LookupError, match="not ingested"):
        run_report("NOPE", checkpointer=memory_checkpointer())
