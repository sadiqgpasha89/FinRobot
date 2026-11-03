"""HTML (Jinja2) -> PDF (WeasyPrint), plus the deterministic format check."""
from __future__ import annotations

import base64
import re
from datetime import date
from html import escape

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from finrobot.agents.context import RunContext
from finrobot.agents.evaluator import CITE_RE, cites_in
from finrobot.config import ROOT
from finrobot.report.model import SECTIONS, ReportModel

TEMPLATES = ROOT / "src" / "finrobot" / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["j2"]))


def clean_prose(text: str) -> str:
    """Models sometimes emit rules (---) or headings; markdown would turn the line above a rule into a heading."""
    lines = [ln for ln in (text or "").splitlines() if not re.fullmatch(r"\s*([-=*_]\s*){3,}", ln)]
    return "\n".join(re.sub(r"^\s*#{1,6}\s*", "", ln) for ln in lines)


def prose_html(text: str) -> str:
    """Markdown -> HTML with [F#]/[S#] citations turned into superscripts."""
    html = markdown.markdown(clean_prose(text), extensions=["sane_lists"])
    return CITE_RE.sub(lambda m: "<sup class='cite'>" + ", ".join(c.strip() for c in re.split(r"[,;]", m.group(1))) + "</sup>", html)


def lineage(ctx: RunContext, fid: str, _seen: set | None = None) -> list[str]:
    """The filing lines a figure ultimately comes from: derived facts are followed back to their XBRL inputs."""
    _seen = _seen if _seen is not None else set()
    if fid in _seen or fid not in ctx.fb.facts:
        return []
    _seen.add(fid)
    f = ctx.fb.facts[fid]
    if f.inputs:
        out: list[str] = []
        for i in f.inputs:
            out += lineage(ctx, i, _seen)
        return out
    parts = f.source.split(":")
    if parts[0] == "XBRL" and len(parts) >= 5:   # XBRL:<tag>:<period end>:<accession>:<reported|derived>
        return [f"{parts[1]} · period ending {parts[2]} · SEC accession {parts[3]}"]
    return [f.source]


def source_text(ctx: RunContext, fid: str) -> str:
    leaves = list(dict.fromkeys(lineage(ctx, fid)))
    f = ctx.fb.facts[fid]
    prefix = f"{f.source.split(':', 1)[1].strip()} — " if f.source.startswith("calc:") else ""
    shown = "; ".join(leaves[:4]) + (f"; +{len(leaves) - 4} more" if len(leaves) > 4 else "")
    return prefix + shown if leaves else f.source


def render_html(ctx: RunContext, model: ReportModel, prose: dict[str, str], extra_tables: dict[str, list[str]], verification: dict,
                extra_text: list[str] | None = None) -> str:
    sections, all_text = [], list(extra_text or [])
    for title in SECTIONS:
        sd = model.sections[title]
        text = prose.get(title, "")
        all_text.append(text)
        has_table = bool(extra_tables.get(title)) or bool(sd.tables)
        sections.append({
            "key": title, "title": title,
            "prose": prose_html(text) if text else ("" if has_table and title == "Risks and Mitigations" else "<p><i>No verified text could be produced for this section.</i></p>"),
            "tables": extra_tables.get(title, []) + sd.tables,
            "charts": [{"b64": base64.b64encode(ctx.artifacts[n]).decode(), "caption": cap} for n, cap in sd.charts if n in ctx.artifacts],
        })
    cited = list(dict.fromkeys(i for t in all_text for i in cites_in(t)))
    cited_facts = [{"id": i, "label": ctx.fb.facts[i].label, "display": ctx.fb.facts[i].display, "source": source_text(ctx, i)}
                   for i in cited if i in ctx.fb.facts]
    cited_passages = [{"id": i, "doc": ctx.fb.passages[i].doc, "snippet": " ".join(ctx.fb.passages[i].text.split())[:220] + "…"}
                      for i in cited if i in ctx.fb.passages]
    kpis = []
    ov_facts = {f.label: f for f in model.sections["Company Overview"].facts}
    for label_part, name in (("TTM revenue", "TTM revenue"), ("TTM EBITDA", "TTM EBITDA"), ("net debt / TTM EBITDA", "Net debt / EBITDA")):
        f = next((f for lab, f in ov_facts.items() if label_part in lab), None)
        if f:
            kpis.append({"label": name, "value": f.display.replace(" billion", "B").replace(" million", "M")})
    if model.dcf_result:
        ps = model.dcf_result["per_share"]
        kpis.append({"label": "DCF value / share", "value": f"${ps:,.2f}"})
    price = model.price
    css = _env.from_string((TEMPLATES / "report.css").read_text()).render(ticker=ctx.ticker, period=ctx.period)
    return _env.get_template("report.html.j2").render(
        css=css, name=ctx.name, ticker=ctx.ticker, period=ctx.period, period_end=model.period_end, generated=date.today().isoformat(),
        price=price, price_text=(f"${price['close']:.2f} ({price['source']}, {price['day']})" if price else ""), kpis=kpis, sections=sections,
        warnings=model.warnings, verification=verification, cited_facts=cited_facts, cited_passages=cited_passages)


def to_pdf(html: str) -> tuple[bytes, int]:
    from weasyprint import HTML
    doc = HTML(string=html).render()
    return doc.write_pdf(), len(doc.pages)


BAD_TOKENS = re.compile(r"\b(NaN|nan|None(?! disclosed)|inf)\b|\{\{|\}\}|\[[FS]#\]|undefined")


def format_check(html: str, pdf: bytes, pages: int) -> dict:
    """Structural checks: every section present, no placeholder junk in the text, images embedded, a real PDF of sane length."""
    text = re.sub(r"<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    issues = []
    for s in SECTIONS:
        if f">{s}<" not in html:
            issues.append(f"missing section: {s}")
    junk = sorted(set(m.group(0) for m in BAD_TOKENS.finditer(text)))
    if junk:
        issues.append(f"placeholder/junk tokens in text: {junk}")
    for m in re.finditer(r'<img src="data:image/png;base64,([^"]*)"', html):
        if len(m.group(1)) < 500:
            issues.append("an embedded chart is empty")
    if not pdf.startswith(b"%PDF"):
        issues.append("output is not a PDF")
    if not 3 <= pages <= 20:
        issues.append(f"unexpected page count: {pages}")
    return {"ok": not issues, "issues": issues, "pages": pages}
