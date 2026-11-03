"""Deterministic Evaluator: ties every unit-bearing number in prose to a registered fact or a cited passage.

A claim is a number with a $ sign or a unit (%, bps, x, thousand/million/billion). Bare numbers (years, counts, dates,
list indices) are not claims. Hard issues fail the draft; 'miscited' is only a warning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from finrobot.tools.factbook import FactBook

CITE_RE = re.compile(r"\[((?:[FS]\d+)(?:\s*[,;]\s*[FS]\d+)*)\]")
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])(?P<neg>[-−–(])?(?P<cur>\$)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?P<unit>%|percentage points?|pp|percent|bps|basis points?|billion|bn|million|mm|thousand|x|×|[BMK])(?![A-Za-z]))?")
SCALES = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


@dataclass
class Claim:
    text: str
    value: float          # in raw units (USD, percent points, bps, multiple)
    kind: str             # money | pct | pp | bps | x
    tol: float
    decimals: int


@dataclass
class Issue:
    level: str            # hard | warn
    kind: str             # unsupported | uncited | bad_cite | miscited
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.kind}: {self.detail} — in: \"{self.where[:140]}\""


@dataclass
class EvalReport:
    issues: list[Issue] = field(default_factory=list)
    claims: int = 0
    matched_facts: int = 0
    grounded_passages: int = 0

    @property
    def hard(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "hard"]

    @property
    def ok(self) -> bool:
        return not self.hard

    def summary(self) -> dict:
        return {"ok": self.ok, "claims": self.claims, "matched_facts": self.matched_facts,
                "grounded_in_passages": self.grounded_passages, "issues": [str(i) for i in self.issues]}


def cites_in(text: str) -> list[str]:
    return [c.strip() for m in CITE_RE.findall(text) for c in re.split(r"[,;]", m)]


def extract_claims(segment: str) -> list[Claim]:
    s = CITE_RE.sub(" ", segment)
    out: list[Claim] = []
    for m in NUM_RE.finditer(s):
        raw, unit, cur = m.group("num"), (m.group("unit") or "").lower(), m.group("cur")
        if not unit and not cur:
            continue  # bare number: year, count, date, index
        if unit in ("b", "m", "k") and not cur:
            continue  # "5M" without a $ is ambiguous (could be anything); leave it alone
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        if unit.startswith("percentage") or unit == "pp":
            kind, scale = "pp", 1.0
        elif unit in ("%", "percent"):
            kind, scale = "pct", 1.0
        elif unit.startswith(("bps", "basis")):
            kind, scale = "bps", 1.0
        elif unit in ("x", "×"):
            kind, scale = "x", 1.0
        else:
            kind, scale = "money", SCALES.get(unit, 1.0)
        raw_val = val * scale
        tol = 0.5 * 10 ** (-dec) * scale
        out.append(Claim(m.group(0).strip(), raw_val, kind, max(tol, abs(raw_val) * 1e-9), dec))
    return out


def _fact_matches(c: Claim, fb: FactBook) -> list[str]:
    hits = []
    for f in fb.facts.values():
        v = abs(f.value)
        cv = abs(c.value)
        if c.kind == "money" and f.unit in ("USD", "USD/shares", "shares") and abs(v - cv) <= c.tol + 1e-9:
            hits.append(f.id)
        elif c.kind == "pct" and f.unit == "%" and abs(v - cv) <= max(c.tol, 0.05):
            hits.append(f.id)
        elif c.kind == "pp" and ((f.unit == "bps" and abs(v / 100 - cv) <= max(c.tol, 0.05))
                                 or (f.unit == "pp" and abs(v - cv) <= max(c.tol, 0.05))):
            hits.append(f.id)
        elif c.kind == "bps" and ((f.unit == "bps" and abs(v - cv) <= max(c.tol, 0.5))
                                  or (f.unit == "pp" and abs(v * 100 - cv) <= max(c.tol, 0.5))):
            hits.append(f.id)
        elif c.kind == "x" and f.unit == "x" and abs(v - cv) <= max(c.tol, 0.005):
            hits.append(f.id)
    return hits


def _passage_numbers(text: str) -> list[float]:
    vals = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            vals.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return vals


def _grounded_in(c: Claim, passages: list[str]) -> bool:
    """The claim's number appears in a cited passage (also tried at the other of million/billion scale)."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", c.text)
    if not m:
        return False
    shown = float(m.group(0).replace(",", ""))
    cands = {shown, shown * 1000, shown / 1000}
    for p in passages:
        for v in _passage_numbers(p):
            if any(abs(v - cand) <= max(abs(cand) * 0.005, 1e-9) for cand in cands):
                return True
    return False


