from datetime import date

import pytest

from finrobot.data.parse import chunk_sections, html_to_text, split_sections
from finrobot.data.providers.market import parse_alphavantage_daily, parse_price_csv

HTML = """<html><head><style>x{}</style></head><body>
<div>TABLE OF CONTENTS</div><div>Item 1. Business</div><div>Item 1A. Risk Factors</div><div>Item 7. Management's Discussion</div>
<div>Item 1. Business</div><p>%s</p>
<div>Item 1A. Risk Factors</div><p>%s</p>
<div>Item 7. Management's Discussion and Analysis</div><p>%s</p>
<table><tr><td>Revenue</td><td>$</td><td>6,846</td><td>6,443</td></tr></table>
<ix:header><ix:hidden>HIDDENFACT</ix:hidden></ix:header></body></html>""" % ("We collect waste. " * 40, "Risks exist. " * 60, "Results improved. " * 50)


def test_html_to_text_keeps_table_rows_and_drops_hidden_xbrl():
    t = html_to_text(HTML)
    assert "Revenue | 6,846 | 6,443" in t and "HIDDENFACT" not in t and "x{}" not in t


def test_sections_use_the_longest_span_so_the_table_of_contents_loses():
    secs = {s.name: s.text for s in split_sections(html_to_text(HTML), "10-K")}
    assert {"Business", "Risk Factors", "MD&A"} <= set(secs)
    assert "We collect waste" in secs["Business"] and "Risks exist" in secs["Risk Factors"]


def test_chunks_are_bounded_and_overlap():
    chunks = chunk_sections(split_sections(html_to_text(HTML), "10-K"), size=400, overlap=50)
    assert chunks and all(len(c.text) <= 400 * 1.5 for c in chunks)
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_price_csv_parsing():
    rows = parse_price_csv("Date,Open,Close,Volume\n2026-10-30,1,203.5,5\n2026-10-31,1,205.25,5\nbad,row,x\n")
    assert rows == [(date(2026, 10, 30), 203.5), (date(2026, 10, 31), 205.25)]
    with pytest.raises(ValueError):
        parse_price_csv("a,b\n1,2\n")


def test_alphavantage_parsing_and_error_surface():
    payload = {"Time Series (Daily)": {"2026-10-31": {"4. close": "205.25"}, "2026-10-30": {"4. close": "203.50"}}}
    assert parse_alphavantage_daily(payload) == [(date(2026, 10, 30), 203.5), (date(2026, 10, 31), 205.25)]
    with pytest.raises(ValueError, match="rate"):
        parse_alphavantage_daily({"Note": "API rate limit reached"})
