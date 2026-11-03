"""SEC EDGAR client: ticker lookup, submissions, XBRL company facts, filing documents.

SEC fair-access rules: a declared User-Agent with contact details and at most 10 requests/second.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from finrobot.config import settings


class EdgarError(RuntimeError):
    pass


@dataclass
class Filing:
    accession: str
    form: str
    filed: str
    report_date: str
    primary_doc: str
    items: str = ""

    @property
    def acc_nodash(self) -> str:
        return self.accession.replace("-", "")


class EdgarClient:
    MIN_GAP = 0.15  # seconds between requests, comfortably under 10/s

    def __init__(self, user_agent: str | None = None, transport: httpx.BaseTransport | None = None):
        ua = user_agent or settings.sec_user_agent
        if not ua:
            raise EdgarError("Set SEC_USER_AGENT in .env (e.g. 'FinRobot you@example.com'). SEC requires contact details.")
        self._client = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=60,
                                    follow_redirects=True, transport=transport)
        self._last = 0.0
        self._gate = threading.Lock()
        self._tickers: dict | None = None
        settings.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str) -> httpx.Response:
        for attempt in range(4):
            with self._gate:
                wait = self.MIN_GAP - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
            try:
                r = self._client.get(url)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise EdgarError(f"GET {url} failed: {e}") from e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 503):
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise EdgarError(f"GET {url} -> {r.status_code}")
            return r
        raise EdgarError(f"GET {url}: rate limited")

    def _cached(self, key: str, url: str) -> bytes:
        path = settings.cache_dir / key
        if path.exists():
            return path.read_bytes()
        data = self._get(url).content
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    # --- identity -----------------------------------------------------------------
    def _ticker_map(self) -> dict:
        if self._tickers is None:
            import json
            raw = self._cached("company_tickers.json", "https://www.sec.gov/files/company_tickers.json")
            self._tickers = json.loads(raw)
        return self._tickers

    def resolve(self, query: str) -> tuple[str, int, str]:
        """Ticker or company name -> (TICKER, cik, official name)."""
        q = query.strip().lower()
        rows = list(self._ticker_map().values())
        for r in rows:
            if r["ticker"].lower() == q:
                return r["ticker"], int(r["cik_str"]), r["title"]
        norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower().replace(",", " ")).replace(" inc", "").replace(" corp", "").strip()
        nq = norm(q)
        hits = [r for r in rows if norm(r["title"]) == nq] or [r for r in rows if nq and nq in norm(r["title"])]
        if not hits:
            raise EdgarError(f"No SEC registrant found for '{query}'")
        r = min(hits, key=lambda x: len(x["title"]))
        return r["ticker"], int(r["cik_str"]), r["title"]

    # --- data ---------------------------------------------------------------------
    def submissions(self, cik: int) -> dict:
        import json
        return json.loads(self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").content)

    def company_facts(self, cik: int) -> dict:
        import json
        return json.loads(self._get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json").content)

    def filings(self, cik: int, forms: tuple[str, ...] = ("10-K", "10-Q"), limit: int | None = None,
                earnings_8k: bool = False) -> list[Filing]:
        rec = self.submissions(cik)["filings"]["recent"]
        out: list[Filing] = []
        for i, form in enumerate(rec["form"]):
            items = rec.get("items", [""] * len(rec["form"]))[i] or ""
            if form in forms or (earnings_8k and form == "8-K" and "2.02" in items):
                out.append(Filing(rec["accessionNumber"][i], form, rec["filingDate"][i], rec.get("reportDate", [""] * len(rec["form"]))[i] or "",
                                  rec["primaryDocument"][i], items))
        return out[:limit] if limit else out

    def document(self, cik: int, f: Filing, name: str | None = None) -> tuple[str, bytes]:
        name = name or f.primary_doc
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{f.acc_nodash}/{name}"
        return name, self._cached(f"{cik}/{f.acc_nodash}/{name}", url)

    def earnings_exhibit(self, cik: int, f: Filing) -> tuple[str, bytes] | None:
        """EX-99.1 of an 8-K with Item 2.02 is the quarterly earnings press release."""
        import json
        idx = json.loads(self._get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{f.acc_nodash}/index.json").content)
        names = [i["name"] for i in idx.get("directory", {}).get("item", [])]
        for n in names:
            if re.search(r"ex[-_]?99[-_.]?0?1", n, re.I) and n.lower().endswith((".htm", ".html")):
                return self.document(cik, f, n)
        return None