def _segments(text: str) -> list[str]:
    segs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        segs.append(line)
    return segs


PLACEHOLDER_RE = re.compile(r"\[[FS]#\]")
PERIOD_RE = re.compile(r"\bFY\s?(\d{4})(?:\s*Q([1-4]))?", re.I)


def periods_in(text: str) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2) or "") for m in PERIOD_RE.finditer(text)}


def fact_periods(fb: FactBook, fid: str, _seen: set | None = None) -> set[tuple[str, str]]:
    """Periods named in a fact's label and, recursively, in the facts it was derived from."""
    _seen = _seen if _seen is not None else set()
    if fid in _seen or fid not in fb.facts:
        return set()
    _seen.add(fid)
    f = fb.facts[fid]
    out = periods_in(f.label)
    for i in f.inputs:
        out |= fact_periods(fb, i, _seen)
    return out


def period_mismatch(seg: str, cited_facts: set[str], fb: FactBook) -> list[str]:
    """Fiscal periods the sentence names that none of its cited facts is about. A mention of 'FY2026' is satisfied by 'FY2026 Q3'."""
    if not cited_facts:
        return []
    allowed: set[tuple[str, str]] = set()
    for fid in cited_facts:
        allowed |= fact_periods(fb, fid)
    if not allowed:
        return []
    bad = []
    for year, q in periods_in(CITE_RE.sub(" ", seg)):
        if not any(y == year and (not q or q == aq) for y, aq in allowed):
            bad.append(f"FY{year}" + (f" Q{q}" if q else ""))
    return bad


def evaluate(text: str, fb: FactBook) -> EvalReport:
    rep = EvalReport()
    for seg in _segments(text):
        if PLACEHOLDER_RE.search(seg):
            rep.issues.append(Issue("hard", "bad_cite", seg, "placeholder citation like [S#]; cite a real id or remove the claim"))
        ids = cites_in(seg)
        known = [i for i in ids if i in fb.facts or i in fb.passages]
        for bad in (i for i in ids if i not in known):
            rep.issues.append(Issue("hard", "bad_cite", seg, f"{bad} does not exist"))
        claims = extract_claims(seg)
        if not claims:
            for bad in period_mismatch(seg, {i for i in known if i in fb.facts}, fb):
                rep.issues.append(Issue("hard", "wrong_period", seg, f"{bad} is named but not covered by the cited figures"))
            continue
        if not known:
            rep.claims += len(claims)
            rep.issues.append(Issue("hard", "uncited", seg, f"{len(claims)} figure(s) without a [F#]/[S#] citation"))
            continue
        cited_passages = [fb.passages[i].text for i in known if i in fb.passages]
        cited_facts = {i for i in known if i in fb.facts}
        for bad in period_mismatch(seg, cited_facts, fb):
            rep.issues.append(Issue("hard", "wrong_period", seg, f"{bad} is named but the cited figures are for "
                                    f"{sorted({('FY' + y + (' Q' + q if q else '')) for f in cited_facts for y, q in fact_periods(fb, f)})}"))
        for c in claims:
            rep.claims += 1
            hits = _fact_matches(c, fb)
            if hits:
                rep.matched_facts += 1
                if cited_facts and not (set(hits) & cited_facts):
                    rep.issues.append(Issue("warn", "miscited", seg, f"'{c.text}' matches {hits[0]} but cites {sorted(cited_facts)}"))
            elif cited_passages and _grounded_in(c, cited_passages):
                rep.grounded_passages += 1
            else:
                rep.issues.append(Issue("hard", "unsupported", seg, f"'{c.text}' matches no registered fact or cited passage"))
    return rep
