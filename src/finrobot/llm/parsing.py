"""Tolerant JSON extraction from model text (fallback when provider-side structured output fails)."""
from __future__ import annotations

import json


def parse_json(text: str) -> dict | None:
    """First JSON object in `text`, tolerating code fences and prose around it; None if there is none."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        brace = text.find("{")
        text = text[brace:] if brace >= 0 else text
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            try:
                out = json.loads(text[a:b + 1])
                return out if isinstance(out, dict) else None
            except json.JSONDecodeError:
                return None
    return None
