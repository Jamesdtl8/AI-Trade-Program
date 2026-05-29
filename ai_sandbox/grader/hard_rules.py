"""Python hard rules before AI grading (New System spec)."""

from __future__ import annotations

from typing import Any

from .. import db

FLOAT_LIMIT = 50_000_000
REVIEW_REGRADE_WINDOW_SEC = 24 * 3600
REGRADE_ELIGIBLE_STATES = frozenset({"WATCH", "PASS", "WATCHING", "PENDING_AI"})
MC_LIMIT = 100_000_000
PRICE_MIN = 0.10
RV_MIN = 5.0  # Raised from 1x — sub-5x is noise, not momentum
MOMENTUM_LABELS = {"MOMENTUM", "BREAKOUT"}
WEAK_LABELS = {"NBREAK", "REV V"}
SQUEEZE_TAGS = {
    "0Borrow",
    "0 Borrow",
    "RegSHO",
    "Reg SHO",
    "PotSqueeze",
    "Potential Squeeze",
}

# RV velocity tolerances — allow modest pullbacks when momentum + price are intact.
RV_STEP_PASS_RATIO = 0.75
RV_STEP_PARTIAL_RATIO = 0.55
RV_PEAK_PARTIAL_RATIO = 0.40
RV_STEP_COLLAPSE_RATIO = 0.45
RV_PEAK_COLLAPSE_RATIO = 0.35
RV_ABSOLUTE_FLOOR = 25.0
LABEL_OVERRIDE_RV = 100.0
LABEL_OVERRIDE_PCT = 40.0
LABEL_OVERRIDE_SQUEEZE_RV = 50.0
ALERT_2_RV_MIN = 50.0  # Minimum RV to send alert-2 to AI — below this it's not a real mover

PERMANENT_DISQUALIFY = frozenset(
    {
        "float_too_large",
        "mc_too_large",
        "price_too_low",
        "rv_too_low",
        "offering_present",
        "negative_news",
        "not_on_t212",
    }
)


def _norm_label(label: str | None) -> str | None:
    if not label:
        return None
    u = label.strip().upper()
    if u.startswith("MOMENTUM"):
        return "MOMENTUM"
    if u.startswith("BREAKOUT"):
        return "BREAKOUT"
    if u.startswith("NBREAK"):
        return "NBREAK"
    if u.startswith("REV"):
        return "REV V"
    if u.startswith("BTT"):
        return "BTT V"
    return label.strip()


def _alert_tags(alert: dict[str, Any]) -> set[str]:
    tags = set(alert.get("tags") or [])
    for ind in alert.get("indicators") or []:
        tags.add(str(ind))
    return tags


def is_nbreak_event(alert: dict[str, Any]) -> bool:
    rank = alert.get("rank") or alert.get("alert_number")
    label = _norm_label(alert.get("label"))
    return rank is not None and int(rank) >= 3 and label == "NBREAK"


def is_recoverable_disqualify(reason: str | None) -> bool:
    if not reason:
        return False
    key = str(reason).strip()
    return key == "nbreak_at_3" or key.startswith("nbreak_")


def hard_disqualify(alert: dict[str, Any]) -> tuple[bool, str | None]:
    flt = alert.get("float")
    mc = alert.get("market_cap")
    price = alert.get("price")
    rv = alert.get("rv")
    atype = alert.get("type")

    if flt is not None and float(flt) > FLOAT_LIMIT:
        return True, "float_too_large"
    if mc is not None and float(mc) > MC_LIMIT:
        return True, "mc_too_large"
    if price is not None and float(price) < PRICE_MIN:
        return True, "price_too_low"
    if rv is not None and float(rv) < RV_MIN:
        return True, "rv_too_low"
    if atype == "OFFERING" or alert.get("has_offering"):
        return True, "offering_present"
    return False, None


def label_ok_for_grade(alert: dict[str, Any], alerts: list[dict[str, Any]]) -> bool:
    """MOMENTUM/BREAKOUT, or strong RV/squeeze move without a formal label."""
    label = _norm_label(alert.get("label"))
    if label in MOMENTUM_LABELS:
        return True
    if label in WEAK_LABELS or (label and str(label).upper().startswith("BTT")):
        return False

    if len(alerts) < 2:
        return False
    price = float(alert.get("price") or alerts[-1].get("price") or 0)
    prev = float(alerts[-2].get("price") or 0)
    if price <= prev or price <= 0:
        return False

    rv = float(alert.get("rv") or alerts[-1].get("rv") or 0)
    pct = float(alert.get("pct") or alert.get("change_pct") or alerts[-1].get("change_pct") or 0)
    tags = _alert_tags(alert)
    if tags.intersection(SQUEEZE_TAGS) and rv >= LABEL_OVERRIDE_SQUEEZE_RV:
        return True
    if rv >= LABEL_OVERRIDE_RV:
        return True
    if pct >= LABEL_OVERRIDE_PCT:
        return True
    return False


