"""HTML / markdown tables built from FactBook facts, so every cell can be traced to a source."""
from __future__ import annotations

from html import escape

from finrobot.tools.factbook import Fact


class Raw(str):
    """A table cell that is trusted HTML. Everything else is escaped: risk-table cells come straight from model output, and a cell
    that merely *started with* '<' used to bypass escaping."""


def money_m(v: float | None) -> str:
    """Table cell in USD millions."""
    if v is None:
        return "—"
    s = f"{abs(v) / 1e6:,.0f}"
    return f"({s})" if v < 0 else s


def cell(f: Fact | None, kind: str = "money") -> str:
    if f is None:
        return "—"
    if kind == "money":
        return money_m(f.value)
    if kind == "pct":
        return f"{f.value:.1f}%"
    if kind == "x":
        return f"{f.value:.2f}x"
    if kind == "eps":
        return f"{f.value:.2f}"
    return f"{f.value:,.2f}"


def html_table(header: list[str], rows: list[list[str]], caption: str = "", notes: str = "", text: bool = False) -> str:
    """text=True left-aligns every column (for prose cells). Tables over 14 rows, and text tables, may break across pages."""
    th = "".join(f"<th>{escape(h)}</th>" for h in header)
    body = ""
    for r in rows:
        tds = "".join(f"<td>{c if isinstance(c, Raw) else escape(str(c))}</td>" for c in r)
        body += f"<tr>{tds}</tr>"
    cap = f"<div class='tcap'>{escape(caption)}</div>" if caption else ""
    foot = f"<p class='tnote'>{escape(notes)}</p>" if notes else ""
    cls = "tblock" + (" long" if text or len(rows) > 14 else "")
    return f"<div class='{cls}'>{cap}<table class='fin{' text' if text else ''}'><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>{foot}</div>"


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)
