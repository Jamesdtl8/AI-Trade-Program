"""Same-day re-entry guardrails after a closed trade."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .. import ticker_identity
from . import hard_rules

REENTRY_MIN_ALERTS = 6
REENTRY_COOLDOWN_SEC = 20 * 60
REENTRY_RV_MIN = 100.0
REENTRY_PRICE_EXIT_MULT = 1.5
REENTRY_DIP_WINDOW = 6
REENTRY_MOMENTUM_STREAK = 3
DIP_LABELS = {"REV V", "NBREAK", "BTT V"}
MOMENTUM_LABELS = {"MOMENTUM", "BREAKOUT"}


def _norm_label(label: str | None) -> str:
    u = str(label or "").strip().upper()
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
    return u


def _scanner_ticker(state: dict[str, Any], scanner_ticker: str | None = None) -> str:
    return ticker_identity.normalize_scanner(scanner_ticker or state.get("ticker"))


def prior_trade_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    pt = state.get("prior_trade")
    if isinstance(pt, dict) and pt.get("exit_ts"):
        return pt
    raw = state.get("prior_trade_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw if raw.get("exit_ts") else None
    try:
        import json

        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) and parsed.get("exit_ts") else None
    except Exception:
        return None


def prior_trade_from_db(scanner_ticker: str | None) -> dict[str, Any] | None:
    from .. import db

    tk = ticker_identity.normalize_scanner(scanner_ticker)
    if not tk:
        return None
    closed = db.last_closed_trade_for_ticker(tk)
    if not closed:
        return None
    return db.prior_trade_snapshot(closed)


def resolve_prior_trade(
    state: dict[str, Any],
    *,
    scanner_ticker: str | None = None,
) -> dict[str, Any] | None:
    pt = prior_trade_from_state(state)
    if pt:
        return pt
    return prior_trade_from_db(_scanner_ticker(state, scanner_ticker))


def sync_reentry_state(
    state: dict[str, Any],
    *,
    scanner_ticker: str | None = None,
) -> dict[str, Any]:
    """Ensure state carries prior_trade when a same-symbol closed trade exists."""
    tk = _scanner_ticker(state, scanner_ticker)
    prior = resolve_prior_trade(state, scanner_ticker=tk)
    if prior and not prior_trade_from_state(state):
        state = dict(state)
        state["prior_trade"] = prior
        state["reentry_active"] = True
    return state


def _has_active_or_recent_trade(scanner_ticker: str | None) -> bool:
    """True when a live OPEN or SELL_PENDING trade exists for this ticker.

    Belt-and-suspenders check: even if the ticker_states row was reset prematurely
    (race condition between position_monitor and the scanner feed), this catches
    in-flight positions that haven't closed in the DB yet.
    """
    from .. import db

    tk = ticker_identity.normalize_scanner(scanner_ticker)
    if not tk:
        return False
    match_sql, match_params = ticker_identity.trades_ticker_where_clause(tk)
    row = db.fetchone(
        f"""SELECT id FROM trades
             WHERE status IN ('OPEN', 'SELL_PENDING')
               AND {match_sql}
             LIMIT 1""",
        match_params,
    )
    return row is not None


def is_reentry_episode(
    state: dict[str, Any],
    *,
    scanner_ticker: str | None = None,
) -> bool:
    if state.get("reentry_active"):
        return True
    if prior_trade_from_state(state):
        return True
    if resolve_prior_trade(state, scanner_ticker=scanner_ticker) is not None:
        return True
    # Final safety: if there's a live position for this ticker in the broker,
    # always treat as reentry — the state may have been reset prematurely.
    tk = _scanner_ticker(state, scanner_ticker)
    return _has_active_or_recent_trade(tk)


def format_prior_trade_block(prior: dict[str, Any]) -> list[str]:
    exit_ts = float(prior.get("exit_ts") or 0)
    exit_iso = (
        datetime.fromtimestamp(exit_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if exit_ts
        else "unknown"
    )
    entry = prior.get("entry_price")
    exit_p = prior.get("exit_price")
    pnl = prior.get("pnl_pct")
    reason = prior.get("exit_reason") or "closed"
    lines = [
        "\nPRIOR TRADE TODAY (same ticker — re-entry rules apply):",
        (
            f"Closed {exit_iso} | entry ${float(entry or 0):.2f} → exit ${float(exit_p or 0):.2f} "
            f"| P&L {float(pnl or 0):+.1f}% | reason: {reason}"
        ),
        (
            f"Re-entry requires: {REENTRY_MIN_ALERTS}+ alerts in this new episode, "
            f"{int(REENTRY_COOLDOWN_SEC / 60)} min cooldown after exit, "
            f"RV ≥ {REENTRY_RV_MIN:.0f}x (no 90x news waiver), "
            f"price must not exceed prior exit ${float(exit_p or 0):.2f} (no chasing), "
            f"and ≤ {REENTRY_PRICE_EXIT_MULT:.1f}× prior exit, "
            f"no unrecovered REV V/NBREAK dip in the last {REENTRY_DIP_WINDOW} alerts."
        ),
        "Default to WATCH/MONITOR or PASS unless the new episode clearly re-validates momentum.",
    ]
    return lines


def has_recent_dip(alerts: list[dict[str, Any]], *, window: int = REENTRY_DIP_WINDOW) -> bool:
    for alert in alerts[-window:]:
        if _norm_label(alert.get("label")) in DIP_LABELS:
            return True
    return False


def price_recovered_above_pre_dip_peak(
    alerts: list[dict[str, Any]],
    *,
    window: int = REENTRY_DIP_WINDOW,
) -> bool:
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


def _price_chasing_prior_exit(prior: dict[str, Any], price: float) -> bool:
    exit_price = float(prior.get("exit_price") or 0)
    return exit_price > 0 and price > exit_price


def reentry_send_block(
    state: dict[str, Any],
    alerts: list[dict[str, Any]],
    alert: dict[str, Any],
    *,
    now_ts: float | None = None,
    scanner_ticker: str | None = None,
) -> tuple[bool, str | None]:
    """Return (blocked, reason_code) before sending to GPT."""
    st = sync_reentry_state(state, scanner_ticker=scanner_ticker)
    if not is_reentry_episode(st, scanner_ticker=scanner_ticker):
        return False, None

    prior = resolve_prior_trade(st, scanner_ticker=scanner_ticker)
    if not prior:
        return False, None

    alert_count = len(alerts)
    if alert_count < REENTRY_MIN_ALERTS:
        return True, "reentry_accumulating"

    now = float(now_ts if now_ts is not None else time.time())
    exit_ts = float(prior.get("exit_ts") or 0)
    if exit_ts > 0 and (now - exit_ts) < REENTRY_COOLDOWN_SEC:
        return True, "reentry_cooldown"

    exit_price = float(prior.get("exit_price") or 0)
    price = float(alert.get("price") or alerts[-1].get("price") or 0)
    if _price_chasing_prior_exit(prior, price):
        return True, "reentry_above_exit"
    if exit_price > 0 and price > exit_price * REENTRY_PRICE_EXIT_MULT:
        return True, "reentry_extended"

    if has_recent_dip(alerts) and not price_recovered_above_pre_dip_peak(alerts):
        return True, "reentry_dip_unrecovered"

    if not hard_rules.label_ok_for_grade(alert, alerts):
        return True, "reentry_no_momentum_label"
    if not hard_rules.price_rising_vs_prior(alerts):
        return True, "reentry_price_not_rising"

    return False, None


def reentry_trade_allowed(
    state: dict[str, Any],
    alerts: list[dict[str, Any]],
    *,
    now_ts: float | None = None,
    scanner_ticker: str | None = None,
) -> tuple[bool, str | None]:
    """Return (allowed, block_reason) for a TRADE action on re-entry."""
    st = sync_reentry_state(state, scanner_ticker=scanner_ticker)
    if not is_reentry_episode(st, scanner_ticker=scanner_ticker):
        return True, None

    prior = resolve_prior_trade(st, scanner_ticker=scanner_ticker)
    if not prior:
        return True, None

    if len(alerts) < REENTRY_MIN_ALERTS:
        return False, "reentry_min_alerts"

    now = float(now_ts if now_ts is not None else time.time())
    exit_ts = float(prior.get("exit_ts") or 0)
    if exit_ts > 0 and (now - exit_ts) < REENTRY_COOLDOWN_SEC:
        return False, "reentry_cooldown"

    exit_price = float(prior.get("exit_price") or 0)
    price = float(alerts[-1].get("price") or 0)
    if _price_chasing_prior_exit(prior, price):
        return False, "reentry_above_exit"
    if exit_price > 0 and price > exit_price * REENTRY_PRICE_EXIT_MULT:
        return False, "reentry_extended"

    cur_rv = float(alerts[-1].get("rv") or 0)
    if cur_rv < REENTRY_RV_MIN:
        return False, "reentry_rv_low"

    if has_recent_dip(alerts):
        if not price_recovered_above_pre_dip_peak(alerts):
            return False, "reentry_dip_unrecovered"
        if cur_rv < REENTRY_RV_MIN:
            return False, "reentry_rv_after_dip"

    if not rising_momentum_streak(alerts, n=REENTRY_MOMENTUM_STREAK):
        return False, "reentry_momentum_streak"

    return True, None


def reentry_block_note(code: str | None) -> str:
    notes = {
        "reentry_min_alerts": (
            f"Re-entry blocked — need {REENTRY_MIN_ALERTS}+ alerts in the new episode "
            f"(currently building history after prior trade)."
        ),
        "reentry_cooldown": (
            f"Re-entry blocked — {int(REENTRY_COOLDOWN_SEC / 60)} min cooldown after prior exit."
        ),
        "reentry_above_exit": (
            "Re-entry blocked — price is above prior exit (no chasing after taking profit)."
        ),
        "reentry_extended": (
            f"Re-entry blocked — price extended beyond {REENTRY_PRICE_EXIT_MULT:.1f}× prior exit."
        ),
        "reentry_dip_unrecovered": (
            "Re-entry blocked — REV V/NBREAK dip in window without reclaiming pre-dip high."
        ),
        "reentry_rv_low": f"Re-entry blocked — RV below {REENTRY_RV_MIN:.0f}x (no 90x news waiver).",
        "reentry_rv_after_dip": (
            f"Re-entry blocked — RV must be ≥ {REENTRY_RV_MIN:.0f}x after a dip label."
        ),
        "reentry_momentum_streak": (
            f"Re-entry blocked — need {REENTRY_MOMENTUM_STREAK} consecutive rising "
            "MOMENTUM/BREAKOUT alerts."
        ),
        "reentry_no_momentum_label": "Re-entry deferred — no momentum label.",
        "reentry_price_not_rising": "Re-entry deferred — price not rising vs prior alert.",
        "reentry_accumulating": "Re-entry — accumulating alerts after prior trade.",
    }
    key = str(code or "").strip()
    return notes.get(key, f"Re-entry blocked — {key.replace('_', ' ')}")
