"""Hard discard rules — context-aware version.

Receives the parsed alert + a per-ticker context dict from
``ticker_context.build()``. Decisions are pre-Gemini scorer so they need to be
deterministic and fast.

Design philosophy (loosened May 2026):
- Alert #1 is almost always WATCH — we rarely trade on first sight, but we
  still want the scorer to *see* it so the alert is logged and conditions the
  scoring of alert #2. The filter therefore stays light:
    * pct cap raised to 50% (70% if halt/news/fast_mover elevated)
    * RV floor dropped to 3x — only true zero-volume noise (<3x) is killed
- Alert #2+ has no pct cap at all. Cumulative % from yesterday's close is
  not our entry metric — the gap between alerts (pct_jump / rv_growth) is.
  A stock up 60% on the day can still give a clean 10% continuation leg;
  The scorer reads the situation.
- Fade kill is still hard: alert #2+ with pct_jump <= 0 = price reversing
  between alerts → discard before scoring.
- Float cap remains hard; market cap is not hard-filtered (scorer handles size).
- A headline on **this** SCANNER message skips the RV floor entirely so Layer 2
  (Haiku news classification) can run; low RV is expected on fresh news.
"""

from __future__ import annotations

from typing import Any

from . import config, db


def hard_filter(
    alert: dict[str, Any],
    slot_ticker_counts: dict[str, int],
    context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return (passed, reason). ``context`` defaults to {} if omitted."""
    ctx = context or {}
    atype = alert.get("type")
    ticker = (alert.get("ticker") or "").upper()

    # ── Immediate structural discards (never reach scorer) ────────────────
    if atype == "HALT":
        return False, "halt"
    if atype == "OFFERING":
        if ticker:
            db.block_offering(ticker)
        return False, "offering"
    if atype in ("UNKNOWN", "NEWS"):
        return False, f"type:{atype}"
    if not ticker:
        return False, "no_ticker"

    if db.offering_block_active(ticker, config.OFFERING_BLOCK_HOURS):
        return False, "offering_block_active"

    if slot_ticker_counts.get(ticker, 0) >= config.MAX_SLOTS_PER_TICKER:
        return False, "ticker_at_slot_cap"

    # ── WHALE and FIRE always pass through to scorer ──────────────────────
    if atype == "WHALE":
        return True, "whale"
    if atype == "FIRE":
        return True, "fire"

    # ── Extract alert values ──────────────────────────────────────────────
    cur_rv = alert.get("rv")
    cur_pct = alert.get("pct")
    flt = alert.get("float")
    tags = alert.get("tags") or []

    # ── Extract context signals ───────────────────────────────────────────
    alert_number = ctx.get("alert_number", 1)
    alert1_tags = ctx.get("alert1_tags") or []
    rv_growth = ctx.get("rv_growth")  # current_rv / prev_rv
    pct_jump = ctx.get("pct_jump")  # price prev→now when parsed; else scanner pct delta fallback
    fast_mover = bool(ctx.get("fast_mover"))
    recently_halted = bool(ctx.get("recently_halted"))
    news = bool(alert.get("news_headline")) or ctx.get("news_in_history")
    big_news = news and cur_rv is not None and cur_rv >= 100.0

    # ── HARD FLOAT CAP (never lift, before two-alert shortcut) ────────────
    if flt is not None and flt > config.HARD_FILTER_FLOAT_MAX:
        return False, "float_too_large"

    # ── TWO-ALERT ACCELERATION CHECK ─────────────────────────────────────
    # Alert #2 needs RV acceleration vs prior ping; alert #3+ trusts KN +
    # quality tags + higher highs — RV often stabilises while price grinds.
    # If this looks like our primary entry pattern, pass everything through
    # to Gemini — do not apply pct cap or RV floor.
    known_runner_both = "KnownRunner" in tags and "KnownRunner" in alert1_tags
    tag_union = tags + list(alert1_tags)
    quality_tag_present = any(
        t in tag_union
        for t in ("PotSqueeze", "BREAKOUT", "RegSHO", "0Borrow", "KnownRunner")
    ) or (
        "KnownRunner" in tags
        and rv_growth is not None
        and rv_growth >= 2.0  # RV doubled — quality signal without extra tags
    )
    rv_growing = rv_growth is not None and rv_growth >= 1.5
    price_higher = pct_jump is not None and pct_jump > 0

    two_alert_setup = (
        alert_number >= 2
        and known_runner_both
        and quality_tag_present
        and price_higher
        and flt is not None
        and flt < 10_000_000
        and (
            rv_growing  # alert #2 — RV acceleration required vs prior SCANNER
            or alert_number >= 3  # alert #3+ — continuation; RV stabilising is OK
        )
    )

    if two_alert_setup:
        return True, "two_alert_acceleration"

    # ── FADE DISCARD ──────────────────────────────────────────────────────
    # Price lower than previous alert = move is reversing. Kill it fast.
    if alert_number >= 2 and pct_jump is not None and pct_jump <= 0:
        return False, "fade"

    # ── Determine exception tier for pct cap and RV floor ────────────────
    elevated = fast_mover or recently_halted or big_news

    rv_floor = (
        0.0
        if fast_mover
        else (
            config.HARD_FILTER_RV_MIN_WITH_POSITIVE_NEWS
            if news
            else config.HARD_FILTER_RV_MIN
        )
    )

    # ── PCT CAP ───────────────────────────────────────────────────────────
    # Only applied on alert #1. From alert #2 onwards we trust the scorer to
    # read the situation — a stock up 80% can still give a clean continuation
    # leg, and pct_jump (alert→alert) is the real entry metric, not cumulative
    # % from yesterday's close.
    if alert_number == 1 and cur_pct is not None:
        pct_cap = (
            config.HARD_FILTER_FIRST_PCT_MAX_ELEVATED
            if elevated
            else config.HARD_FILTER_FIRST_PCT_MAX
        )
        if cur_pct > pct_cap:
            return False, "first_pct_too_late"

    # ── Headline on this alert: always reach news classifier (Haiku) ───────
    # RV snapshot is often 1x pre-volume; do not block before classification.
    if (alert.get("news_headline") or "").strip():
        return True, "news_headline_present"

    # ── RV FLOOR (pure momentum — no headline on this message) ───────────
    if not fast_mover:
        if cur_rv is None:
            return False, "no_rv"
        if cur_rv < rv_floor:
            return False, f"rv<{int(rv_floor) if rv_floor >= 1 else rv_floor}"

    # ── Passed — tag for logging ──────────────────────────────────────────
    tag = "ok"
    if fast_mover:
        tag = "fast_mover"
    elif recently_halted:
        tag = "post_halt"
    elif big_news:
        tag = "news_catalyst"
    elif alert_number >= 2:
        tag = f"alert#{alert_number}"
    return True, tag
