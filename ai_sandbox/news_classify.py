"""Rule-based headline classification (replaces Gemini news classifier)."""

from __future__ import annotations

import re

_NEGATIVE = re.compile(
    r"\b("
    r"offering|atm offering|registered direct|shelf registration|"
    r"bankruptcy|chapter 11|delist|delisting|"
    r"reverse split|stock split reverse|"
    r"going concern|substantial doubt|"
    r"sec investigation|class action|"
    r"termination of.*offering|withdrawn offering"
    r")\b",
    re.IGNORECASE,
)


def classify_headline(headline: str) -> str:
    """Return POSITIVE, NEGATIVE, NEUTRAL, or SQUEEZE."""
    h = (headline or "").strip()
    if not h:
        return "NEUTRAL"
    if _NEGATIVE.search(h):
        return "NEGATIVE"
    if re.search(r"\b(short squeeze|0 borrow|reg sho)\b", h, re.IGNORECASE):
        return "SQUEEZE"
    return "NEUTRAL"
