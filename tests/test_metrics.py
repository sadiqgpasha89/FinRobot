from datetime import date

import pytest

from finrobot.tools.factbook import FactBook
from finrobot.tools.metrics import Metrics, MissingData, period_label, shift


class FakeSource:
    """Quarterly facts keyed by (metric, fy, fq)."""

    def __init__(self, data: dict):
        self.data = data

    def get_fact(self, ticker, metric, fy, fq):
        v = self.data.get((metric, fy, fq))
        if v is None:
            return None
        return {"fiscal_year": fy, "fiscal_q": fq, "value": v, "unit": "USD", "source_id": f"XBRL:{metric}:{fy}Q{fq}:acc:reported",
                "derived": False, "period_end": date(fy, fq * 3, 28)}

    def get_facts(self, ticker, metric, quarterly=True, limit=12, up_to=None):
        rows = [(k, v) for k, v in self.data.items() if k[0] == metric]
        rows.sort(key=lambda kv: (kv[0][1], kv[0][2]), reverse=True)
        return [self.get_fact(ticker, m, fy, fq) for (m, fy, fq), _ in rows[:limit]]

    def latest_quarter(self, ticker):
        return (2026, 3)


def make(extra=None):
    d = {}
    for i, (fy, fq) in enumerate([(2025, 1), (2025, 2), (2025, 3), (2025, 4), (2026, 1), (2026, 2), (2026, 3)]):
        d[("revenue", fy, fq)] = 100.0 + 10 * i
        d[("operating_income", fy, fq)] = 20.0 + i
        d[("d_and_a", fy, fq)] = 10.0
        d[("op_cash_flow", fy, fq)] = 30.0
        d[("capex", fy, fq)] = 12.0
        d[("net_income", fy, fq)] = 15.0
        d[("cash", fy, fq)] = 50.0
        d[("debt_noncurrent", fy, fq)] = 400.0
        d[("debt_current", fy, fq)] = 20.0
        d[("net_interest", fy, fq)] = -5.0
    d.update(extra or {})
    return Metrics("T", FactBook(), FakeSource(d))


def test_shift_and_labels():
    assert shift(2026, 1, -1) == (2025, 4) and shift(2026, 3, -4) == (2025, 3) and shift(2025, 4, 1) == (2026, 1)
    assert period_label(2026, 3) == "FY2026 Q3" and period_label(2026, 0) == "FY2026"


def test_growth_margin_ebitda_and_provenance():
    m = make()
    g = m.compute("revenue_growth_yoy", 2026, 3)          # 160 vs 120
    assert g.value == pytest.approx(33.333, abs=0.01) and g.unit == "%" and len(g.inputs) == 2
    e = m.compute("ebitda", 2026, 3)
    assert e.value == 26.0 + 10.0 and e.derived
    assert m.compute("ebitda_margin", 2026, 3).value == pytest.approx(36 / 160 * 100)
    # every derived fact points at registered inputs
    for f in m.fb.facts.values():
        assert all(i in m.fb.facts for i in f.inputs)


def test_margin_change_is_in_basis_points():
    m = make()
    bps = m.compute("operating_margin_change_yoy", 2026, 3)
    assert bps.unit == "bps" and bps.value == pytest.approx((26 / 160 - 22 / 120) * 10000)


def test_ttm_net_debt_and_leverage():
    m = make()
    assert m.compute("ttm_revenue", 2026, 3).value == 130 + 140 + 150 + 160
    assert m.compute("net_debt", 2026, 3).value == 400 + 20 - 50
    lev = m.compute("net_debt_to_ttm_ebitda", 2026, 3)
    assert lev.unit == "x" and lev.value == pytest.approx(370 / sum(m.compute("ebitda", *shift(2026, 3, -i)).value for i in range(4)))


def test_interest_coverage_falls_back_to_net_interest():
    cov = make().compute("interest_coverage_ttm", 2026, 3)
    assert cov.unit == "x" and cov.value > 0


def test_missing_data_raises_a_clear_error_instead_of_guessing():
    with pytest.raises(MissingData):
        make().compute("revenue_growth_yoy", 2025, 1)      # no 2024 data
    with pytest.raises(MissingData, match="unknown metric"):
        make().compute("vibes", 2026, 3)


def test_zero_denominator_becomes_missing_data():
    m = make({("revenue", 2026, 3): 0.0})
    with pytest.raises(MissingData):
        m.compute("operating_margin", 2026, 3)
