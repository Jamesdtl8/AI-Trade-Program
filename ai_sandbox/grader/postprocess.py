"""Deterministic corrections after GPT grader output."""

from __future__ import annotations

import math
import re
from typing import Any

from . import hard_rules
from . import reentry
from .ai_reasoning import normalize_grade_action

MOMENTUM_LABELS = {"MOMENTUM", "BREAKOUT"}
DIP_LABELS = {"REV V", "NBREAK", "BTT V"}
KNOWN_RUNNER_TAGS = {"Known Runner", "KnownRunner"}
KNOWN_RUNNER_RV_MIN = 100.0
KNOWN_RUNNER_MOMENTUM_STREAK = 3
KNOWN_RUNNER_RECOVERY_WINDOW = 6
NEWS_MOMENTUM_RV_MIN = 90.0
NEWS_MOMENTUM_STREAK = 2
TIGHT_FLOAT_MAX = 5_000_000
ALERT_2_RV_MIN = 50.0
ALERT_2_MAX_GAP_SEC = 900.0
ALERT_2_FLOAT_MAX = 5_000_000
_NEWS_SKIP = frozenset({"none", "same", "n/a", "-", ""})


def parse_rs_ratio(reverse_split: str | None) -> int | None:
    if not reverse_split:
        return None
    m = re.match(r"1:(\d+)", str(reverse_split).strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def rs_tier(reverse_split: str | None) -> str:
    """none | flag | cap_watch | cap_watch_high"""
    ratio = parse_rs_ratio(reverse_split)
    if ratio is None:
        return "none"
    if ratio <= 5:
        return "none"
    if ratio <= 20:
        return "flag"
    if ratio <= 50:
        return "cap_watch"
    return "cap_watch_high"


def latest_reverse_split(alerts: list[dict[str, Any]]) -> str | None:
    for alert in reversed(alerts):
        rs = alert.get("reverse_split")
        if rs:
            return str(rs)
    return None


def _norm_label(label: str | None) -> str:
    u = str(label or "").strip().upper()
    if u.startswith("MOMENTUM"):
        return "MOMENTUM"
    if u.startswith("BREAKOUT"):
        return "BREAKOUT"
    return u


def consecutive_momentum_count(alerts: list[dict[str, Any]]) -> int:
    count = 0
    for alert in reversed(alerts):
        if _norm_label(alert.get("label")) in MOMENTUM_LABELS:
            count += 1
        else:
            break
    return count


def prices_strictly_increasing(alerts: list[dict[str, Any]]) -> bool:
    prev: float | None = None
    for alert in alerts:
        price = float(alert.get("price") or 0)
        if prev is not None and price <= prev:
            return False
        prev = price
    return len(alerts) >= 2


def compute_entry_target(alerts: list[dict[str, Any]]) -> tuple[float, float, float]:
    """Return (entry_price, target_price, anchor_price).

    Target is the higher of:
    - Momentum projection: anchor + (anchor - prev) * 0.5
    - 7.5% minimum profit from entry (our declared profit goal)
    """
    if not alerts:
        return 0.0, 0.0, 0.0
    n = len(alerts)
    if n >= 4:
        anchor = float(alerts[-1].get("price") or 0)
        prev = float(alerts[-2].get("price") or 0)
    elif n >= 3:
        anchor = float(alerts[2].get("price") or 0)
        prev = float(alerts[1].get("price") or 0)
    else:
        anchor = float(alerts[-1].get("price") or 0)
        prev = float(alerts[-2].get("price") or anchor) if n >= 2 else anchor
    entry = round(anchor * 1.03, 2)
    momentum_target = anchor + (anchor - prev) * 0.5
    # Always round UP the minimum so we never target < 7.5% from entry
    min_target = math.ceil(entry * 1.075 * 100) / 100
    target = round(max(momentum_target, min_target), 2)
    return entry, target, anchor


def _gate_passes(gate_val: str | None, *, allow_partial: bool = False) -> bool:
    g = str(gate_val or "").upper()
    if g.startswith("PASS"):
        return True
    return allow_partial and g == "PARTIAL"


def momentum_override_applies(alerts: list[dict[str, Any]], gates: dict[str, Any]) -> bool:
    """4+ consecutive MOMENTUM/BREAKOUT, rising prices, RV>100x — no gate_4 block."""
    if len(alerts) < 4:
        return False
    if consecutive_momentum_count(alerts) < 4:
        return False
    if not prices_strictly_increasing(alerts):
        return False
    rv_min = continuation_momentum_rv_min(gates)
    if float(alerts[-1].get("rv") or 0) < rv_min:
        return False
    if not _gate_passes(gates.get("gate_1")):
        return False
    if not (_gate_passes(gates.get("gate_2")) or _gate_passes(gates.get("gate_2"), allow_partial=True)):
        return False
    return True


def continuation_momentum_rv_min(gates: dict[str, Any]) -> float:
    """4+ alert continuation: slightly lower RV bar when news catalyst is present."""
    g2 = str(gates.get("gate_2") or "").upper()
    if g2.startswith("PASS"):
        return NEWS_MOMENTUM_RV_MIN
    return 100.0


def _alert_tags(alert: dict[str, Any]) -> set[str]:
    tags = set(alert.get("indicators") or [])
    for t in alert.get("tags") or []:
        tags.add(str(t))
    return tags


def has_known_runner(alerts: list[dict[str, Any]]) -> bool:
    for alert in alerts:
        if _alert_tags(alert).intersection(KNOWN_RUNNER_TAGS):
            return True
    return False


def rising_momentum_streak(alerts: list[dict[str, Any]], *, n: int) -> bool:
    if len(alerts) < n:
        return False
    window = alerts[-n:]
    prev: float | None = None
    for alert in window:
        if _norm_label(alert.get("label")) not in MOMENTUM_LABELS:
            return False
        price = float(alert.get("price") or 0)
        if price <= 0 or (prev is not None and price <= prev):
            return False
        prev = price
    return True


def _momentum_prices(alerts: list[dict[str, Any]]) -> list[float]:
    prices: list[float] = []
    for alert in alerts:
        if _norm_label(alert.get("label")) not in MOMENTUM_LABELS:
            continue
        price = float(alert.get("price") or 0)
        if price > 0:
            prices.append(price)
    return prices


def rising_momentum_streak_skip_dips(
    alerts: list[dict[str, Any]],
    *,
    n: int,
    window: int = KNOWN_RUNNER_RECOVERY_WINDOW,
) -> bool:
    """Last N MOMENTUM/BREAKOUT prices in a window, ignoring REV V/NBREAK between them."""
    if len(alerts) < n:
        return False
    momentum_prices = _momentum_prices(alerts[-window:])
    if len(momentum_prices) < n:
        return False
    tail = momentum_prices[-n:]
    prev: float | None = None
    for price in tail:
        if prev is not None and price <= prev:
            return False
        prev = price
    return True


def has_recent_dip_label(alerts: list[dict[str, Any]], *, window: int = KNOWN_RUNNER_RECOVERY_WINDOW) -> bool:
    for alert in alerts[-window:]:
        label = _norm_label(alert.get("label"))
        if label in DIP_LABELS:
            return True
    return False


def price_recovered_above_pre_dip_peak(
    alerts: list[dict[str, Any]],
    *,
    window: int = KNOWN_RUNNER_RECOVERY_WINDOW,
) -> bool:
    """Current price must exceed the high printed before the latest REV V/NBREAK dip."""
    if len(alerts) < 2:
        return False
    recent = alerts[-window:]
    dip_idx: int | None = None
    for idx, alert in enumerate(recent):
        if _norm_label(alert.get("label")) in DIP_LABELS:
            dip_idx = idx
            break
    if dip_idx is None:
        return True
    pre_dip = recent[:dip_idx]
    if not pre_dip:
        return float(alerts[-1].get("price") or 0) > 0
    peak = max(float(a.get("price") or 0) for a in pre_dip)
    current = float(alerts[-1].get("price") or 0)
    return current > peak > 0


def known_runner_momentum_qualifies(alerts: list[dict[str, Any]]) -> bool:
    """Strict 3-alert streak, or post-REV V recovery with 3 rising momentum prints."""
    if rising_momentum_streak(alerts, n=KNOWN_RUNNER_MOMENTUM_STREAK):
        return True
    if not has_recent_dip_label(alerts):
        return False
    if _norm_label(alerts[-1].get("label")) not in MOMENTUM_LABELS:
        return False
    if not hard_rules.price_rising_vs_prior(alerts):
        return False
    if not rising_momentum_streak_skip_dips(
        alerts,
        n=KNOWN_RUNNER_MOMENTUM_STREAK,
        window=KNOWN_RUNNER_RECOVERY_WINDOW,
    ):
        return False
    return price_recovered_above_pre_dip_peak(alerts, window=KNOWN_RUNNER_RECOVERY_WINDOW)


def _gate_pass_strong(gate_val: str | None) -> bool:
    return str(gate_val or "").upper().startswith("PASS_STRONG")


def _tight_float(alerts: list[dict[str, Any]]) -> bool:
    for alert in reversed(alerts):
        fl = alert.get("float_shares") if alert.get("float_shares") is not None else alert.get("float")
        if fl is not None:
            return float(fl) < TIGHT_FLOAT_MAX
    return False


def has_named_news_in_alerts(alerts: list[dict[str, Any]]) -> bool:
    """True when alert history contains a real headline (not 'none' / empty)."""
    for alert in alerts:
        for key in ("news", "news_headline"):
            raw = str(alert.get(key) or "").strip()
            if raw and raw.lower() not in _NEWS_SKIP:
                return True
    return False


def _latest_float(alerts: list[dict[str, Any]]) -> float | None:
    for alert in reversed(alerts):
        fl = alert.get("float_shares") if alert.get("float_shares") is not None else alert.get("float")
        if fl is not None:
            return float(fl)
    return None


def alert_2_trade_allowed(alerts: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Alert #2 TRADE requires tight float, strong RV, both momentum labels, short gap."""
    if len(alerts) != 2:
        return True, None
    a1, a2 = alerts[0], alerts[1]
    if _norm_label(a1.get("label")) not in MOMENTUM_LABELS:
        return False, "alert_2_need_momentum_both"
    if _norm_label(a2.get("label")) not in MOMENTUM_LABELS:
        return False, "alert_2_need_momentum_both"
    ts1 = float(a1.get("ts") or 0.0)
    ts2 = float(a2.get("ts") or 0.0)
    if ts1 > 0 and ts2 > 0 and (ts2 - ts1) > ALERT_2_MAX_GAP_SEC:
        return False, "alert_2_gap_too_long"
    if float(a2.get("rv") or 0.0) < ALERT_2_RV_MIN:
        return False, "alert_2_rv_too_low"
    fl = _latest_float(alerts)
    if fl is not None and fl > ALERT_2_FLOAT_MAX:
        return False, "alert_2_float_too_large"
    return True, None


def news_momentum_override_applies(
    alerts: list[dict[str, Any]],
    gates: dict[str, Any],
) -> bool:
    """Tight float + named news + RV≥90 + 2 rising momentum — Gate 3 FAIL does not block TRADE."""
    if len(alerts) < 3:
        return False
    if not has_named_news_in_alerts(alerts):
        return False
    g3 = str(gates.get("gate_3") or "").upper()
    if g3.startswith("PASS"):
        return False
    if not (_gate_pass_strong(gates.get("gate_1")) or _tight_float(alerts)):
        return False
    g2 = str(gates.get("gate_2") or "").upper()
    if not g2.startswith("PASS"):
        return False
    if not _gate_passes(gates.get("gate_4")):
        return False
    if float(alerts[-1].get("rv") or 0) < NEWS_MOMENTUM_RV_MIN:
        return False
    if consecutive_momentum_count(alerts) < NEWS_MOMENTUM_STREAK:
        return False
    if not rising_momentum_streak(alerts, n=NEWS_MOMENTUM_STREAK):
        return False
    return True


def known_runner_override_applies(alerts: list[dict[str, Any]], gates: dict[str, Any]) -> bool:
    """Known Runner + RV>100 + rising momentum — Gate 2 FAIL or PARTIAL does not block TRADE.

    NOTE: Gate 2 is auto-upgraded FAIL→PARTIAL when Gate 1 is PASS_STRONG and Gate 3 is PASS,
    so we accept PARTIAL here — the override was always designed for setups without real news.
    """
    if len(alerts) < KNOWN_RUNNER_MOMENTUM_STREAK:
        return False
    if not has_known_runner(alerts):
        return False
    if float(alerts[-1].get("rv") or 0) < KNOWN_RUNNER_RV_MIN:
        return False
    if not known_runner_momentum_qualifies(alerts):
        return False
    if not _gate_passes(gates.get("gate_1"), allow_partial=True):
        return False
    if not _gate_passes(gates.get("gate_4"), allow_partial=True):
        return False
    g2 = str(gates.get("gate_2") or "").upper()
    # Accept FAIL or PARTIAL — real PASS (named news catalyst) is not required by Known Runner logic.
    # Gate 2 auto-upgrade (FAIL→PARTIAL) for tight float + squeeze flags must not defeat this override.
    return not g2.startswith("PASS")


def apply_rules(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Apply tiered R/S, continuation pricing, and momentum override."""
    scanner_tk = str(state.get("ticker") or "")
    state = reentry.sync_reentry_state(state, scanner_ticker=scanner_tk)
    out = dict(result)
    alerts: list[dict[str, Any]] = list(state.get("alerts") or [])
    alert_count = len(alerts)
    ctx = dict(out.get("context") or {})
    gates = dict(ctx.get("gates") or {})
    rs = latest_reverse_split(alerts)
    tier = rs_tier(rs)

    flags = list(out.get("risk_flags") or ctx.get("risk_flags") or [])
    if tier == "flag" and rs:
        note = f"R/S {rs} (modest ratio — noted, not capped)"
        if note not in flags:
            flags.append(note)
    if tier in ("cap_watch", "cap_watch_high") and rs:
        note = f"R/S {rs} (high ratio — cap WATCH)"
        if note not in flags:
            flags.append(note)

    entry, target, anchor = compute_entry_target(alerts)
    out["entry_price"] = entry
    out["target_price"] = target
    ctx["realistic_entry"] = entry
    if alert_count >= 4:
        ctx["current_alert_price"] = round(anchor, 2)
    if alert_count >= 3:
        ctx["alert_3_price"] = round(float(alerts[2].get("price") or anchor), 2)
        ctx["alert_3_rv"] = alerts[2].get("rv")
    elif alert_count == 2:
        ctx["alert_3_price"] = round(float(alerts[1].get("price") or anchor), 2)
        ctx["alert_3_rv"] = alerts[1].get("rv")

    if alerts:
        ctx["highest_price_seen"] = round(
            max(float(a.get("price") or 0) for a in alerts),
            2,
        )
        ctx["highest_rv_seen"] = max(float(a.get("rv") or 0) for a in alerts)
        ctx["price_sequence"] = [round(float(a.get("price") or 0), 2) for a in alerts]
        ctx["rv_sequence"] = [float(a.get("rv") or 0) for a in alerts]

    rv_gate, rv_note = hard_rules.evaluate_rv_velocity(alerts)
    g4 = str(gates.get("gate_4") or "").upper()
    if rv_gate == "PASS" and not g4.startswith("PASS"):
        gates["gate_4"] = "PASS"
        if rv_note and rv_note not in flags:
            flags.append(rv_note)
    elif rv_gate == "PARTIAL" and g4 == "FAIL":
        gates["gate_4"] = "PARTIAL"
        if rv_note and rv_note not in flags:
            flags.append(rv_note)
    elif rv_gate == "FAIL" and rv_note == "rv_collapse" and g4 not in ("FAIL",):
        gates["gate_4"] = "FAIL"
        note = "RV collapse — drop too large vs prior/peak"
        if note not in flags:
            flags.append(note)
    ctx["gates"] = gates

    grade = str(out.get("grade") or "SKIP").upper()
    action = str(out.get("action") or "PASS").upper()
    prior_grade = str(ctx.get("current_grade") or grade).upper()
    rs_caps = tier in ("cap_watch", "cap_watch_high")
    kr_override = known_runner_override_applies(alerts, gates)
    # 1:51+ still blocks auto-promote; 1:21–1:50 waived when Known Runner qualifies.
    rs_blocks_auto_override = tier == "cap_watch_high"
    rs_caps_trade = tier == "cap_watch_high" or (tier == "cap_watch" and not kr_override)

    if kr_override and not rs_blocks_auto_override and not reentry.is_reentry_episode(
        state, scanner_ticker=scanner_tk
    ):
        note = (
            "Known Runner continuation — Gate 2 news override "
            "(3 rising momentum or post-REV V recovery, RV>100x)"
        )
        if tier == "cap_watch" and rs:
            note = (
                f"Known Runner continuation — R/S {rs} cap waived "
                "(3 rising momentum or post-REV V recovery, RV>100x)"
            )
        if note not in flags:
            flags.append(note)
        gates["gate_2"] = "PARTIAL"
        if grade != "STRONG" or action != "TRADE":
            history = list(ctx.get("grade_change_history") or [])
            history.append(
                {
                    "from": prior_grade,
                    "to": "STRONG",
                    "reason": "known_runner_continuation_override",
                }
            )
            ctx["grade_change_history"] = history
        grade = "STRONG"
        action = "TRADE"
    elif news_momentum_override_applies(alerts, gates) and not rs_blocks_auto_override and not reentry.is_reentry_episode(
        state, scanner_ticker=scanner_tk
    ):
        note = (
            "News momentum override — tight float + catalyst + RV≥90x "
            "(Gate 3 squeeze flags not required)"
        )
        if note not in flags:
            flags.append(note)
        gates["gate_3"] = "PARTIAL"
        if grade != "STRONG" or action != "TRADE":
            history = list(ctx.get("grade_change_history") or [])
            history.append(
                {
                    "from": prior_grade,
                    "to": "STRONG",
                    "reason": "news_momentum_override",
                }
            )
            ctx["grade_change_history"] = history
        grade = "STRONG"
        action = "TRADE"
    elif momentum_override_applies(alerts, gates) and not rs_blocks_auto_override and not reentry.is_reentry_episode(
        state, scanner_ticker=scanner_tk
    ):
        if grade != "STRONG" or action != "TRADE":
            history = list(ctx.get("grade_change_history") or [])
            history.append(
                {
                    "from": prior_grade,
                    "to": "STRONG",
                    "reason": "momentum_override_4plus",
                }
            )
            ctx["grade_change_history"] = history
        grade = "STRONG"
        action = "TRADE"
    elif rs_caps_trade and grade == "STRONG":
        grade = "WATCH"
        if action == "TRADE":
            action = "MONITOR"
    elif (
        grade == "SKIP"
        and rv_gate in ("PASS", "PARTIAL")
        and hard_rules.latest_has_momentum(alerts)
        and hard_rules.price_rising_vs_prior(alerts)
        and _gate_passes(gates.get("gate_1"))
        and _gate_passes(gates.get("gate_3"))
        and _gate_passes(gates.get("gate_4"), allow_partial=True)
        and not rs_caps
    ):
        history = list(ctx.get("grade_change_history") or [])
        if rv_gate == "PASS" and _gate_passes(gates.get("gate_2"), allow_partial=True):
            grade = "STRONG"
            action = "TRADE"
            history.append({"from": prior_grade, "to": "STRONG", "reason": "rv_momentum_tolerance"})
        else:
            grade = "WATCH"
            action = "MONITOR"
            history.append({"from": prior_grade, "to": "WATCH", "reason": "rv_momentum_tolerance"})
        ctx["grade_change_history"] = history

    grade, action, mismatch = normalize_grade_action(grade, action, gates=gates)
    if mismatch:
        history = list(ctx.get("grade_change_history") or [])
        history.append(
            {
                "from": prior_grade,
                "to": grade,
                "reason": "grade_action_normalized",
            }
        )
        ctx["grade_change_history"] = history
        note = mismatch
        if note not in flags:
            flags.append(note)

    if action == "TRADE" and alert_count == 2 and not reentry.is_reentry_episode(
        state, scanner_ticker=scanner_tk
    ):
        allowed2, block2 = alert_2_trade_allowed(alerts)
        if not allowed2:
            note = {
                "alert_2_need_momentum_both": (
                    "Alert 2 TRADE blocked — both alerts need MOMENTUM/BREAKOUT labels"
                ),
                "alert_2_gap_too_long": (
                    "Alert 2 TRADE blocked — gap between alerts exceeds 15 minutes"
                ),
                "alert_2_rv_too_low": (
                    f"Alert 2 TRADE blocked — RV below {ALERT_2_RV_MIN:.0f}x (wait for alert 3)"
                ),
                "alert_2_float_too_large": (
                    f"Alert 2 TRADE blocked — float above {ALERT_2_FLOAT_MAX / 1_000_000:.0f}M"
                ),
            }.get(block2 or "", "Alert 2 TRADE blocked — criteria not met")
            if note not in flags:
                flags.append(note)
            history = list(ctx.get("grade_change_history") or [])
            history.append(
                {
                    "from": grade,
                    "to": "WATCH",
                    "reason": block2 or "alert_2_guard",
                }
            )
            ctx["grade_change_history"] = history
            grade = "WATCH"
            action = "MONITOR"

    if action == "TRADE" and reentry.is_reentry_episode(state, scanner_ticker=scanner_tk):
        allowed, block_code = reentry.reentry_trade_allowed(
            state,
            alerts,
            scanner_ticker=scanner_tk,
        )
        if not allowed:
            note = reentry.reentry_block_note(block_code)
            if note not in flags:
                flags.append(note)
            history = list(ctx.get("grade_change_history") or [])
            history.append(
                {
                    "from": grade,
                    "to": "WATCH",
                    "reason": block_code or "reentry_guard",
                }
            )
            ctx["grade_change_history"] = history
            grade = "WATCH"
            action = "MONITOR"

    ctx["reverse_split"] = rs
    ctx["alert_count"] = alert_count
    ctx["current_grade"] = grade
    if not ctx.get("initial_grade"):
        ctx["initial_grade"] = grade
    ctx["risk_flags"] = flags
    out["grade"] = grade
    out["action"] = action
    out["risk_flags"] = flags
    out["context"] = ctx
    from .ai_reasoning import align_summary_to_outcome

    align_summary_to_outcome(out, grade, action)
    return out
