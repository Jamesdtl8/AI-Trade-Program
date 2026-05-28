"""Python hard rules before AI grading (New System spec)."""

from __future__ import annotations

from typing import Any

FLOAT_LIMIT = 50_000_000
MC_LIMIT = 100_000_000
PRICE_MIN = 0.10
RV_MIN = 1.0
EARLY_PROMOTION_FLOAT_LIMIT = 2_000_000
EARLY_PROMOTION_RV_MIN = 50.0
SQUEEZE_INDICATORS = {"0 Borrow", "Reg SHO", "Potential Squeeze", "0Borrow", "RegSHO", "PotSqueeze"}
MOMENTUM_LABELS = {"MOMENTUM", "BREAKOUT"}


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
    return label.strip()


def hard_disqualify(alert: dict[str, Any]) -> tuple[bool, str | None]:
    flt = alert.get("float")
    mc = alert.get("market_cap")
    price = alert.get("price")
    rv = alert.get("rv")
    rank = alert.get("rank") or alert.get("alert_number")
    label = _norm_label(alert.get("label"))
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
    if rank is not None and int(rank) >= 3 and label == "NBREAK":
        return True, "nbreak_at_3"
    return False, None


def should_send_to_ai(state: dict[str, Any], alert: dict[str, Any]) -> tuple[bool, str]:
    alerts: list[dict[str, Any]] = state.get("alerts") or []
    alert_count = len(alerts)
    label = _norm_label(alert.get("label"))
    price = float(alert.get("price") or 0.0)
    rv = alert.get("rv")
    st = str(state.get("state") or "NEW")

    if alert_count == 3:
        if label not in MOMENTUM_LABELS:
            return False, "no_momentum_label_at_3"
        alert_2 = alerts[1]
        if price <= float(alert_2.get("price") or 0.0):
            return False, "price_not_higher_than_alert_2"
        return True, "alert_3_standard"

    if alert_count == 2:
        alert_1 = alerts[0]
        float_val = state.get("float_shares") or alert.get("float")
        float_ok = float_val is not None and float(float_val) < EARLY_PROMOTION_FLOAT_LIMIT
        indicators = set(alert.get("indicators") or [])
        tags = set(alert.get("tags") or [])
        squeeze_ok = bool(SQUEEZE_INDICATORS.intersection(indicators | tags))
        label_ok = label in MOMENTUM_LABELS
        price_ok = price > float(alert_1.get("price") or 0.0)
        rv_ok = rv is not None and float(rv) >= EARLY_PROMOTION_RV_MIN
        if float_ok and squeeze_ok and label_ok and price_ok and rv_ok:
            return True, "alert_2_early_promotion"
        return False, "alert_2_accumulating"

    if alert_count == 1:
        return False, "alert_1_accumulating"

    if alert_count >= 4 and st == "WATCH":
        return True, "continuation_watch"

    return False, "not_ready"
