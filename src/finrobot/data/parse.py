"""Filing HTML -> text -> labelled sections -> overlapping chunks."""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

SECTION_NAMES = {
    "10-K": {"1": "Business", "1A": "Risk Factors", "7": "MD&A", "7A": "Market Risk", "8": "Financial Statements"},
    "10-Q": {"1": "Financial Statements", "2": "MD&A", "1A": "Risk Factors", "3": "Market Risk"},
}
ITEM_RE = re.compile(r"^\s*item\s+(\d{1,2}[ab]?)\s*[.:\-–—]?\s*(.*)$", re.I)


def html_to_text(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for t in soup.find_all(["ix:header"]):  # inline-XBRL hidden facts
        t.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c not in {"$", ")", "%"}]
        tr.replace_with(" | ".join(cells) + "\n")
    for t in soup.find_all(BLOCK_TAGS):
        t.append("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


@dataclass
class Section:
    name: str
    text: str


def split_sections(text: str, form: str) -> list[Section]:
    """Split on 'Item N' headings. The table of contents also matches, so the longest span per name wins."""
    names = SECTION_NAMES.get(form)
    if not names:
        return [Section("Full text", text)]
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    pos = 0
    for ln in lines:
        m = ITEM_RE.match(ln)
        if m and len(ln) < 160:
            marks.append((pos, m.group(1).upper()))
        pos += len(ln) + 1
    best: dict[str, str] = {}
    for i, (start, item) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        name = names.get(item)
        if not name:
            continue
        body = text[start:end].strip()
        if len(body) > len(best.get(name, "")):
            best[name] = body
    return [Section(n, b) for n, b in best.items() if len(b) > 400] or [Section("Full text", text)]


@dataclass
class Chunk:
    idx: int
    section: str
    text: str


def chunk_sections(sections: list[Section], size: int = 1200, overlap: int = 150) -> list[Chunk]:
    chunks: list[Chunk] = []
    for sec in sections:
        paras = [p.strip() for p in re.split(r"\n{1,}", sec.text) if p.strip()]
        buf = ""
        for p in paras:
            if len(buf) + len(p) + 1 <= size:
                buf = f"{buf}\n{p}" if buf else p
                continue
            if buf:
                chunks.append(Chunk(len(chunks), sec.name, buf))
                buf = (buf[-overlap:] + "\n" + p) if overlap else p
            else:
                buf = p
            while len(buf) > size * 1.5:  # one enormous paragraph
                chunks.append(Chunk(len(chunks), sec.name, buf[:size]))
                buf = buf[size - overlap:]
        if buf.strip():
            chunks.append(Chunk(len(chunks), sec.name, buf))
    return chunks
