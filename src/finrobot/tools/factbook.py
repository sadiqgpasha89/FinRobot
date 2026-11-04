"""FactBook: every number and passage an agent sees is registered under an id (F1.., S1..).

Agents cite those ids; the Evaluator later checks that numbers in prose tie back to registered facts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


def fmt_money(v: float, decimals: int | None = None) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.{2 if decimals is None else decimals}f} billion"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.{1 if decimals is None else decimals}f} million"
    return f"{sign}${a:,.{0 if decimals is None else decimals}f}"


def fmt_value(v: float, unit: str) -> str:
    if unit == "USD":
        return fmt_money(v)
    if unit == "USD/shares":
        return f"${v:,.2f}"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "pp":
        return f"{v:.1f} percentage points"
    if unit == "bps":
        return f"{v:,.0f} basis points"
    if unit == "x":
        return f"{v:.2f}x"
    if unit == "shares":
        return f"{v / 1e6:,.1f} million shares"
    return f"{v:,.4g}"


@dataclass
class Fact:
    id: str
    label: str
    value: float
    unit: str  # USD, USD/shares, shares, %, pp (percentage points), bps, x, count
    source: str
    derived: bool = False
    inputs: list[str] = field(default_factory=list)
    basis: str = ""   # period basis: instant (balance sheet), q (one quarter), ttm, fy; "mixed" when inputs disagree

    @property
    def display(self) -> str:
        return fmt_value(self.value, self.unit)


@dataclass
class Passage:
    id: str
    chunk_id: int
    doc: str  # e.g. "10-Q filed 2026-10-28 · MD&A"
    text: str


class FactBook:
    def __init__(self) -> None:
        self.facts: dict[str, Fact] = {}
        self.passages: dict[str, Passage] = {}
        self._fact_key: dict[tuple, str] = {}
        self._chunk_key: dict[int, str] = {}

    def add_fact(self, label: str, value: float, unit: str, source: str, derived: bool = False,
                 inputs: list[str] | None = None, basis: str = "") -> Fact:
        key = (label, round(value, 6), unit, source)
        if key in self._fact_key:
            return self.facts[self._fact_key[key]]
        f = Fact(f"F{len(self.facts) + 1}", label, float(value), unit, source, derived, list(inputs or []), basis)
        self.facts[f.id] = f
        self._fact_key[key] = f.id
        return f

    def add_passage(self, chunk_id: int, doc: str, text: str) -> Passage:
        if chunk_id in self._chunk_key:
            return self.passages[self._chunk_key[chunk_id]]
        p = Passage(f"S{len(self.passages) + 1}", chunk_id, doc, text)
        self.passages[p.id] = p
        self._chunk_key[chunk_id] = p.id
        return p

    def to_dict(self) -> dict:
        return {"facts": [asdict(f) for f in self.facts.values()], "passages": [asdict(p) for p in self.passages.values()]}

    def load(self, d: dict) -> "FactBook":
        """Merge a serialised FactBook into this one, in place (objects holding a reference to it keep working)."""
        for f in d.get("facts", []):
            self.facts[f["id"]] = Fact(**f)
            self._fact_key[(f["label"], round(f["value"], 6), f["unit"], f["source"])] = f["id"]
        for p in d.get("passages", []):
            self.passages[p["id"]] = Passage(**p)
            self._chunk_key[p["chunk_id"]] = p["id"]
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "FactBook":
        return cls().load(d)
