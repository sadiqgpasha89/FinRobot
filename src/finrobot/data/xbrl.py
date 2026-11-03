"""Turn SEC XBRL 'company facts' into clean quarterly and annual rows.

XBRL reports flow items (revenue, cash flow) cumulatively inside a fiscal year: 3 months, 6 months,
9 months, 12 months. Discrete quarters are recovered by differencing, which also yields Q4 (FY - 9M)
and the cash-flow quarters that 10-Qs never state directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# metric -> (kind, unit, candidate us-gaap tags in priority order)
CONCEPTS: dict[str, tuple[str, str, list[str]]] = {
    "revenue": ("flow", "USD", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                                "RevenueFromContractWithCustomerIncludingAssessedTax"]),
    "cost_of_revenue": ("flow", "USD", ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]),
    "gross_profit": ("flow", "USD", ["GrossProfit"]),
    "operating_income": ("flow", "USD", ["OperatingIncomeLoss"]),
    "pretax_income": ("flow", "USD", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax": ("flow", "USD", ["IncomeTaxExpenseBenefit"]),
    "net_income": ("flow", "USD", ["NetIncomeLoss", "ProfitLoss"]),
    "interest_expense": ("flow", "USD", ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"]),
    # negative = net expense; used when a company reports no gross interest expense line
    "net_interest": ("flow", "USD", ["InterestIncomeExpenseNonoperatingNet", "InterestIncomeExpenseNet"]),
    "d_and_a": ("flow", "USD", ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                                "DepreciationAmortizationAndAccretionNet"]),
    "op_cash_flow": ("flow", "USD", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("flow", "USD", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
    "eps_diluted": ("flow", "USD/shares", ["EarningsPerShareDiluted"]),
    "shares_diluted": ("flow", "shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    "cash": ("stock", "USD", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "total_assets": ("stock", "USD", ["Assets"]),
    "equity": ("stock", "USD", ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "debt_noncurrent": ("stock", "USD", ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]),
    "debt_current": ("stock", "USD", ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]),
    "current_assets": ("stock", "USD", ["AssetsCurrent"]),
    "current_liabilities": ("stock", "USD", ["LiabilitiesCurrent"]),
}

QUARTER_DAYS = 91.3


@dataclass
class FactRow:
    metric: str
    period_end: date
    fiscal_year: int
    fiscal_q: int  # 1-4 discrete quarter (or quarter-end for stock items), 0 = full fiscal year flow
    value: float
    unit: str
    tag: str
    form: str
    filed: date
    accession: str
    derived: bool

    @property
    def source_id(self) -> str:
        how = "derived" if self.derived else "reported"
        return f"XBRL:{self.tag}:{self.period_end}:{self.accession}:{how}"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _entries(facts: dict, tags: list[str], unit: str) -> list[dict]:
    """All entries across candidate tags; for the same period the higher-priority tag wins."""
    seen: dict[tuple[str | None, str], dict] = {}
    for rank, tag in enumerate(tags):
        node = facts.get("us-gaap", {}).get(tag)
        if not node:
            continue
        for e in node.get("units", {}).get(unit, []):
            key = (e.get("start"), e["end"])
            cand = {**e, "tag": tag, "rank": rank}
            cur = seen.get(key)
            # prefer higher-priority tag, then the most recently filed (restated) value
            if cur is None or (cand["rank"], -_d(cand["filed"]).toordinal()) < (cur["rank"], -_d(cur["filed"]).toordinal()):
                seen[key] = cand
    return list(seen.values())


def _fy_anchor(entries: list[dict]) -> tuple[int, int]:
    """(month, day) on which the fiscal year ends, from the latest ~12-month entry."""
    annual = [e for e in entries if e.get("start") and 350 <= (_d(e["end"]) - _d(e["start"])).days <= 380]
    latest = max(annual, key=lambda e: e["end"]) if annual else max(entries, key=lambda e: e["end"])
    d = _d(latest["end"])
    return d.month, d.day


def fiscal_position(end: date, anchor: tuple[int, int]) -> tuple[int, int]:
    """(fiscal_year, quarter 1-4) for a period end date given the fiscal-year-end month/day."""
    m, d = anchor

    def year_end(y: int) -> date:
        try:
            return date(y, m, d)
        except ValueError:
            return date(y, m, 28)

    # the fiscal year this period closes in: the earliest year-end that is not (much) before `end`.
    # The 15-day slack covers 52/53-week years whose end date drifts around the anchor.
    fy_end = next(e for e in (year_end(end.year - 1), year_end(end.year), year_end(end.year + 1))
                  if e >= end - timedelta(days=15))
    months_left = round((fy_end - end).days / 30.44)
    q = 4 - round(months_left / 3)
    return fy_end.year, max(1, min(4, q))


def normalise(company_facts: dict) -> list[FactRow]:
    facts = company_facts.get("facts", {})
    rows: list[FactRow] = []
    for metric, (kind, unit, tags) in CONCEPTS.items():
        entries = _entries(facts, tags, unit)
        if not entries:
            continue
        anchor = _fy_anchor(entries)
        if kind == "stock":
            for e in entries:
                if e.get("start"):
                    continue
                end = _d(e["end"])
                fy, q = fiscal_position(end, anchor)
                rows.append(FactRow(metric, end, fy, q, float(e["val"]), unit, e["tag"], e["form"], _d(e["filed"]), e["accn"], False))
            continue
        rows.extend(_flow_rows(metric, unit, entries, anchor))
    # one row per (metric, period_end, fiscal_q)
    uniq: dict[tuple, FactRow] = {}
    for r in rows:
        uniq[(r.metric, r.period_end, r.fiscal_q)] = r
    return sorted(uniq.values(), key=lambda r: (r.metric, r.period_end, r.fiscal_q))


def _flow_rows(metric: str, unit: str, entries: list[dict], anchor: tuple[int, int]) -> list[FactRow]:
    rows: dict[tuple[date, int], FactRow] = {}
    chains: dict[str, dict[int, dict]] = {}
    for e in entries:
        if not e.get("start"):
            continue
        days = (_d(e["end"]) - _d(e["start"])).days
        k = round(days / QUARTER_DAYS)
        if not 1 <= k <= 4 or abs(days - k * QUARTER_DAYS) > 14:
            continue
        end = _d(e["end"])
        fy, q = fiscal_position(end, anchor)
        if k == 4:  # full fiscal year
            rows[(end, 0)] = FactRow(metric, end, fy, 0, float(e["val"]), unit, e["tag"], e["form"], _d(e["filed"]), e["accn"], False)
        if k == 1:  # directly reported discrete quarter
            rows[(end, q)] = FactRow(metric, end, fy, q, float(e["val"]), unit, e["tag"], e["form"], _d(e["filed"]), e["accn"], False)
        chains.setdefault(e["start"], {})[k] = e
    # fill gaps by differencing cumulative periods that share a start date
    # (weighted-average share counts cannot be differenced)
    for chain in ([] if metric == "shares_diluted" else chains.values()):
        for k in (2, 3, 4):
            if k in chain and k - 1 in chain:
                e = chain[k]
                end = _d(e["end"])
                fy, q = fiscal_position(end, anchor)
                if (end, q) in rows:
                    continue
                val = float(e["val"]) - float(chain[k - 1]["val"])
                rows[(end, q)] = FactRow(metric, end, fy, q, val, unit, e["tag"], e["form"], _d(e["filed"]), e["accn"], True)
    return list(rows.values())
