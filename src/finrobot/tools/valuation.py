"""Driver-based projections and DCF. Pure Python: no LLM involved, every assumption is explicit and editable."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from finrobot.tools.metrics import Metrics, MissingData, shift

YEARS = 5


@dataclass
class Assumptions:
    base_revenue: float            # TTM revenue, USD
    base_ebitda: float             # TTM EBITDA, USD
    net_debt: float                # latest, USD
    shares: float                  # diluted shares, latest quarter
    rev_growth: list[float]        # decimal per projected year, e.g. 0.05
    ebitda_margin: list[float]     # decimal per projected year
    da_pct: float = 0.10           # D&A % of revenue
    capex_pct: float = 0.11        # capex % of revenue
    tax_rate: float = 0.24
    nwc_pct_of_delta_rev: float = 0.0
    wacc: float = 0.08
    terminal_growth: float = 0.025
    basis: dict = field(default_factory=dict)   # where each default came from (for the report)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Assumptions":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _fade(start: float, end: float, n: int = YEARS) -> list[float]:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def default_assumptions(m: Metrics, fy: int, fq: int) -> Assumptions:
    """History-derived defaults. Nothing here is a forecast opinion: growth starts at the trailing rate (clipped)
    and fades toward a long-run rate, margins hold at the trailing level, capex and D&A at trailing ratios."""
    ttm_rev, prior_ttm_rev = m.ttm("revenue", fy, fq), m.ttm("revenue", *shift(fy, fq, -4))
    ttm_ebitda = m._m_ttm_ebitda(fy, fq)
    ttm_capex, ttm_da = m.ttm("capex", fy, fq), m.ttm("d_and_a", fy, fq)
    ttm_tax, ttm_pretax = None, None
    try:
        ttm_tax, ttm_pretax = m.ttm("income_tax", fy, fq), m.ttm("pretax_income", fy, fq)
    except MissingData:
        pass
    growth = ttm_rev.value / prior_ttm_rev.value - 1
    g1 = _clip(growth, -0.03, 0.12)
    margin = ttm_ebitda.value / ttm_rev.value
    tax = _clip(ttm_tax.value / ttm_pretax.value, 0.15, 0.30) if ttm_tax and ttm_pretax and ttm_pretax.value > 0 else 0.24
    shares = m.fact("shares_diluted", fy, fq).value
    nd = m._m_net_debt(fy, fq).value
    return Assumptions(
        base_revenue=ttm_rev.value, base_ebitda=ttm_ebitda.value, net_debt=nd, shares=shares,
        rev_growth=_fade(g1, min(g1, 0.035)), ebitda_margin=[margin] * YEARS,
        da_pct=ttm_da.value / ttm_rev.value, capex_pct=ttm_capex.value / ttm_rev.value, tax_rate=tax,
        basis={"rev_growth": f"trailing TTM growth {growth * 100:.1f}% (clipped to -3%..12%), fading to {min(g1, 0.035) * 100:.1f}%",
               "ebitda_margin": f"held at TTM margin {margin * 100:.1f}%",
               "da_pct": "TTM D&A / revenue", "capex_pct": "TTM capex / revenue",
               "tax_rate": "TTM effective rate, clipped to 15%-30%", "wacc": "default 8.0%, not derived", "terminal_growth": "default 2.5%"})


def project(a: Assumptions) -> list[dict]:
    """Annual rows for years 1..5 after the TTM base."""
    rows, rev = [], a.base_revenue
    for i in range(YEARS):
        prev = rev
        rev = prev * (1 + a.rev_growth[i])
        ebitda = rev * a.ebitda_margin[i]
        da = rev * a.da_pct
        ebit = ebitda - da
        tax = max(ebit, 0) * a.tax_rate
        capex = rev * a.capex_pct
        d_nwc = (rev - prev) * a.nwc_pct_of_delta_rev
        fcff = ebit - tax + da - capex - d_nwc
        rows.append({"year": i + 1, "revenue": rev, "growth": a.rev_growth[i] * 100, "ebitda": ebitda, "ebitda_margin": a.ebitda_margin[i] * 100,
                     "da": da, "ebit": ebit, "tax": tax, "capex": capex, "d_nwc": d_nwc, "fcff": fcff})
    return rows


def dcf(a: Assumptions, wacc: float | None = None, g: float | None = None) -> dict:
    wacc, g = a.wacc if wacc is None else wacc, a.terminal_growth if g is None else g
    if wacc <= g:
        raise ValueError("WACC must exceed terminal growth")
    rows = project(a)
    pv = sum(r["fcff"] / (1 + wacc) ** r["year"] for r in rows)
    tv = rows[-1]["fcff"] * (1 + g) / (wacc - g)
    pv_tv = tv / (1 + wacc) ** YEARS
    ev = pv + pv_tv
    equity = ev - a.net_debt
    return {"pv_fcff": pv, "terminal_value": tv, "pv_terminal": pv_tv, "enterprise_value": ev, "equity_value": equity,
            "per_share": equity / a.shares, "tv_share_of_ev": pv_tv / ev * 100 if ev else float("nan"),
            "implied_ev_ebitda": ev / a.base_ebitda if a.base_ebitda else float("nan"), "wacc": wacc, "g": g}


def sensitivity(a: Assumptions, waccs: list[float] | None = None, gs: list[float] | None = None) -> dict:
    waccs = waccs or [a.wacc + d for d in (-0.01, -0.005, 0, 0.005, 0.01)]
    gs = gs or [a.terminal_growth + d for d in (-0.01, -0.005, 0, 0.005, 0.01)]
    grid = [[dcf(a, w, g)["per_share"] if w > g else None for g in gs] for w in waccs]
    return {"waccs": waccs, "gs": gs, "grid": grid}


def comps_row(ticker: str, m: Metrics, fy: int, fq: int, price: float | None) -> dict:
    """Operating profile of a peer from its own XBRL facts; multiples only when a price is on file."""
    rev, prior = m.ttm("revenue", fy, fq), m.ttm("revenue", *shift(fy, fq, -4))
    ebitda = m._m_ttm_ebitda(fy, fq)
    row = {"ticker": ticker, "revenue": rev.value, "growth": (rev.value / prior.value - 1) * 100,
           "ebitda_margin": ebitda.value / rev.value * 100, "net_debt_ebitda": None, "ev_ebitda": None, "pe": None,
           "fact_ids": [rev.id, ebitda.id]}
    try:
        nd = m._m_net_debt(fy, fq)
        row["net_debt_ebitda"] = nd.value / ebitda.value
        if price:
            shares = m.fact("shares_diluted", fy, fq).value
            row["ev_ebitda"] = (price * shares + nd.value) / ebitda.value
            ni = m.ttm("net_income", fy, fq).value
            row["pe"] = price * shares / ni if ni > 0 else None
    except MissingData:
        pass
    return row
