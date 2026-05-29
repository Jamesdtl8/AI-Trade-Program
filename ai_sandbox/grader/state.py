"""SQLite ticker_states helpers (UK calendar day boundary)."""

from __future__ import annotations

import json
import time
from typing import Any

from .. import config, db


def uk_date_iso(ts: float | None = None) -> str:
    return db.uk_date_iso(ts)


def get_or_create(ticker: str, *, ts: float | None = None) -> dict[str, Any]:
    db.init()
    date = uk_date_iso(ts)
    tk = ticker.upper()
    row = db.fetchone(
        "SELECT * FROM ticker_states WHERE ticker=? AND date=?",
        (tk, date),
    )
    if row:
        return _hydrate(dict(row))
    now = float(ts or time.time())
    db.execute(
        """INSERT INTO ticker_states(
              ticker, date, state, alert_count, alerts_json, created_ts, updated_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (tk, date, "NEW", 0, "[]", now, now),
    )
    row = db.fetchone(
        "SELECT * FROM ticker_states WHERE ticker=? AND date=?",
        (tk, date),
    )
    return _hydrate(dict(row)) if row else {"ticker": tk, "date": date, "state": "NEW", "alerts": []}


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    alerts_raw = row.pop("alerts_json", "[]")
    try:
        row["alerts"] = json.loads(alerts_raw or "[]")
    except json.JSONDecodeError:
        row["alerts"] = []
    ctx_raw = row.pop("ai_context_json", None)
    if ctx_raw:
        try:
            row["ai_context"] = json.loads(ctx_raw)
        except json.JSONDecodeError:
            row["ai_context"] = ctx_raw
    else:
        row["ai_context"] = None
    prior_raw = row.pop("prior_trade_json", None)
    if prior_raw:
        try:
            row["prior_trade"] = json.loads(prior_raw)
        except json.JSONDecodeError:
            row["prior_trade"] = None
    else:
        row["prior_trade"] = None
    row["reentry_active"] = bool(int(row.get("reentry_active") or 0))
    return row


_GRADER_IN_FLIGHT_SEC = 90.0


def recover_stale_pending_ai(st_row: dict[str, Any], *, now: float | None = None) -> str:
    """Unstick tickers left in PENDING_AI after a failed or timed-out GPT call."""
    st = str(st_row.get("state") or "NEW")
    if st != "PENDING_AI":
        return st
    age = float(now or time.time()) - float(st_row.get("updated_ts") or 0)
    if age < _GRADER_IN_FLIGHT_SEC:
        return st
    tk = str(st_row.get("ticker") or "").upper()
    if tk:
        update(tk, {"state": "WATCH"})
    return "WATCH"


def update(ticker: str, updates: dict[str, Any], *, ts: float | None = None) -> None:
    get_or_create(ticker, ts=ts)
    date = uk_date_iso(ts)
    tk = ticker.upper()
    payload = dict(updates)
    if "alerts" in payload:
        payload["alerts_json"] = json.dumps(payload.pop("alerts"), default=str)
        payload["alert_count"] = len(json.loads(payload["alerts_json"]))
    if "ai_context" in payload:
        ctx = payload.pop("ai_context")
        payload["ai_context_json"] = json.dumps(ctx, default=str) if ctx is not None else None
    if "prior_trade" in payload:
        pt = payload.pop("prior_trade")
        payload["prior_trade_json"] = json.dumps(pt, default=str) if pt is not None else None
    if "reentry_active" in payload:
        payload["reentry_active"] = 1 if payload["reentry_active"] else 0
    payload["updated_ts"] = float(ts or time.time())
    cols = ", ".join(f"{k}=?" for k in payload)
    db.execute(
        f"UPDATE ticker_states SET {cols} WHERE ticker=? AND date=?",
        (*payload.values(), tk, date),
    )


def all_for_date(date: str | None = None) -> dict[str, dict[str, Any]]:
    d = date or uk_date_iso()
    rows = db.fetchall("SELECT * FROM ticker_states WHERE date=?", (d,))
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        h = _hydrate(dict(r))
        out[str(h.get("ticker") or "").upper()] = h
    return out


def ui_label(state: dict[str, Any]) -> str | None:
    st = str(state.get("state") or "")
    if st == "NEW":
        return "NEW"
    if st == "WATCHING":
        return "WATCHING"
    if st == "PENDING_AI":
        return "REVIEW"
    if st == "WATCH":
        return "REVIEW WATCH"
    if st == "TRADE":
        return "TRADE"
    if st == "TRADED":
        return "TRADED"
    if st in ("PASS", "DISQUALIFIED"):
        return "FILTERED"
    return None


def mark_traded(ticker: str, *, ts: float | None = None) -> None:
    """Trade closed for the day — allow feed label update and future re-entry."""
    update(
        ticker,
        {
            "state": "TRADED",
            "ai_decision": "CLOSED",
        },
        ts=ts,
    )


def reset_traded_for_new_alert(
    ticker: str,
    *,
    ts: float | None = None,
    prior_trade: dict[str, Any] | None = None,
) -> None:
    """Fresh scanner episode after a completed trade."""
    from .. import ticker_identity

    if prior_trade is None:
        closed = db.last_closed_trade_for_ticker(ticker.upper(), before_ts=ts)
        if closed:
            prior_trade = db.prior_trade_snapshot(closed)
        else:
            # Trade may still be in SELL_PENDING (exit order in flight, not yet CLOSED).
            # Treat SELL_PENDING as a prior trade so reentry guards apply immediately
            # instead of waiting for broker confirmation — prevents same-ticker re-entry
            # while the sell is still confirming at the broker.
            match_sql, match_params = ticker_identity.trades_ticker_where_clause(ticker.upper())
            sell_row = db.fetchone(
                f"""SELECT id, entry_price, exit_price, open_ts, exit_ts,
                           pnl_pct, pnl_gbp, exit_reason, alert_id
                      FROM trades
                     WHERE status='SELL_PENDING'
                       AND {match_sql}
                     ORDER BY open_ts DESC
                     LIMIT 1""",
                match_params,
            )
            if sell_row:
                prior_trade = db.prior_trade_snapshot(dict(sell_row))
    update(
        ticker,
        {
            "state": "NEW",
            "alerts": [],
            "ai_context": None,
            "ai_decision": None,
            "ai_grade": None,
            "disqualify_reason": None,
            "reentry_active": bool(prior_trade),
            "prior_trade": prior_trade,
        },
        ts=ts,
    )


def mark_traded_from_trade_id(trade_id: int, *, ts: float | None = None) -> None:
    from .. import ticker_identity

    row = db.fetchone("SELECT ticker, alert_id FROM trades WHERE id=?", (int(trade_id),))
    if not row:
        return
    display = ticker_identity.grader_ticker_for_trade_row(dict(row))
    if not display:
        return
    mark_traded(display, ts=ts)
