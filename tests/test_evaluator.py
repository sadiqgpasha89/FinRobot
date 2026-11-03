from finrobot.agents.evaluator import cites_in, evaluate, extract_claims
from finrobot.tools.factbook import FactBook


def book():
    fb = FactBook()
    fb.add_fact("Q2 EBITDA", 1.622e9, "USD", "x")            # F1
    fb.add_fact("consensus", 1.64e9, "USD", "x")             # F2
    fb.add_fact("margin", 24.2, "%", "x")                    # F3
    fb.add_fact("margin change", -50.0, "bps", "x")          # F4
    fb.add_fact("leverage", 2.43, "x", "x")                  # F5
    fb.add_fact("EPS", 1.84, "USD/shares", "x")              # F6
    fb.add_passage(1, "8-K", "Risk management impact of 75 basis points; recycled commodity prices cost $40 million.")  # S1
    return fb


def test_matching_numbers_in_many_formats_pass():
    text = ("- Adjusted EBITDA was $1.622 billion [F1] versus $1.64 billion consensus [F2].\n"
            "- Margin was 24.2% [F3], down 50 basis points [F4]; leverage 2.43x [F5]; EPS of $1.84 [F6].\n"
            "- Also $1,622 million [F1] and $1.6 billion [F1].")
    rep = evaluate(text, book())
    assert rep.ok, rep.summary()
    assert rep.claims == 8 and rep.matched_facts == 8


def test_unsupported_number_is_a_hard_issue():
    rep = evaluate("- EBITDA was $1.9 billion [F1].", book())
    assert not rep.ok and rep.hard[0].kind == "unsupported"


def test_uncited_figures_are_a_hard_issue():
    rep = evaluate("- EBITDA was $1.622 billion.", book())
    assert not rep.ok and rep.hard[0].kind == "uncited"


def test_nonexistent_citation_is_a_hard_issue():
    rep = evaluate("- EBITDA was $1.622 billion [F99].", book())
    assert not rep.ok and any(i.kind == "bad_cite" for i in rep.hard)


def test_number_from_cited_passage_is_grounded():
    rep = evaluate("- Risk management cost 75 basis points [S1] and commodity prices cost $40 million [S1].", book())
    assert rep.ok and rep.grounded_passages == 2


def test_passage_number_without_citing_it_fails():
    assert not evaluate("- Commodity prices cost $40 million [F1].", book()).ok


def test_miscitation_only_warns():
    rep = evaluate("- Margin was 24.2% [F1].", book())
    assert rep.ok and rep.issues[0].level == "warn"


def test_bare_numbers_years_and_labels_are_not_claims():
    assert extract_claims("In Q2 2026 (FY2026 Q3), 10-Q item 1A listed 3 risks across 12 pages on June 30, 2026.") == []


def test_headings_are_ignored_and_citation_parsing():
    assert evaluate("## Revenue was $5 billion\n- fine text", book()).ok
    assert cites_in("x [F1, F2] y [S3][F4]") == ["F1", "F2", "S3", "F4"]


def test_sign_is_ignored_for_declines():
    assert evaluate("- Margin declined 50 basis points [F4].", book()).ok


def test_percentage_points_are_checked_not_ignored():
    fb = FactBook()
    fb.add_fact("margin change", 3.4, "pp", "x")
    assert evaluate("- Margin rose 3.4 percentage points [F1].", fb).ok
    assert evaluate("- Margin rose 340 basis points [F1].", fb).ok
    assert not evaluate("- Margin rose 3 basis points [F1].", fb).ok      # the mislabelled-unit bug
    assert not evaluate("- Margin rose 9.9 percentage points [F1].", fb).ok


def test_placeholder_citations_fail():
    rep = evaluate("- EBITDA was $1.622 billion [F1] and grew [S#].", book())
    assert not rep.ok and any("placeholder" in i.detail for i in rep.hard)


def period_book():
    fb = FactBook()
    fb.add_fact("WM revenue FY2026 Q3", 6846e6, "USD", "x")            # F1
    fb.add_fact("WM revenue FY2025 Q3", 6443e6, "USD", "x")            # F2
    fb.add_fact("WM revenue growth YoY FY2026 Q3", 6.3, "%", "x", derived=True, inputs=["F1", "F2"])  # F3
    return fb


def test_wrong_period_label_is_a_hard_issue():
    """Regression: a model wrote 'FY2024 Q3' next to the FY2026 Q3 figure."""
    rep = evaluate("- Revenue FY2024 Q3: $6.85 billion [F1].", period_book())
    assert not rep.ok and rep.hard[0].kind == "wrong_period"


def test_correct_periods_pass_including_inputs_of_derived_facts():
    assert evaluate("- Revenue FY2026 Q3: $6.85 billion [F1].", period_book()).ok
    assert evaluate("- Revenue grew 6.3% in FY2026 Q3 from FY2025 Q3 [F3].", period_book()).ok   # FY2025 Q3 is an input of F3
    assert evaluate("- FY2026 revenue was $6.85 billion [F1].", period_book()).ok                  # year-only mention
