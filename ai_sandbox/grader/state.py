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
    return row


def update(ticker: str, updates: dict[str, Any], *, ts: float | None = None) -> None:
    date = uk_date_iso(ts)
    tk = ticker.upper()
    payload = dict(updates)
    if "alerts" in payload:
        payload["alerts_json"] = json.dumps(payload.pop("alerts"), default=str)
        payload["alert_count"] = len(json.loads(payload["alerts_json"]))
    if "ai_context" in payload:
        ctx = payload.pop("ai_context")
        payload["ai_context_json"] = json.dumps(ctx, default=str) if ctx is not None else None
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
    if st in ("PASS", "DISQUALIFIED"):
        return "FILTERED"
    return None
