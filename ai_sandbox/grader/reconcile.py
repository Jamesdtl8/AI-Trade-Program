"""Sync ticker_states from DB alerts and backfill missed GPT grades."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import config, db
from . import hard_rules, processor, state as ticker_state

_log = logging.getLogger("ai_sandbox.grader.reconcile")

_SCANNER_TYPES = ("SCANNER", "SCANNER_EDIT", "NEWS_TESTER")


def _parsed_alert(row: dict[str, Any]) -> dict[str, Any]:
    alert: dict[str, Any] = {"ts": float(row.get("ts") or time.time())}
    if row.get("parsed_json"):
        try:
            alert.update(json.loads(row["parsed_json"]))
        except json.JSONDecodeError:
            pass
    alert["type"] = row.get("type") or "SCANNER"
    return alert


def sync_ticker_from_db(ticker: str, *, day0: float | None = None) -> dict[str, Any] | None:
    """Rebuild accumulated alerts in ticker_states from SQLite alert rows."""
    tk = ticker.upper()
    if db.t212_blacklist_get(tk):
        return None

    start = float(day0 if day0 is not None else config.uk_day_start_ts())
    rows = db.fetchall(
        """SELECT id, ts, type, parsed_json
             FROM alerts
            WHERE UPPER(ticker)=? AND ts >= ?
              AND type IN ('SCANNER', 'SCANNER_EDIT', 'NEWS_TESTER')
            ORDER BY id ASC""",
        (tk, start),
    )
    if not rows:
        return None

    snapshots: list[dict[str, Any]] = []
    float_val = None
    for row in rows:
        parsed = _parsed_alert(dict(row))
        if hard_rules.is_nbreak_event(parsed):
            continue
        if hard_rules.hard_disqualify(parsed)[0]:
            continue
        snapshots.append(processor.alert_snapshot(parsed, ts=float(row["ts"])))
        float_val = parsed.get("float") or float_val

    if not snapshots:
        return None

    st_row = ticker_state.get_or_create(tk)
    st = str(st_row.get("state") or "NEW")
    st = ticker_state.recover_stale_pending_ai(st_row)
    if st != str(st_row.get("state") or ""):
        st_row = ticker_state.get_or_create(tk)
        st = str(st_row.get("state") or "NEW")
    disqual = st_row.get("disqualify_reason")
    if st == "DISQUALIFIED" and hard_rules.is_recoverable_disqualify(disqual):
        st = "WATCHING"
        disqual = None
    elif st in ("DISQUALIFIED", "PASS") and disqual in hard_rules.PERMANENT_DISQUALIFY:
        return st_row
    elif st == "PASS":
        st = "WATCHING"
    elif st in ("NEW",):
        st = "WATCHING"

    ticker_state.update(
        tk,
        {
            "state": st,
            "alerts": snapshots,
            "float_shares": float_val or st_row.get("float_shares"),
            "disqualify_reason": disqual if st == "DISQUALIFIED" else None,
        },
    )
    return ticker_state.get_or_create(tk)


def _last_graded_alert_number(ticker: str, *, day0: float) -> int:
    row = db.fetchone(
        "SELECT MAX(alert_number) AS n FROM ai_decisions WHERE ticker=? AND ts >= ?",
        (ticker.upper(), day0),
    )
    return int(row["n"] or 0) if row else 0


def list_backfill_candidates(*, day0: float | None = None) -> list[dict[str, Any]]:
    start = float(day0 if day0 is not None else config.uk_day_start_ts())
    tickers = db.fetchall(
        """SELECT DISTINCT UPPER(ticker) AS ticker
             FROM alerts
            WHERE ts >= ? AND ticker IS NOT NULL AND TRIM(ticker) != ''
              AND type IN ('SCANNER', 'SCANNER_EDIT', 'NEWS_TESTER')""",
        (start,),
    )
    out: list[dict[str, Any]] = []
    for row in tickers:
        tk = str(row["ticker"] or "").strip().upper()
        if not tk or db.t212_blacklist_get(tk):
            continue
        st_row = sync_ticker_from_db(tk, day0=start)
        if not st_row:
            continue
        st = ticker_state.recover_stale_pending_ai(st_row)
        if st != str(st_row.get("state") or ""):
            st_row = ticker_state.get_or_create(tk)
        if str(st_row.get("state") or "") in ("TRADE", "TRADED"):
            continue
        if str(st_row.get("state") or "") == "PENDING_AI":
            continue
        disqual = st_row.get("disqualify_reason")
        if str(st_row.get("state") or "") == "DISQUALIFIED" and not hard_rules.is_recoverable_disqualify(
            disqual
        ):
            continue

        alerts = list(st_row.get("alerts") or [])
        if len(alerts) < 2:
            continue

        last_row = db.fetchone(
            """SELECT id, ts, type, parsed_json FROM alerts
                WHERE UPPER(ticker)=? AND ts >= ?
                  AND type IN ('SCANNER', 'SCANNER_EDIT', 'NEWS_TESTER')
                ORDER BY id DESC LIMIT 1""",
            (tk, start),
        )
        if not last_row:
            continue
        latest = _parsed_alert(dict(last_row))
        if hard_rules.is_nbreak_event(latest) or hard_rules.hard_disqualify(latest)[0]:
            latest = alerts[-1]
            alert_id = int(last_row["id"])
        else:
            alert_id = int(last_row["id"])

        ready, why = hard_rules.should_send_to_ai(st_row, latest)
        if not ready:
            continue

        last_graded = _last_graded_alert_number(tk, day0=start)
        alert_n = len(alerts)
        if alert_n <= last_graded and str(st_row.get("state") or "") not in ("PASS",):
            continue

        out.append(
            {
                "ticker": tk,
                "alert_id": alert_id,
                "alert": latest,
                "why": why,
                "alert_number": alert_n,
                "last_graded": last_graded,
            }
        )
    out.sort(key=lambda x: (-int(x["alert_number"]), x["ticker"]))
    return out


def clear_stale_pending_ai() -> int:
    """Sweep all PENDING_AI ticker_states older than the timeout and restore them to WATCH.

    This handles the WNW-class bug: when an AI call fails silently or hangs and no new
    alert arrives for that ticker, the state stays stuck in PENDING_AI indefinitely.
    The processor's inline recovery only fires when a new alert arrives; this sweep runs
    periodically in the background so stale PENDING_AI never persists beyond ~2 minutes.

    Returns the number of tickers that were unstuck.
    """
    today = config.uk_date_iso()
    stale_rows = db.fetchall(
        """SELECT ticker, updated_ts, state FROM ticker_states
            WHERE date=? AND state='PENDING_AI'""",
        (today,),
    )
    if not stale_rows:
        return 0
    cleared = 0
    now = time.time()
    for row in stale_rows:
        tk = str(row["ticker"] or "").upper()
        age = now - float(row["updated_ts"] or 0)
        if age >= ticker_state._GRADER_IN_FLIGHT_SEC:
            _log.warning(
                "stale PENDING_AI sweep: clearing %s (age=%.0fs → WATCH)", tk, age
            )
            ticker_state.update(tk, {"state": "WATCH"})
            cleared += 1
    return cleared


async def run_backfill(engine: Any) -> int:
    """Grade tickers that missed GPT due to old rules or state drift."""
    day0 = config.uk_day_start_ts()
    candidates = list_backfill_candidates(day0=day0)
    if not candidates:
        _log.info("grader backfill: nothing pending")
        return 0

    _log.info("grader backfill: %d candidate(s)", len(candidates))
    graded = 0
    for item in candidates:
        tk = item["ticker"]
        recent_entry: dict[str, Any] = {
            "ts": time.time(),
            "ticker": tk,
            "type": "SCANNER",
            "backfill": True,
        }
        try:
            sync_ticker_from_db(tk, day0=day0)
            decision = await processor.process_scanner_alert(
                ticker=tk,
                alert=item["alert"],
                alert_id=int(item["alert_id"]),
                recent_entry=recent_entry,
                skip_append=True,
            )
            if not decision:
                _log.info("grader backfill skip %s (%s)", tk, recent_entry.get("defer_reason"))
                continue
            graded += 1
            _log.info(
                "grader backfill graded %s alert#%s -> %s (%s)",
                tk,
                item["alert_number"],
                decision.get("decision"),
                item["why"],
            )
            if decision.get("decision") == "TRADE" and hasattr(engine, "_try_open_trade"):
                await engine._try_open_trade(
                    tk,
                    item["alert"],
                    decision,
                    int(item["alert_id"]),
                    fail_if_no_slot=True,
                )
            elif decision.get("decision") == "WATCH":
                try:
                    db.watch_episode_ensure_open(
                        tk,
                        alert_id=int(item["alert_id"]),
                        added_ts=time.time(),
                        event={
                            "kind": "grader_backfill",
                            "ts": time.time(),
                            "alert_id": int(item["alert_id"]),
                            "decision": decision.get("decision"),
                            "grade": decision.get("grade"),
                            "reason": item["why"],
                        },
                    )
                except Exception:
                    _log.exception("watch_episode backfill failed for %s", tk)
        except Exception:
            _log.exception("grader backfill failed for %s", tk)
    return graded
