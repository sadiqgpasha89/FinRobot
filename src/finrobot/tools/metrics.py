"""Financial metrics. All arithmetic lives here (never in the LLM), and every result is registered
in the FactBook together with the ids of the facts it was computed from."""
from __future__ import annotations

from typing import Callable, Protocol

from finrobot.data.xbrl import CONCEPTS
from finrobot.tools.factbook import Fact, FactBook


class MissingData(LookupError):
    pass


class FactSource(Protocol):
    def get_fact(self, ticker: str, metric: str, fiscal_year: int, fiscal_q: int) -> dict | None: ...
    def get_facts(self, ticker: str, metric: str, quarterly: bool = True, limit: int = 12, up_to=None) -> list[dict]: ...
    def latest_quarter(self, ticker: str) -> tuple[int, int] | None: ...


def shift(fy: int, fq: int, n: int) -> tuple[int, int]:
    """Move n quarters (negative = back)."""
    idx = fy * 4 + (fq - 1) + n
    return idx // 4, idx % 4 + 1


def period_label(fy: int, fq: int) -> str:
    return f"FY{fy}" if fq == 0 else f"FY{fy} Q{fq}"


def _basis(metric: str, fq: int) -> str:
    if CONCEPTS.get(metric, ("flow",))[0] == "stock":
        return "instant"
    return "fy" if fq == 0 else "q"


def pct_change(cur: float, prior: float) -> float:
    if prior == 0:
        raise ZeroDivisionError("prior value is zero")
    return (cur / abs(prior) - 1) * 100 if prior < 0 else (cur / prior - 1) * 100


