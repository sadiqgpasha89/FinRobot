import pytest

from finrobot.tools.valuation import Assumptions, dcf, project, sensitivity, YEARS


def base(**kw):
    d = dict(base_revenue=1000.0, base_ebitda=300.0, net_debt=500.0, shares=100.0, rev_growth=[0.05] * YEARS, ebitda_margin=[0.30] * YEARS,
             da_pct=0.10, capex_pct=0.10, tax_rate=0.25, wacc=0.08, terminal_growth=0.02)
    d.update(kw)
    return Assumptions(**d)


def test_projection_arithmetic_by_hand():
    r = project(base())[0]
    assert r["revenue"] == pytest.approx(1050) and r["ebitda"] == pytest.approx(315)
    ebit = 315 - 105                                   # D&A is 10% of revenue
    assert r["fcff"] == pytest.approx(ebit * 0.75 + 105 - 105)   # capex == D&A here, so FCFF = NOPAT


def test_dcf_matches_a_hand_computation():
    a = base()
    rows = project(a)
    pv = sum(r["fcff"] / 1.08 ** r["year"] for r in rows)
    tv = rows[-1]["fcff"] * 1.02 / (0.08 - 0.02) / 1.08 ** 5
    out = dcf(a)
    assert out["enterprise_value"] == pytest.approx(pv + tv)
    assert out["equity_value"] == pytest.approx(pv + tv - 500)
    assert out["per_share"] == pytest.approx((pv + tv - 500) / 100)
    assert out["tv_share_of_ev"] == pytest.approx(tv / (pv + tv) * 100)


def test_wacc_must_exceed_growth():
    with pytest.raises(ValueError):
        dcf(base(wacc=0.02, terminal_growth=0.02))


def test_sensitivity_is_monotonic():
    s = sensitivity(base())
    for row in s["grid"]:                       # higher terminal growth -> higher value
        assert row == sorted(row)
    for col in zip(*s["grid"]):                 # higher WACC -> lower value
        assert list(col) == sorted(col, reverse=True)
    assert s["grid"][2][2] == pytest.approx(dcf(base())["per_share"])


def test_negative_ebit_pays_no_tax():
    r = project(base(ebitda_margin=[0.05] * YEARS))[0]   # EBITDA 52.5 < D&A 105
    assert r["tax"] == 0 and r["ebit"] < 0
