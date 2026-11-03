from datetime import date

from finrobot.data.xbrl import fiscal_position, normalise


def entry(start, end, val, form="10-Q", filed="2025-08-01", accn="0001-25-000001"):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "accn": accn}


def facts(tag, entries, unit="USD"):
    return {"facts": {"us-gaap": {tag: {"units": {unit: entries}}}}}


def by_q(rows, metric):
    return {(r.fiscal_year, r.fiscal_q): r for r in rows if r.metric == metric}


def test_fiscal_position_calendar_year():
    anchor = (12, 31)
    assert fiscal_position(date(2025, 3, 31), anchor) == (2025, 1)
    assert fiscal_position(date(2025, 6, 30), anchor) == (2025, 2)
    assert fiscal_position(date(2025, 9, 30), anchor) == (2025, 3)
    assert fiscal_position(date(2025, 12, 31), anchor) == (2025, 4)


def test_fiscal_position_non_calendar_and_52_week_jitter():
    assert fiscal_position(date(2025, 12, 27), (9, 30)) == (2026, 1)   # Oct-Dec quarter of a Sept FY
    assert fiscal_position(date(2025, 6, 28), (12, 28)) == (2025, 2)   # 52/53-week year ending late December
    assert fiscal_position(date(2026, 1, 3), (12, 28)) == (2025, 4)


def test_q4_is_full_year_minus_nine_months_and_marked_derived():
    e = [entry("2025-01-01", "2025-03-31", 100), entry("2025-04-01", "2025-06-30", 110),
         entry("2025-01-01", "2025-06-30", 210), entry("2025-07-01", "2025-09-30", 120),
         entry("2025-01-01", "2025-09-30", 330), entry("2025-01-01", "2025-12-31", 470, form="10-K", filed="2026-02-01")]
    q = by_q(normalise(facts("Revenues", e)), "revenue")
    assert [q[(2025, i)].value for i in (1, 2, 3, 4)] == [100, 110, 120, 140]
    assert q[(2025, 4)].derived and not q[(2025, 2)].derived
    assert q[(2025, 0)].value == 470 and q[(2025, 0)].fiscal_q == 0


def test_cash_flow_quarters_come_from_cumulative_differences():
    e = [entry("2025-01-01", "2025-03-31", 50), entry("2025-01-01", "2025-06-30", 130),
         entry("2025-01-01", "2025-09-30", 240), entry("2025-01-01", "2025-12-31", 400, form="10-K", filed="2026-02-01")]
    q = by_q(normalise(facts("NetCashProvidedByUsedInOperatingActivities", e)), "op_cash_flow")
    assert [q[(2025, i)].value for i in (1, 2, 3, 4)] == [50, 80, 110, 160]
    assert q[(2025, 2)].derived


def test_restated_value_wins_and_tag_priority():
    e = [entry("2025-01-01", "2025-03-31", 100, filed="2025-05-01"), entry("2025-01-01", "2025-03-31", 105, filed="2026-05-01"),
         entry("2025-01-01", "2025-12-31", 400, form="10-K", filed="2026-02-01")]
    q = by_q(normalise(facts("Revenues", e)), "revenue")
    assert q[(2025, 1)].value == 105


def test_stock_items_use_instant_entries():
    e = [{"end": "2025-06-30", "val": 900, "form": "10-Q", "filed": "2025-08-01", "accn": "a"},
         {"end": "2025-12-31", "val": 950, "form": "10-K", "filed": "2026-02-01", "accn": "b"}]
    rows = normalise({"facts": {"us-gaap": {"Assets": {"units": {"USD": e}}, "Revenues": {"units": {"USD": [
        entry("2025-01-01", "2025-12-31", 1, form="10-K")]}}}}})
    q = by_q(rows, "total_assets")
    assert q[(2025, 2)].value == 900 and q[(2025, 4)].value == 950


def test_shares_are_never_differenced():
    e = [entry("2025-01-01", "2025-06-30", 250), entry("2025-01-01", "2025-09-30", 249),
         entry("2025-01-01", "2025-12-31", 248, form="10-K"), entry("2025-01-01", "2025-03-31", 251)]
    q = by_q(normalise(facts("WeightedAverageNumberOfDilutedSharesOutstanding", e, "shares")), "shares_diluted")
    assert (2025, 2) not in q and (2025, 4) not in q and q[(2025, 1)].value == 251