def _rv_values(alerts: list[dict[str, Any]]) -> list[float]:
    return [float(a.get("rv") or 0) for a in alerts]


def price_rising_vs_prior(alerts: list[dict[str, Any]]) -> bool:
    if len(alerts) < 2:
        return False
    cur = float(alerts[-1].get("price") or 0)
    prev = float(alerts[-2].get("price") or 0)
    return cur > prev > 0


def latest_has_momentum(alerts: list[dict[str, Any]]) -> bool:
    if not alerts:
        return False
    return _norm_label(alerts[-1].get("label")) in MOMENTUM_LABELS


def velocity_context_ok(alerts: list[dict[str, Any]], alert: dict[str, Any] | None = None) -> bool:
    probe = alert or (alerts[-1] if alerts else {})
    return latest_has_momentum(alerts) or label_ok_for_grade(probe, alerts)


def evaluate_rv_velocity(alerts: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Return (gate_4_level, note) where level is PASS | PARTIAL | FAIL."""
    if len(alerts) < 2:
        return "FAIL", None

    rvs = _rv_values(alerts)
    cur_rv = rvs[-1]
    prev_rv = rvs[-2]
    peak_rv = max(rvs) if rvs else 0.0
    momentum = velocity_context_ok(alerts)
    price_up = price_rising_vs_prior(alerts)

    if not momentum or not price_up:
        if len(alerts) >= 2 and prev_rv > 0 and cur_rv < prev_rv:
            return "FAIL", "rv_down_without_momentum"
        if len(alerts) >= 2 and prev_rv > 0 and cur_rv >= prev_rv:
            return "PASS" if momentum and price_up else "PARTIAL", None
        return "FAIL", None

    if prev_rv <= 0 or peak_rv <= 0:
        return "PARTIAL" if cur_rv >= RV_ABSOLUTE_FLOOR else "FAIL", "rv_missing"

    step_ratio = cur_rv / prev_rv
    peak_ratio = cur_rv / peak_rv

    if step_ratio < RV_STEP_COLLAPSE_RATIO or peak_ratio < RV_PEAK_COLLAPSE_RATIO:
        return "FAIL", "rv_collapse"
    if cur_rv < RV_ABSOLUTE_FLOOR and step_ratio < RV_STEP_PARTIAL_RATIO:
        return "FAIL", "rv_floor"

    if step_ratio >= RV_STEP_PASS_RATIO:
        note = None
        if step_ratio < 1.0:
            note = f"RV modest pullback tolerated ({cur_rv:.0f}x vs {prev_rv:.0f}x prior)"
        return "PASS", note

    if step_ratio >= RV_STEP_PARTIAL_RATIO and peak_ratio >= RV_PEAK_PARTIAL_RATIO:
        return "PARTIAL", (
            f"RV modest pullback tolerated ({cur_rv:.0f}x vs {prev_rv:.0f}x prior, "
            f"{peak_ratio:.0%} of peak {peak_rv:.0f}x)"
        )

    return "FAIL", "rv_drop_too_large"


def is_positive_ai_review(row: dict[str, Any] | None) -> bool:
    """True when the last GPT outcome was constructive (WATCH/MONITOR), not PASS/SKIP."""
    if not row:
        return False
    action = str(row.get("action") or "").upper()
    grade = str(row.get("grade") or "").upper()
    if action == "PASS" or grade == "SKIP":
        return False
    if action == "MONITOR" and grade in ("WATCH", "STRONG"):
        return True
    if action == "TRADE" and grade == "STRONG":
        return True
    return False


def positive_tape_for_regrade(
    alerts: list[dict[str, Any]],
    alert: dict[str, Any],
    last_review: dict[str, Any],
) -> bool:
    """Tape improved since the last positive GPT review — worth a fresh grade."""
    last_n = int(last_review.get("alert_number") or 0)
    if last_n <= 0 or len(alerts) <= last_n:
        return False

    reviewed_px = float(alerts[last_n - 1].get("price") or 0)
    cur_px = float(alert.get("price") or alerts[-1].get("price") or 0)

    if label_ok_for_grade(alert, alerts) and price_rising_vs_prior(alerts):
        return True
    if reviewed_px > 0 and cur_px > reviewed_px:
        return True
    if reviewed_px > 0 and cur_px >= reviewed_px * 0.92:
        if latest_has_momentum(alerts) or label_ok_for_grade(alert, alerts):
            return True
    rv = float(alert.get("rv") or alerts[-1].get("rv") or 0)
    if rv >= LABEL_OVERRIDE_RV and price_rising_vs_prior(alerts):
        return True
    return False


def watchlist_regrade_ready(
    state: dict[str, Any],
    alert: dict[str, Any],
    *,
    within_sec: float = REVIEW_REGRADE_WINDOW_SEC,
) -> tuple[bool, str | None]:
    """Send back to GPT when a prior positive review exists and tape has moved on."""
    ticker = str(state.get("ticker") or "").strip().upper()
    if not ticker:
        return False, None
    st = str(state.get("state") or "NEW")
    if st not in REGRADE_ELIGIBLE_STATES:
        return False, None

    last_review = db.last_ai_decision_for_ticker(ticker, within_sec=within_sec)
    if not is_positive_ai_review(last_review):
        return False, None

    alerts: list[dict[str, Any]] = state.get("alerts") or []
    last_n = int((last_review or {}).get("alert_number") or 0)
    if len(alerts) <= last_n:
        return False, None

    if positive_tape_for_regrade(alerts, alert, last_review):
        return True, "watchlist_regrade"
    return False, None


def should_send_to_ai(state: dict[str, Any], alert: dict[str, Any]) -> tuple[bool, str]:
    from . import reentry

    alerts: list[dict[str, Any]] = state.get("alerts") or []
    alert_count = len(alerts)
    price = float(alert.get("price") or 0.0)
    st = str(state.get("state") or "NEW")

    ready, why = watchlist_regrade_ready(state, alert)
    if ready:
        return True, why or "watchlist_regrade"

    if reentry.is_reentry_episode(state, scanner_ticker=state.get("ticker")):
        blocked, block_reason = reentry.reentry_send_block(
            state,
            alerts,
            alert,
            scanner_ticker=state.get("ticker"),
        )
        if blocked:
            return False, block_reason or "reentry_not_ready"
        if alert_count >= reentry.REENTRY_MIN_ALERTS and st in ("WATCH", "PASS", "WATCHING", "NEW"):
            if label_ok_for_grade(alert, alerts) and price_rising_vs_prior(alerts):
                return True, "reentry_regrade"
        return False, "reentry_not_ready"

    if alert_count >= 4 and st in REGRADE_ELIGIBLE_STATES:
        if label_ok_for_grade(alert, alerts) and price_rising_vs_prior(alerts):
            return True, "continuation_regrade"
        # In WATCH state, re-evaluate every strong MOMENTUM/BREAKOUT print even on a micro-dip —
        # the system is already watching; don't miss a re-acceleration by skipping a 1-tick pullback.
        if st == "WATCH":
            norm_label = str(alert.get("label") or "").strip().upper()
            is_momentum = norm_label.startswith("MOMENTUM") or norm_label.startswith("BREAKOUT")
            rv_val = float(alert.get("rv") or 0)
            cur_px = float(alerts[-1].get("price") or 0)
            prev_px = float(alerts[-2].get("price") or 0) if len(alerts) >= 2 else 0
            price_not_collapsed = prev_px <= 0 or cur_px >= prev_px * 0.95
            if is_momentum and rv_val >= LABEL_OVERRIDE_RV and price_not_collapsed:
                return True, "watch_momentum_regrade"
        return False, "not_ready"

    if alert_count == 3:
        if not label_ok_for_grade(alert, alerts):
            return False, "no_momentum_label_at_3"
        alert_2 = alerts[1]
        if price <= float(alert_2.get("price") or 0.0):
            return False, "price_not_higher_than_alert_2"
        return True, "alert_3_standard"

    if alert_count == 2:
        alert_1 = alerts[0]
        if not label_ok_for_grade(alert, alerts):
            return False, "no_momentum_label_at_2"
        if price <= float(alert_1.get("price") or 0.0):
            return False, "price_not_higher_than_alert_1"
        # Require minimum RV at alert 2 — low-RV setups don't have the buying pressure
        # needed to reach a 7.5% target. Below 50x is noise, not momentum.
        rv_at_2 = float(alert.get("rv") or 0.0)
        if rv_at_2 < ALERT_2_RV_MIN:
            return False, "alert_2_rv_too_low"
        return True, "alert_2_standard"

    if alert_count == 1:
        return False, "alert_1_accumulating"

    return False, "not_ready"
