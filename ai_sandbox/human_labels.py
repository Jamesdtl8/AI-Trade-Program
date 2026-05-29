"""Plain-English labels for audit / UI reason codes."""

from __future__ import annotations

_LABELS: dict[str, str] = {
    # Exit reasons
    "stop_loss_10pct": "Hit Stop Loss 10%",
    "tp_market": "Take Profit (market sell)",
    "trail_breach": "Trailing Stop Hit",
    "hard_stop_10pct": "Hit Hard Stop 10%",
    "market_sell": "Market Sell",
    "slots_full": "Unable due to Slots Full",
    # Grader disqualify
    "float_too_large": "Disqualified — float over 50M",
    "mc_too_large": "Disqualified — market cap over $100M",
    "price_too_low": "Disqualified — price below $0.10",
    "rv_too_low": "Disqualified — relative volume below 1x",
    "offering_present": "Disqualified — dilutive offering",
    "nbreak_at_3": "Disqualified — NBREAK at alert 3+",
    "no_momentum_label_at_3": "Deferred — no MOMENTUM/BREAKOUT label at alert 3",
    "price_not_higher_than_alert_2": "Deferred — price not above alert 2",
    "alert_1_accumulating": "Alert 1 — accumulating (GPT grades at alert 3)",
    "alert_2_accumulating": "Alert 2 — accumulating (GPT grades at alert 3)",
    "alert_2_early_promotion": "Alert 2 — early promotion, sent to GPT",
    "alert_3_standard": "Alert 3 — sent to GPT grader",
    "continuation_watch": "Continuation watch — sent to GPT",
    "not_ready": "Waiting for more scanner alerts before GPT",
    "news_tester_force": "News tester — forced GPT grade",
    "negative_news": "Skipped — negative news headline",
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
