"""Parse TrendVision all-in-one-scanner Discord messages into typed dicts.

Covers every shape seen in trading_ai/data/all_in_one_scanner_2026-05-11.json:

    SCANNER:  **TICK** :flag_us: • `#1` · `↑68%` · `$1.60` • **FT** 7.9M · **MC** 37M · **RV** 2x · **1V** 8K | `IND` • 0 Borrow
    FIRE:     `🔥` **TICK** ... CTB/SI
    WHALE:    `🐋` **TICK** ... Direction: ↑ | Price: 1.40 | Shares: 56.76K | Value: $79.46K
    HALT:     `🛑` **TICK** ... HALTED ...
    OFFERING: `⚠️` **TICK** ... OFFERING ...
    NEWS:     news bullets inside scanner alerts

Returns dict with at minimum {"type": ..., "ticker": ..., "raw": ...}.
"""

from __future__ import annotations

import re
from typing import Any

_TICKER_RE = re.compile(r"\*\*([A-Z][A-Z0-9.\-]{0,8})\*\*")
_PCT_RE = re.compile(r"`?[↑↓]\s*(\d+(?:\.\d+)?)%`?")
_PRICE_RE = re.compile(r"`?\$([0-9]+(?:\.[0-9]+)?)`?")
_FT_RE = re.compile(r"\*\*FT\*\*\s+([0-9.]+)\s*([KMB]?)", re.IGNORECASE)
_MC_RE = re.compile(r"\*\*MC\*\*\s+([0-9.]+)\s*([KMB]?)", re.IGNORECASE)
_RV_RE = re.compile(r"\*\*RV\*\*\s+([0-9.]+)x", re.IGNORECASE)
_RANK_RE = re.compile(r"`#(\d+)`")
_HALT_RE = re.compile(r"HALTED", re.IGNORECASE)
_OFFERING_RE = re.compile(r"OFFERING", re.IGNORECASE)
_FIRE_PREFIX = "🔥"
_WHALE_PREFIX = "🐋"
_HALT_PREFIX = "🛑"
_OFFER_PREFIX = "⚠️"

_WHALE_DIRECTION_RE = re.compile(r"Direction:\s*([↑→↓])")
_WHALE_PRICE_RE = re.compile(r"Price:\s*([0-9.]+)")
_WHALE_SHARES_RE = re.compile(r"Shares:\s*([0-9.]+)\s*([KMB]?)", re.IGNORECASE)
_WHALE_VALUE_RE = re.compile(r"Value:\s*\$([0-9.]+)\s*([KMB]?)", re.IGNORECASE)

_NEWS_LINE_RE = re.compile(r"`NEWS`|NEWS:", re.IGNORECASE)
_CTB_RE = re.compile(r"CTB[: ]+([0-9.]+%?)", re.IGNORECASE)
_SI_RE = re.compile(r"SI[: ]+([0-9.]+%?)", re.IGNORECASE)
_BORROW_RE = re.compile(r"0\s*Borrow", re.IGNORECASE)


def _scale(token: str) -> float:
    return {"K": 1e3, "M": 1e6, "B": 1e9}.get(token.upper(), 1.0)


def _ticker(content: str) -> str | None:
    m = _TICKER_RE.search(content)
    return m.group(1) if m else None


def _parse_amount(value: str, suffix: str) -> float:
    try:
        return float(value) * _scale(suffix)
    except ValueError:
        return 0.0


def detect_type(content: str) -> str:
    if _HALT_PREFIX in content or _HALT_RE.search(content):
        return "HALT"
    if _OFFER_PREFIX in content or _OFFERING_RE.search(content):
        return "OFFERING"
    if _FIRE_PREFIX in content:
        return "FIRE"
    if _WHALE_PREFIX in content:
        return "WHALE"
    if _NEWS_LINE_RE.search(content) and not _RV_RE.search(content):
        return "NEWS"
    if _RV_RE.search(content) or _RANK_RE.search(content):
        return "SCANNER"
    return "UNKNOWN"


