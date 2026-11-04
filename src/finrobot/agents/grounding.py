"""Grounding check for qualitative claims. The numeric Evaluator cannot see 'positioned well against peers';
this pass asks the figures model (gpt-oss, the fact-check role) which factual sentences the evidence does not support."""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from finrobot.agents.context import RunContext
from finrobot.agents.evaluator import cites_in
from finrobot.config import settings


class GroundingOut(BaseModel):
    unsupported: list[str]


SYSTEM = ("You are a strict fact-checker for an equity research note. You get EVIDENCE (verified figures and document passages) "
          "and a DRAFT. List every sentence in the DRAFT that states a fact about the company, its peers, its industry, or the cause "
          "of a result that the EVIDENCE does not support. Copy each such sentence exactly as written in the DRAFT. "
          "A cause attributed to a result ('driven by pricing power', 'suggests operational leverage', 'reflects cost discipline') counts as a "
          "factual claim and is unsupported unless a passage says it. "
          "Do not list: sentences fully supported by the evidence, recommendations, what-to-watch items, or sentences that only restate figures "
          "from the evidence. If everything is supported, return an empty list.")


def enabled() -> bool:
    return settings.grounding_check


def evidence_for(ctx: RunContext, text: str) -> str:
    ids = list(dict.fromkeys(cites_in(text)))
    parts = []
    for i in ids:
        if i in ctx.fb.facts:
            f = ctx.fb.facts[i]
            parts.append(f"[{i}] {f.label} = {f.display}")
        elif i in ctx.fb.passages:
            parts.append(f"[{i}] ({ctx.fb.passages[i].doc}) {' '.join(ctx.fb.passages[i].text.split())[:900]}")
    return "\n".join(parts)


def grounding_check(ctx: RunContext, text: str) -> tuple[str, list[str]]:
    """Returns (text with unsupported sentences removed, the sentences removed)."""
    if not enabled() or not text.strip():
        return text, []
    evidence = evidence_for(ctx, text)
    out = ctx.llm.structured("figures", GroundingOut, [SystemMessage(SYSTEM),
                                                       HumanMessage(f"EVIDENCE:\n{evidence or '(none)'}\n\nDRAFT:\n{text}")], temperature=0)
    flagged = out.unsupported if out is not None else []
    removed = []
    for sent in flagged:
        sent = (sent or "").strip()
        if len(sent) > 15 and sent in text:   # only remove text that really is in the draft
            text = text.replace(sent, "")
            removed.append(sent)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text, removed
