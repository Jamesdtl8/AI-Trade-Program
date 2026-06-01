"""Plain-English labels for audit / UI reason codes."""

from __future__ import annotations

_LABELS: dict[str, str] = {
    # Exit reasons
    "stop_loss_10pct": "Hit Stop Loss 15%",
    "tp_market": "Take Profit (market sell)",
    "trail_breach": "Trailing Stop Hit",
    "hard_stop_10pct": "Hit Hard Stop 15%",
    "market_sell": "Market Sell",
    "slots_full": "Unable due to Slots Full",
    # Grader disqualify
    "float_too_large": "Disqualified — float over 50M",
    "mc_too_large": "Disqualified — market cap over $100M",
    "price_too_low": "Disqualified — price below $0.10",
    "rv_too_low": "Disqualified — relative volume below 1x",
    "offering_present": "Disqualified — dilutive offering",
    "nbreak_at_3": "Paused — NBREAK on this alert (can resume on next momentum)",
    "nbreak_skip": "Skipped — NBREAK alert (episode continues on next momentum)",
    "no_momentum_label_at_3": "Deferred — no MOMENTUM/BREAKOUT label at alert 3",
    "price_not_higher_than_alert_2": "Deferred — price not above alert 2",
    "alert_1_accumulating": "Alert 1 — accumulating (GPT grades at alert 2)",
    "alert_2_accumulating": "Alert 2 — waiting for MOMENTUM/BREAKOUT + rising price",
    "alert_2_standard": "Alert 2 — sent to GPT grader",
    "alert_2_need_momentum_both": "Alert 2 TRADE blocked — both alerts need MOMENTUM/BREAKOUT",
    "alert_2_gap_too_long": "Alert 2 TRADE blocked — alerts too far apart (>15m)",
    "alert_2_rv_too_low": "Alert 2 TRADE blocked — RV below 50x",
    "alert_2_float_too_large": "Alert 2 TRADE blocked — float above 5M",
    "alert_2_guard": "Alert 2 TRADE blocked — wait for alert 3",
    "no_momentum_label_at_2": "Deferred — alert 2 needs MOMENTUM or BREAKOUT label",
    "price_not_higher_than_alert_1": "Deferred — price not above alert 1",
    "alert_3_standard": "Alert 3 — sent to GPT grader",
    "continuation_regrade": "Continuation — sent to GPT grader",
    "watchlist_regrade": "Watchlist regrade — prior WATCH within 24h, tape improved",
    "reentry_regrade": "Re-entry episode — sent to GPT grader (strict rules)",
    "reentry_accumulating": "Re-entry — accumulating alerts after prior trade",
    "reentry_cooldown": "Re-entry deferred — cooldown after prior exit",
    "reentry_extended": "Re-entry deferred — price too far above prior exit",
    "reentry_above_exit": "Re-entry blocked — price above prior exit (no chase)",
    "reentry_dip_unrecovered": "Re-entry deferred — dip not reclaimed",
    "reentry_no_momentum_label": "Re-entry deferred — no momentum label",
    "reentry_price_not_rising": "Re-entry deferred — price not rising vs prior alert",
    "reentry_not_ready": "Re-entry — waiting for stricter setup",
    "reentry_min_alerts": "Re-entry blocked — need 6+ alerts in new episode",
    "reentry_rv_low": "Re-entry blocked — RV below 100x",
    "reentry_guard": "Re-entry blocked — stricter rules not met",
    "continuation_watch": "Continuation — sent to GPT grader",
    "not_ready": "Waiting for more scanner alerts before GPT",
    "grader_in_flight": "GPT review in progress — will retry on next alert",
    "sell_in_flight": "Sell order in flight — waiting for broker confirmation",
    "watch_momentum_regrade": "WATCH — strong momentum regrade triggered",
    "alert_2_rv_too_low": "Alert 2 deferred — RV below 50x (not enough momentum yet)",
    "g2_partial_g3_weak_cap": "Capped at WATCH — no named news + weak squeeze structure",
    "extreme_rv_no_news_scalp": "Momentum scalp override — extreme RV + strong squeeze, no news needed",
    "news_tester_force": "News tester — forced GPT grade",
    "negative_news": "Skipped — negative news headline",
    "not_on_t212": "Filtered — not on Trading 212",
    "ai_pass": "AI Pass",
    "disqualified": "Disqualified",
    "blacklist": "Filtered — T212 blacklist",
    # AI actions
    "TRADE": "AI — Trade",
    "MONITOR": "AI — Monitor (watch)",
    "PASS": "AI — Pass",
    "SKIP": "AI — Skip",
    "WATCH": "AI — Watch",
}


def humanize(code: str | None, *, fallback: str | None = None) -> str:
    if not code:
        return fallback or "—"
    key = str(code).strip()
    if not key:
        return fallback or "—"
    if key in _LABELS:
        return _LABELS[key]
    if key.startswith("blacklist:"):
        return f"Filtered — T212 blacklist ({key.split(':', 1)[1]})"
    if key.startswith("paused:"):
        return f"Paused — {humanize(key.split(':', 1)[1], fallback=key.split(':', 1)[1])}"
    if key.startswith("trail_breach_"):
        # e.g. trail_breach_5pct_peak12.3pct → "Trailing Stop (5% trail, peak +12.3%)"
        parts = key.split("_")
        trail_part = next((p for p in parts if p.endswith("pct") and not p.startswith("peak")), None)
        peak_part = next((p for p in parts if p.startswith("peak")), None)
        trail_str = trail_part.replace("pct", "%") if trail_part else ""
        peak_str = peak_part.replace("peak", "+").replace("pct", "%") if peak_part else ""
        detail = " | ".join(filter(None, [f"trail {trail_str}", f"peak {peak_str}"]))
        return f"Trailing Stop Hit ({detail})"
    if key.startswith("sell_in_flight"):
        return "Deferred — sell order in flight (SELL_PENDING)"
    if key.startswith("watch_momentum_regrade"):
        return "WATCH — momentum regrade (high RV, in WATCH state)"
    if key.startswith("ai_error:"):
        return "AI grading error"
    if key.startswith("startup_flat_or_unfilled:"):
        return "Closed — position never filled at startup"
    if key.startswith("state_drift:"):
        return f"Closed — broker state drift ({key.split(':', 1)[1]})"
    return key.replace("_", " ").strip().title()


def humanize_list(codes: list[str] | None) -> list[str]:
    if not codes:
        return []
    return [humanize(c) for c in codes]