def parse(content: str) -> dict[str, Any]:
    """Return a structured alert dict. Always includes type + raw."""
    out: dict[str, Any] = {"raw": content, "type": detect_type(content), "ticker": _ticker(content)}

    pct = _PCT_RE.search(content)
    if pct:
        try:
            out["pct"] = float(pct.group(1))
        except ValueError:
            pass

    price = _PRICE_RE.search(content)
    if price:
        try:
            out["price"] = float(price.group(1))
        except ValueError:
            pass

    ft = _FT_RE.search(content)
    if ft:
        out["float"] = _parse_amount(ft.group(1), ft.group(2))

    mc = _MC_RE.search(content)
    if mc:
        out["market_cap"] = _parse_amount(mc.group(1), mc.group(2))

    rv = _RV_RE.search(content)
    if rv:
        try:
            out["rv"] = float(rv.group(1))
        except ValueError:
            pass

    rank = _RANK_RE.search(content)
    if rank:
        try:
            out["rank"] = int(rank.group(1))
        except ValueError:
            pass

    if _BORROW_RE.search(content):
        out["zero_borrow"] = True

    if out["type"] == "WHALE":
        d = _WHALE_DIRECTION_RE.search(content)
        if d:
            out["direction"] = {"↑": "up", "↓": "down", "→": "flat"}.get(d.group(1), "flat")
        s = _WHALE_SHARES_RE.search(content)
        if s:
            out["shares"] = _parse_amount(s.group(1), s.group(2))
        v = _WHALE_VALUE_RE.search(content)
        if v:
            out["value_usd"] = _parse_amount(v.group(1), v.group(2))
        p = _WHALE_PRICE_RE.search(content)
        if p:
            try:
                out["price"] = float(p.group(1))
            except ValueError:
                pass

    if out["type"] == "FIRE":
        c = _CTB_RE.search(content)
        if c:
            out["ctb"] = c.group(1)
        s = _SI_RE.search(content)
        if s:
            out["si"] = s.group(1)

    headline = _extract_news_headline(content)
    if headline:
        out["news_headline"] = headline

    if out["type"] == "SCANNER":
        tags = _scanner_tags(content)
        if tags:
            out["tags"] = tags
        lbl = _scanner_label(content)
        if lbl:
            out["label"] = lbl
        out["indicators"] = _scanner_indicators(content)
        if _OFFERING_RE.search(content):
            out["has_offering"] = True

    return out


_LABEL_RE = re.compile(
    r"`#(\d+)`\s*·\s*`([^`]+)`",
    re.IGNORECASE,
)


def _scanner_label(content: str) -> str | None:
    first = content.split("\n", 1)[0]
    m = _LABEL_RE.search(first)
    if not m:
        return None
    raw = m.group(2).strip()
    upper = raw.upper()
    if upper.startswith("MOMENTUM"):
        return "MOMENTUM"
    if upper.startswith("BREAKOUT"):
        return "BREAKOUT"
    if upper.startswith("NBREAK"):
        return "NBREAK"
    if upper.startswith("REV"):
        return "REV V"
    if upper.startswith("BTT"):
        return "BTT V"
    if re.search(r"^[↑↓]", raw):
        return None
    return raw.split()[0] if raw else None


def _scanner_indicators(content: str) -> list[str]:
    first = content.split("\n", 1)[0]
    inds: list[str] = []
    if _BORROW_RE.search(first):
        inds.append("0 Borrow")
    if re.search(r"Reg\s*SHO", first, re.IGNORECASE):
        inds.append("Reg SHO")
    if re.search(r"Potential\s+Squeeze", first, re.IGNORECASE):
        inds.append("Potential Squeeze")
    if re.search(r"Known\s+Runner", first, re.IGNORECASE):
        inds.append("Known Runner")
    return inds


_TAG_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Known\s+Runner", re.IGNORECASE), "KnownRunner"),
    (re.compile(r"Potential\s+Squeeze", re.IGNORECASE), "PotSqueeze"),
    (re.compile(r"Reg\s*SHO", re.IGNORECASE), "RegSHO"),
)


def _scanner_tags(content: str) -> list[str]:
    """Extract TrendVision trailing tags / markers (first line only; avoids NEWS/SEC tails)."""
    first = content.split("\n", 1)[0]
    seen: set[str] = set()
    ordered: list[str] = []

    def add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            ordered.append(tag)

    if _BORROW_RE.search(first):
        add("0Borrow")
    for pat, canon in _TAG_PHRASES:
        if pat.search(first):
            add(canon)
    if re.search(r"`BREAKOUT`", first):
        add("BREAKOUT")
    return ordered


def _extract_news_headline(content: str) -> str | None:
    """Pull the headline portion from a NEWS bullet. None if no news in this message."""
    m = re.search(r"(?:`NEWS`|NEWS:)\s*[•\-:]?\s*(.+?)(?:\n|$)", content, re.IGNORECASE)
    if not m:
        return None
    headline = m.group(1).strip()
    headline = re.sub(r"\s*https?://\S+", "", headline).strip()
    return headline or None
