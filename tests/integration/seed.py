"""A synthetic company with 15 quarters of XBRL-style facts and a few filings, so the whole pipeline can run without the network."""
from __future__ import annotations

from datetime import date, timedelta

from fakes import FakeEmbeddings
from finrobot.data import store
from finrobot.data.xbrl import FactRow

TICKER, NAME = "TST", "Testco Inc"
QUARTER_ENDS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
RATIOS = {"operating_income": 0.20, "d_and_a": 0.08, "op_cash_flow": 0.25, "capex": 0.10, "net_income": 0.12, "pretax_income": 0.16,
          "income_tax": 0.04, "interest_expense": 0.02}
SHARES = 100e6


def revenue(i: int) -> float:
    """Quarter index 0 = FY2023 Q1; 2% sequential growth."""
    return 1000e6 * 1.02 ** i


def _row(metric, end, fy, q, value, unit="USD", tag="Tag"):
    return FactRow(metric, end, fy, q, float(value), unit, tag, "10-Q", end + timedelta(days=30), f"0000000-{end:%y}-{q}", False)


def seed(last_year: int = 2026, last_q: int = 3) -> dict[str, float]:
    store.upsert_company(TICKER, 1234567, NAME, "Refuse Systems", "--12-31")
    rows, annual = [], {}
    quarters = [(y, q) for y in range(2023, last_year + 1) for q in range(1, 5) if (y, q) <= (last_year, last_q)]
    for i, (y, q) in enumerate(quarters):
        end = date(y, *QUARTER_ENDS[q])
        rev = revenue(i)
        rows.append(_row("revenue", end, y, q, rev, tag="Revenues"))
        for metric, ratio in RATIOS.items():
            rows.append(_row(metric, end, y, q, rev * ratio, tag=metric))
            annual.setdefault((metric, y), 0.0)
            annual[(metric, y)] += rev * ratio
        annual[("revenue", y)] = annual.get(("revenue", y), 0.0) + rev
        rows += [_row("shares_diluted", end, y, q, SHARES, "shares"), _row("eps_diluted", end, y, q, rev * 0.12 / SHARES, "USD/shares"),
                 _row("cash", end, y, q, 200e6), _row("debt_noncurrent", end, y, q, 1.5e9), _row("debt_current", end, y, q, 50e6),
                 _row("total_assets", end, y, q, 5e9), _row("equity", end, y, q, 2e9)]
    for (metric, y), v in annual.items():
        if y < last_year:
            rows.append(_row(metric, date(y, 12, 31), y, 0, v, tag=metric))
    store.replace_facts(TICKER, rows)
    return {"revenue_q3_2026": revenue(quarters.index((2026, 3))), "revenue_q3_2025": revenue(quarters.index((2025, 3)))}


PASSAGES = [
    ("8-K", "2026-10-28", "Earnings Release", "Third quarter revenue increased on pricing and volume growth in the collection business. "
     "Commodity prices for recycled materials were a headwind. Management expects full year growth to continue."),
    ("10-Q", "2026-10-30", "MD&A", "Operating margin improved because of lower fuel costs and productivity gains in operations. "
     "Labor and maintenance costs rose with inflation but were offset by pricing."),
    ("10-K", "2026-02-20", "Business", "Testco provides collection, transfer, recycling and disposal services to residential, commercial and industrial customers."),
    ("10-K", "2026-02-20", "Risk Factors", "Our business is subject to extensive environmental regulation and liability. Fluctuations in commodity prices for "
     "recycled materials affect our results. Our indebtedness could limit our flexibility."),
]


def seed_filings(emb: FakeEmbeddings | None = None) -> None:
    emb = emb or FakeEmbeddings()
    for n, (form, filed, section, text) in enumerate(PASSAGES):
        period = "2026-09-30" if form != "10-K" else "2025-12-31"
        store.insert_filing(TICKER, form, f"0000000-26-{n:06d}", filed, period, f"https://example.test/{n}", f"raw/{n}",
                            [(0, section, text, emb.embed_documents(["search_document: " + text])[0])])