def safe_div(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("denominator is zero")
    return a / b


METRIC_NAMES = [
    "revenue_growth_yoy", "revenue_growth_qoq", "gross_margin", "operating_margin", "net_margin",
    "ebitda", "ebitda_margin", "ebitda_growth_yoy", "ebitda_margin_change_yoy", "operating_margin_change_yoy",
    "gross_margin_change_yoy", "net_margin_change_yoy",
    "fcf", "fcf_margin", "net_debt", "ttm_revenue", "ttm_ebitda", "ttm_fcf", "net_debt_to_ttm_ebitda",
    "interest_coverage_ttm", "eps_growth_yoy", "capex_pct_revenue", "da_pct_revenue",
]


class Metrics:
    def __init__(self, ticker: str, fb: FactBook, src: FactSource):
        self.ticker, self.fb, self.src = ticker, fb, src

    # --- raw facts -----------------------------------------------------------------------
    def fact(self, metric: str, fy: int, fq: int) -> Fact:
        row = self.src.get_fact(self.ticker, metric, fy, fq)
        if row is None:
            raise MissingData(f"no '{metric}' for {period_label(fy, fq)}")
        return self.fb.add_fact(f"{self.ticker} {metric} {period_label(row['fiscal_year'], fq)}", row["value"], row["unit"],
                                row["source_id"], derived=bool(row["derived"]), basis=_basis(metric, fq))

    def _opt(self, metric: str, fy: int, fq: int) -> Fact | None:
        try:
            return self.fact(metric, fy, fq)
        except MissingData:
            return None

    def series(self, metric: str, quarters: int = 8, quarterly: bool = True, up_to=None) -> list[Fact]:
        rows = self.src.get_facts(self.ticker, metric, quarterly=quarterly, limit=quarters, up_to=up_to)
        out = []
        for r in rows:
            q = r["fiscal_q"] if quarterly else 0
            out.append(self.fb.add_fact(f"{self.ticker} {metric} {period_label(r['fiscal_year'], q)}", r["value"], r["unit"],
                                        r["source_id"], derived=bool(r["derived"]), basis=_basis(metric, q)))
        return out

    def ttm(self, metric: str, fy: int, fq: int) -> Fact:
        parts = [self.fact(metric, *shift(fy, fq, -i)) for i in range(4)]
        return self._derive(f"{self.ticker} TTM {metric} to {period_label(fy, fq)}", sum(p.value for p in parts), parts[0].unit, parts, basis="ttm")

    def _derive(self, label: str, value: float, unit: str, inputs: list[Fact], basis: str | None = None) -> Fact:
        if basis is None:   # shared basis of the inputs, else 'mixed'
            bases = {i.basis for i in inputs if i.basis}
            basis = bases.pop() if len(bases) == 1 else ("mixed" if bases else "")
        return self.fb.add_fact(label, value, unit, "derived: " + ", ".join(i.id for i in inputs), derived=True,
                                inputs=[i.id for i in inputs], basis=basis)

    # --- computed metrics ----------------------------------------------------------------
    def compute(self, name: str, fy: int, fq: int) -> Fact:
        fn: Callable | None = getattr(self, f"_m_{name}", None)
        if fn is None:
            raise MissingData(f"unknown metric '{name}'. Available: {', '.join(METRIC_NAMES)}")
        try:
            return fn(fy, fq)
        except ZeroDivisionError as e:
            raise MissingData(f"{name} {period_label(fy, fq)}: {e}") from e

    def _ratio(self, label: str, num: Fact, den: Fact, unit: str = "%", scale: float = 100) -> Fact:
        return self._derive(label, safe_div(num.value, den.value) * scale, unit, [num, den])

    def _m_revenue_growth_yoy(self, fy, fq):
        cur, prior = self.fact("revenue", fy, fq), self.fact("revenue", *shift(fy, fq, -4))
        return self._derive(f"{self.ticker} revenue growth YoY {period_label(fy, fq)}", pct_change(cur.value, prior.value), "%", [cur, prior])

    def _m_revenue_growth_qoq(self, fy, fq):
        cur, prior = self.fact("revenue", fy, fq), self.fact("revenue", *shift(fy, fq, -1))
        return self._derive(f"{self.ticker} revenue growth QoQ {period_label(fy, fq)}", pct_change(cur.value, prior.value), "%", [cur, prior])

    def _m_gross_margin(self, fy, fq):
        rev = self.fact("revenue", fy, fq)
        gp = self._opt("gross_profit", fy, fq)
        if gp is None:
            cost = self.fact("cost_of_revenue", fy, fq)
            return self._derive(f"{self.ticker} gross margin {period_label(fy, fq)}", (1 - safe_div(cost.value, rev.value)) * 100, "%", [rev, cost])
        return self._ratio(f"{self.ticker} gross margin {period_label(fy, fq)}", gp, rev)

    def _m_operating_margin(self, fy, fq):
        return self._ratio(f"{self.ticker} operating margin {period_label(fy, fq)}", self.fact("operating_income", fy, fq), self.fact("revenue", fy, fq))

    def _m_net_margin(self, fy, fq):
        return self._ratio(f"{self.ticker} net margin {period_label(fy, fq)}", self.fact("net_income", fy, fq), self.fact("revenue", fy, fq))

    def _m_ebitda(self, fy, fq):
        oi, da = self.fact("operating_income", fy, fq), self.fact("d_and_a", fy, fq)
        return self._derive(f"{self.ticker} EBITDA (operating income + D&A) {period_label(fy, fq)}", oi.value + da.value, "USD", [oi, da])

    def _m_ebitda_margin(self, fy, fq):
        return self._ratio(f"{self.ticker} EBITDA margin {period_label(fy, fq)}", self._m_ebitda(fy, fq), self.fact("revenue", fy, fq))

    def _m_ebitda_growth_yoy(self, fy, fq):
        cur, prior = self._m_ebitda(fy, fq), self._m_ebitda(*shift(fy, fq, -4))
        return self._derive(f"{self.ticker} EBITDA growth YoY {period_label(fy, fq)}", pct_change(cur.value, prior.value), "%", [cur, prior])

    def _m_ebitda_margin_change_yoy(self, fy, fq):
        cur, prior = self._m_ebitda_margin(fy, fq), self._m_ebitda_margin(*shift(fy, fq, -4))
        return self._derive(f"{self.ticker} EBITDA margin change YoY {period_label(fy, fq)}", (cur.value - prior.value) * 100, "bps", [cur, prior])

    def _margin_change(self, name: str, fn, fy, fq):
        cur, prior = fn(fy, fq), fn(*shift(fy, fq, -4))
        return self._derive(f"{self.ticker} {name} margin change YoY {period_label(fy, fq)}", (cur.value - prior.value) * 100, "bps", [cur, prior])

    def _m_operating_margin_change_yoy(self, fy, fq):
        return self._margin_change("operating", self._m_operating_margin, fy, fq)

    def _m_gross_margin_change_yoy(self, fy, fq):
        return self._margin_change("gross", self._m_gross_margin, fy, fq)

    def _m_net_margin_change_yoy(self, fy, fq):
        return self._margin_change("net", self._m_net_margin, fy, fq)

    def _m_fcf(self, fy, fq):
        ocf, capex = self.fact("op_cash_flow", fy, fq), self.fact("capex", fy, fq)
        return self._derive(f"{self.ticker} free cash flow (operating cash flow - capex) {period_label(fy, fq)}", ocf.value - capex.value, "USD", [ocf, capex])

    def _m_fcf_margin(self, fy, fq):
        return self._ratio(f"{self.ticker} FCF margin {period_label(fy, fq)}", self._m_fcf(fy, fq), self.fact("revenue", fy, fq))

    def _m_net_debt(self, fy, fq):
        nc, cur, cash = self.fact("debt_noncurrent", fy, fq), self._opt("debt_current", fy, fq), self.fact("cash", fy, fq)
        parts = [p for p in (nc, cur, cash) if p is not None]
        return self._derive(f"{self.ticker} net debt {period_label(fy, fq)}", nc.value + (cur.value if cur else 0) - cash.value, "USD", parts)

    def _m_ttm_revenue(self, fy, fq):
        return self.ttm("revenue", fy, fq)

    def _m_ttm_fcf(self, fy, fq):
        parts = [self._m_fcf(*shift(fy, fq, -i)) for i in range(4)]
        return self._derive(f"{self.ticker} TTM free cash flow to {period_label(fy, fq)}", sum(p.value for p in parts), "USD", parts, basis="ttm")

    def _m_ttm_ebitda(self, fy, fq):
        parts = [self._m_ebitda(*shift(fy, fq, -i)) for i in range(4)]
        return self._derive(f"{self.ticker} TTM EBITDA to {period_label(fy, fq)}", sum(p.value for p in parts), "USD", parts, basis="ttm")

    def _m_net_debt_to_ttm_ebitda(self, fy, fq):
        return self._ratio(f"{self.ticker} net debt / TTM EBITDA {period_label(fy, fq)}", self._m_net_debt(fy, fq),
                           self._m_ttm_ebitda(fy, fq), unit="x", scale=1)

    def _m_interest_coverage_ttm(self, fy, fq):
        ebit = self.ttm("operating_income", fy, fq)
        try:
            interest = self.ttm("interest_expense", fy, fq)
            denom, parts = interest.value, [ebit, interest]
        except MissingData:
            net = self.ttm("net_interest", fy, fq)  # negative = net expense
            denom, parts = -net.value, [ebit, net]
        if denom <= 0:
            raise MissingData("no net interest expense to cover")
        return self._derive(f"{self.ticker} interest coverage (TTM EBIT / interest) {period_label(fy, fq)}", ebit.value / denom, "x", parts)

    def _m_eps_growth_yoy(self, fy, fq):
        cur, prior = self.fact("eps_diluted", fy, fq), self.fact("eps_diluted", *shift(fy, fq, -4))
        return self._derive(f"{self.ticker} diluted EPS growth YoY {period_label(fy, fq)}", pct_change(cur.value, prior.value), "%", [cur, prior])

    def _m_capex_pct_revenue(self, fy, fq):
        return self._ratio(f"{self.ticker} capex % of revenue {period_label(fy, fq)}", self.fact("capex", fy, fq), self.fact("revenue", fy, fq))

    def _m_da_pct_revenue(self, fy, fq):
        return self._ratio(f"{self.ticker} D&A % of revenue {period_label(fy, fq)}", self.fact("d_and_a", fy, fq), self.fact("revenue", fy, fq))
