"""An in-process fake of the SEC endpoints FinRobot uses, as an httpx MockTransport."""
from __future__ import annotations

import json

import httpx

CIK = 1234567
TICKERS = {"0": {"cik_str": CIK, "ticker": "TST", "title": "Testco Inc"}, "1": {"cik_str": 999, "ticker": "OTH", "title": "Other Corp"}}

BASE_10Q = ("<html><body><div>Item 1. Financial Statements</div><p>{a}</p><div>Item 2. Management's Discussion and Analysis</div><p>{b}</p>"
            "<div>Item 1A. Risk Factors</div><p>{c}</p></body></html>")
HTML_10Q = BASE_10Q.format(a="Statements of operations and balance sheets. " * 20, b="Revenue grew on pricing and volume across all lines of business. " * 15,
                           c="Environmental regulation and commodity prices are risks to results. " * 15)
EX99 = "<html><body><p>Third quarter results. Revenue increased 8% on pricing and volume. " + "Detail. " * 80 + "</p></body></html>"


def submissions(n_q: int = 1, with_8k: bool = True) -> dict:
    # (form, accession, filed, report date, primary document, items)
    rows = [("10-Q", f"0001234567-26-00010{i}", f"2026-10-{20 + i:02d}", "2026-09-30", f"tst-10q{i}.htm", "") for i in range(n_q)]
    if with_8k:
        rows += [("8-K", "0001234567-26-000200", "2026-10-28", "2026-09-30", "tst-8k.htm", "2.02,9.01"),
                 ("8-K", "0001234567-26-000201", "2026-10-29", "", "tst-8k2.htm", "5.02")]   # the second is not an earnings release
    form, acc, filed, report, doc, items = (list(col) for col in zip(*rows, strict=True))
    return {"sicDescription": "Refuse Systems", "fiscalYearEnd": "1231",
            "filings": {"recent": {"form": form, "accessionNumber": acc, "filingDate": filed, "reportDate": report,
                                   "primaryDocument": doc, "items": items}}}


def company_facts() -> dict:
    def e(start, end, val, form="10-Q", filed="2026-10-20", accn="0001234567-26-000100"):
        return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "accn": accn}
    rev = [e("2025-01-01", "2025-12-31", 3600, form="10-K", filed="2026-02-20"),   # a full fiscal year anchors the fiscal calendar (Dec 31)
           e("2025-10-01", "2025-12-31", 900), e("2026-01-01", "2026-03-31", 950), e("2026-04-01", "2026-06-30", 1000), e("2026-07-01", "2026-09-30", 1050)]
    return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rev}}}}}


class FakeSEC:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self.requests: list[str] = []
        self.fail_next: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=submissions(**self.kw))
        if "/companyfacts/" in url:
            return httpx.Response(200, content=json.dumps(company_facts()))
        if url.endswith("/index.json"):
            acc = url.split("/")[-2]
            names = ["tst-8k.htm", "tst-ex991.htm"] if acc.endswith("000200") else ["tst-8k2.htm"]
            return httpx.Response(200, json={"directory": {"item": [{"name": n} for n in names]}})
        if url.endswith("tst-ex991.htm"):
            return httpx.Response(200, content=EX99.encode())
        if url.endswith(".htm"):
            return httpx.Response(200, content=HTML_10Q.encode())
        return httpx.Response(404)
