"""Parse #news-scanner Discord posts (structured headline embeds; any market cap)."""

from __future__ import annotations

import re
from typing import Any

_TICKER_START_RE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,8})\b")
_PRICE_FOLLOW_RE = re.compile(r"\$?\s*([0-9]+(?:\.[0-9]+)?)")
_MCAP_FOLLOW_RE = re.compile(r"^\$?\s*([0-9.]+)\s*([KMB]?)\s*$", re.IGNORECASE)
_RE_URL = re.compile(r"https://[^\s\)\]<>\"'\`]+")


def extract_source_urls(raw: str | None, *, limit: int = 4) -> list[str]:
    """HTTP(S) URLs from flattened Discord/embed text — used for bounded web follow-up."""
    if not raw or not str(raw).strip():
        return []
    seen: list[str] = []
    dup: set[str] = set()
    for m in _RE_URL.finditer(raw):
        u = str(m.group(0)).rstrip(".,;]")
        while u.endswith((")", "]", ".", ",", ";")):
            u = u[:-1]
        if u and u not in dup and u.startswith(("http://", "https://")):
            dup.add(u)
            seen.append(u)
            if len(seen) >= max(1, int(limit)):
                break
    return seen


def _scale(suffix: str) -> float:
    return {"K": 1e3, "M": 1e6, "B": 1e9}.get((suffix or "").upper(), 1.0)


def parse_news_scanner_post(content: str) -> dict[str, Any] | None:
    """Extract ticker, headline, optional price and market cap from a news-scanner message."""
    raw = (content or "").strip()
    if not raw:
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None

    i = 0
    if lines[0].lower() in ("small cap", "smallcap", "small-cap"):
        i = 1
    if i >= len(lines):
        return None

    ticker_line = lines[i].replace("**", "")
    tm = _TICKER_START_RE.match(ticker_line.strip())
    if not tm:
        return None
    ticker = tm.group(1).upper().strip(" .")
    i += 1

    headline_parts: list[str] = []
    while i < len(lines):
        low = lines[i].lower()
        if low in ("price", "mcap", "market cap", "mc", "timestamp", "link", "view news"):
            break
        headline_parts.append(lines[i])
        i += 1
    headline = " ".join(headline_parts).strip()
    if not headline:
        return None

    price: float | None = None
    mcap: float | None = None
    while i < len(lines):
        low = lines[i].lower()
        if low == "price" and i + 1 < len(lines):
            pm = _PRICE_FOLLOW_RE.search(lines[i + 1])
            if pm:
                try:
                    price = float(pm.group(1))
                except ValueError:
                    price = None
            i += 2
            continue
        if low in ("mcap", "market cap", "mc") and i + 1 < len(lines):
            mm = _MCAP_FOLLOW_RE.match(lines[i + 1].strip())
            if mm:
                try:
                    mcap = float(mm.group(1)) * _scale(mm.group(2) or "")
                except ValueError:
                    mcap = None
            i += 2
            continue
        i += 1

    return {
        "ticker": ticker,
        "news_headline": headline[:2000],
        "price": price,
        "market_cap": mcap,
        "raw_excerpt": raw[:6000],
    }
