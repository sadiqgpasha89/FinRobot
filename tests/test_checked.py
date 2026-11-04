from datetime import date

from langchain_core.messages import AIMessage

from fakes import make_gateway
from finrobot.agents.checked import NO_ANSWER, checked
from finrobot.agents.context import RunContext

GOOD = "- EBITDA was $1.50 billion [F1]."
BAD = "- EBITDA was $2.10 billion [F1]."          # a real fact cited, but the number is not that fact's


def setup(figures, label="T ebitda FY2026 Q3"):
    gw, models = make_gateway(figures=figures)
    events = []
    ctx = RunContext("T", "TestCo", gw, fy=2026, fq=3, period_end=date(2026, 9, 30), emit=events.append)
    ctx.fb.add_fact(label, 1.5e9, "USD", "src")   # F1
    return ctx, events, models["figures"]


def run(ctx, **kw):
    return checked(ctx, "figures", "system", "question", [], "Data-CoT", max_steps=1, **kw)


def test_a_verified_draft_passes_through_untouched():
    ctx, events, _ = setup([GOOD])
    out = run(ctx, must_cover_focus=True)
    assert out["text"] == GOOD and not out["retried"] and out["removed"] == 0 and out["eval"]["ok"]
    assert [e["type"] for e in events] == ["agent_start", "agent_done"]


def test_agent_done_reports_the_wall_time_of_the_whole_agent():
    """Regression: the clock started after the first draft, so every agent reported 0.0 s."""
    import time
    ctx, events, _ = setup([lambda messages: (time.sleep(0.2), AIMessage(GOOD))[1]])
    run(ctx)
    assert next(e for e in events if e["type"] == "agent_done")["seconds"] >= 0.2


def test_a_failed_draft_is_retried_once_with_the_issues_in_the_prompt():
    ctx, events, model = setup([BAD, GOOD])
    out = run(ctx)
    assert out["text"] == GOOD and out["retried"] and out["eval"]["ok"]
    retry_prompt = model.seen[1]["messages"][1].content   # [system, user + feedback, last-step nudge]
    assert "failed verification" in retry_prompt and "$2.10 billion" in retry_prompt and BAD in retry_prompt
    assert [e["type"] for e in events] == ["agent_start", "retry", "agent_done"]


def test_unverifiable_lines_are_removed_after_the_retry_never_softened():
    two_lines = BAD + "\n- Management sounded confident."
    ctx, _, _ = setup([two_lines, two_lines])
    out = run(ctx)
    assert out["text"] == "- Management sounded confident." and out["removed"] == 1 and out["retried"]


def test_real_figures_for_the_wrong_period_are_rejected_not_published():
    """Regression: 'FY2024 Q3: $2.07 billion' was a real figure for the wrong quarter, and the numeric check alone accepted it."""
    ctx, events, _ = setup([GOOD, GOOD], label="T ebitda FY2025 Q3")
    out = run(ctx, must_cover_focus=True)
    assert out["text"] == NO_ANSWER and out.get("wrong_period") and out["retried"]
    assert any(e["type"] == "retry" and any("focus period FY2026 Q3" in i for i in e["issues"]) for e in events)


def test_a_wrong_period_draft_can_be_repaired_by_the_retry():
    fixed = "- EBITDA was $1.50 billion [F2]."
    ctx, _, _ = setup([GOOD, fixed], label="T ebitda FY2025 Q3")
    ctx.fb.add_fact("T ebitda FY2026 Q3", 1.5e9, "USD", "src2")   # F2, the right period
    out = run(ctx, must_cover_focus=True)
    assert out["text"] == fixed and out["retried"] and "wrong_period" not in out


def test_an_empty_answer_is_not_retried_and_reports_no_verified_answer():
    ctx, events, model = setup([""])
    out = run(ctx)
    assert out["text"] == NO_ANSWER and not out["retried"] and len(model.seen) == 1
