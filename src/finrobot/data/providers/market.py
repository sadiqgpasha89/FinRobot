"""Prices. There is no free, unauthenticated, non-scraping price API, so prices come from:

  * a CSV the user supplies (`finrobot import-prices`),
  * a manual quote (`finrobot set-price`),
  * the official Alpha Vantage API when ALPHAVANTAGE_API_KEY is set.

Everything downstream degrades gracefully when no price exists (valuation shows intrinsic value, no upside).
"""
from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from finrobot.config import settings


def parse_price_csv(text: str) -> list[tuple[date, float]]:
    """Accepts headers like Date,Close or date,close,... (case-insensitive; 'Adj Close' ignored)."""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
    dcol = cols.get("date") or cols.get("day")
    pcol = cols.get("close") or cols.get("price")
    if not dcol or not pcol:
        raise ValueError("CSV needs 'Date' and 'Close' columns")
    for r in reader:
        try:
            rows.append((date.fromisoformat(r[dcol].strip()[:10]), float(r[pcol])))
        except (ValueError, TypeError):
            continue
    return rows


def parse_alphavantage_daily(payload: dict) -> list[tuple[date, float]]:
    series = payload.get("Time Series (Daily)")
    if not series:
        raise ValueError(payload.get("Note") or payload.get("Information") or payload.get("Error Message") or "no price data returned")
    return sorted((date.fromisoformat(d), float(v["4. close"])) for d, v in series.items())


def fetch_alphavantage(ticker: str, client: httpx.Client | None = None) -> list[tuple[date, float]]:
    if not settings.alphavantage_key:
        raise ValueError("ALPHAVANTAGE_API_KEY is not set")
    c = client or httpx.Client(timeout=30)
    r = c.get("https://www.alphavantage.co/query", params={"function": "TIME_SERIES_DAILY", "symbol": ticker,
                                                           "outputsize": "compact", "apikey": settings.alphavantage_key})
    r.raise_for_status()
    return parse_alphavantage_daily(r.json())
