from finrobot.agents.sections.writers import BRIEFS
from finrobot.report.model import SECTIONS
from finrobot.report.render import clean_prose, format_check, prose_html


def good_html():
    return "<html><body>" + "".join(f"<h2><span>1</span>{s}</h2><p>text</p>" for s in SECTIONS) + "</body></html>"


def test_every_prose_section_has_a_brief_and_the_table_driven_one_is_separate():
    assert set(SECTIONS) - set(BRIEFS) == {"Risks and Mitigations"}


def test_format_check_passes_a_complete_document():
    assert format_check(good_html(), b"%PDF-1.7 ...", 8)["ok"]


def test_format_check_catches_junk_missing_sections_and_bad_pdf():
    bad = good_html().replace(">Valuation<", ">Valuatio<") + "<p>growth was NaN and [S#] {{ x }}</p>"
    out = format_check(bad, b"nope", 1)
    text = " ".join(out["issues"])
    assert not out["ok"] and "missing section: Valuation" in text and "junk" in text and "not a PDF" in text and "page count" in text


def test_the_risk_table_phrase_none_disclosed_is_not_junk():
    assert format_check(good_html() + "<p>None disclosed in the retrieved filings</p>", b"%PDF", 5)["ok"]


def test_rules_do_not_turn_the_previous_line_into_a_heading():
    """Regression: a model's '---' under a bullet rendered it as a giant heading."""
    assert "<h" not in prose_html("- The margin rose [F1]\n---\nMore text")
    assert "#" not in clean_prose("## Heading\nbody")


def test_citations_become_superscripts_in_one_group():
    assert "<sup class='cite'>F1, S2</sup>" in prose_html("Revenue was up [F1, S2].")
