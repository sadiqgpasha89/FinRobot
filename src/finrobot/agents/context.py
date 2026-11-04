"""Per-run context shared by agents and tools: company, FactBook, LLM, artifact store, event sink."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from finrobot.data import store
from finrobot.llm.gateway import LLMGateway
from finrobot.tools.factbook import FactBook
from finrobot.tools.metrics import Metrics, period_label


@dataclass
class RunContext:
    ticker: str
    name: str
    llm: LLMGateway
    fb: FactBook = field(default_factory=FactBook)
    fy: int | None = None            # focus period
    fq: int | None = None
    period_end: date | None = None   # end date of the focus quarter
    artifacts: dict[str, bytes] = field(default_factory=dict)
    emit: Callable[[dict], None] = lambda e: None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metrics = Metrics(self.ticker, self.fb, store)
        if self.period_end is None and self.fy is not None:
            row = store.get_fact(self.ticker, "revenue", self.fy, self.fq or 4)
            self.period_end = row["period_end"] if row else None

    @property
    def period(self) -> str:
        return period_label(self.fy, self.fq) if self.fy is not None else "latest"

    def event(self, type_: str, **kw: Any) -> None:
        self.emit({"type": type_, **kw})


def resolve_period(ticker: str, fy: int | None, fq: int | None) -> tuple[int, int]:
    """Fill in a missing fiscal year/quarter with the most recent matching reported quarter."""
    latest = store.latest_quarter(ticker)
    if latest is None:
        raise LookupError(f"No financial data for {ticker}. Run `finrobot ingest {ticker}` first.")
    if fq is None:
        return (fy or latest[0]), latest[1] if fy is None else 4
    if fy is not None:
        return fy, fq
    # most recent year that has this quarter reported
    rows = store.get_facts(ticker, "revenue", quarterly=True, limit=20)
    for r in rows:
        if r["fiscal_q"] == fq:
            return r["fiscal_year"], fq
    return latest[0], fq
