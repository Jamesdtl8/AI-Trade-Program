"""AI sandbox engine: glues every layer together as one asyncio coroutine.

Started by :mod:`ai_sandbox.service`. One instance per process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .grader import processor as grader_processor
from . import (
    alert_parser,
    config,
    db,
    entry_fill,
    news_scanner_feed,
    news_scanner_parser,
    news_classify,
    position_monitor,
    price_data,
    scanner_feed,
    t212_ai,
    ticker_context,
)
from .slot_manager import QueuedAlert, Slot, SlotManager

_log = logging.getLogger("ai_sandbox.engine")


def _t212_detail_short(body: Any, http_status: int) -> str:
    if isinstance(body, dict):
        for k in ("description", "detail", "message", "errorMessage", "title", "humanMessage"):
            v = body.get(k)
            if v:
                return str(v)[:220]
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            return str(errs[0])[:220]
    try:
        s = json.dumps(body, default=str)
        if len(s) > 240:
            return s[:240] + "…"
        return s
    except Exception:
        return f"t212_http_{http_status}"


def _t212_error_blob(body: Any, http_status: int) -> str:
    try:
        return json.dumps({"http_status": http_status, "body": body}, default=str)
    except Exception:
        return json.dumps({"http_status": http_status, "body": repr(body)})


def _normalize_scorer_decision(decision: dict[str, Any]) -> None:
    raw = decision.get("decision")
    if isinstance(raw, str):
        decision["decision"] = raw.strip().upper()


def _alert_row_summary_db(alert_id: int) -> dict[str, Any] | None:
    if not alert_id:
        return None
    ar = db.fetchone(
        "SELECT id, ts, type, raw, parsed_json, news_class FROM alerts WHERE id=?",
        (int(alert_id),),
    )
    if not ar:
        return None
    pj = None
    if ar["parsed_json"]:
        try:
            pj = json.loads(ar["parsed_json"])
        except Exception:
            pj = None
    return {
        "id": int(ar["id"]),
        "ts": float(ar["ts"]),
        "type": ar["type"],
        "news_class": ar["news_class"],
        "parsed": pj,
        "raw_excerpt": (str(ar["raw"] or "")[:800]),
    }


def _failed_trade_audit_blob(
    *,
    raw_ticker: str,
    t212_code: str | None,
    alert: dict[str, Any],
    decision: dict[str, Any],
    alert_id: int | None,
    entry: float,
    tp_plan: float,
    stop: float,
    max_entry: float,
    quantity: float,
    phase: str,
    broker_rejection: dict[str, Any],
) -> dict[str, Any]:
    aid = int(alert_id) if alert_id else None
    score_chain = db.scores_for_alert(aid) if aid else []
    ar_sum = _alert_row_summary_db(aid) if aid else None
    return {
        "failed_ts": time.time(),
        "market_phase": phase,
        "raw_scanner_ticker": raw_ticker,
        "t212_instrument": t212_code,
        "scorer_decision": decision,
        "alert_at_trade": alert,
        "alert_row": ar_sum,
        "scores_for_alert": score_chain,
        "planned_entry": {
            "planned_entry": entry,
            "tp": tp_plan,
            "stop": stop,
            "max_entry_limit": max_entry,
            "quantity_attempted": quantity,
        },
        "broker_rejection": broker_rejection,
    }


class Engine:
    def __init__(self) -> None:
        self.mgr = SlotManager()
        self._trade_lock = asyncio.Lock()
        self._monitor_tasks: dict[int, asyncio.Task] = {}
        self._scanner_recent: list[dict[str, Any]] = []
        self._scanner_recent_max = 200
        self._api_spend_today_gbp = 0.0
        self._scan_count = 0
        self._score_count = 0
        self._last_event_ts: float = 0.0

    # ── status for the dashboard ─────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        return {
            "enabled": config.trading_enabled(),
            "credentials_ok": config.t212_credentials_ok(),
            "t212_env": config.t212_env(),
            "scanner_recent_count": self._scan_count,
            "scored": self._score_count,
            "last_event_age_s": (time.time() - self._last_event_ts) if self._last_event_ts else None,
            "queue_size": len(self.mgr.state.queue),
        }

    def scanner_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._scanner_recent[-limit:])

    def slots_snapshot(self) -> dict[str, Any]:
        self.mgr.sweep_expired_cooling()
        return self.mgr.snapshot()

    def reset_after_wipe(self) -> None:
        self._scanner_recent.clear()
        self._scan_count = 0
        self._score_count = 0
        self._last_event_ts = 0.0
        self.mgr = SlotManager()
        for task in list(self._monitor_tasks.values()):
            task.cancel()
        self._monitor_tasks.clear()

    # ── main loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        db.init()
        _log.info(
            "AI sandbox engine starting (enabled=%s, creds_ok=%s, env=%s)",
            config.trading_enabled(),
            config.t212_credentials_ok(),
            config.t212_env(),
        )
        # Restore the watch queue persisted from the previous process.
        self.mgr.hydrate_from_db()
        # Build the T212 instrument map up front so the FIRST trade has a
        # populated map to resolve against (otherwise the AI would reject
        # every TRADE as ticker_not_on_t212 until the main bot wakes up).
        try:
            if config.trading_enabled():
                n = await t212_ai.refresh_ticker_map(force=True)
                _log.info("AI sandbox ticker map ready: %d entries", n)
            else:
                _log.info("AI sandbox ticker map skipped (AI_TRADING_ENABLED=0)")
        except Exception:
            _log.exception("initial ticker map build failed (will retry on demand)")
        asyncio.create_task(t212_ai.run_positions_poller(), name="ai-t212-positions-poller")
        asyncio.create_task(t212_ai.run_account_summary_poller(), name="ai-t212-account-poller")
        asyncio.create_task(self._ticker_map_refresher(), name="ai-ticker-map-refresh")
        asyncio.create_task(self._position_reconciler(), name="ai-t212-position-reconcile")
        asyncio.create_task(self._backfill_broker_pnl_loop(), name="ai-broker-pnl-backfill")
        if await t212_ai.wait_for_positions_cache(45.0):
            try:
                await self._resume_open_trades_after_restart()
            except Exception:
                _log.exception("resume OPEN trades failed")
        else:
            _log.warning("positions cache empty after startup wait — resume deferred to reconciler")

        asyncio.create_task(self._grader_backfill_loop(), name="ai-grader-backfill")

        async def _scanner_tail():
            async for msg in scanner_feed.tail(interval=1.0, start_at_end=True):
                try:
                    await self._handle_message(msg)
                except Exception:
                    _log.exception("error handling scanner message")

        async def _news_tester_tail():
            async for msg in news_scanner_feed.tail(interval=1.0, start_at_end=True):
                try:
                    await self._handle_news_tester_message(msg)
                except Exception:
                    _log.exception("error handling news-tester message")

        if config.news_feed_enabled():
            _log.info("news-tester feed enabled → GPT grader (news_scanner_feed.jsonl)")
            await asyncio.gather(_scanner_tail(), _news_tester_tail())
        else:
            _log.info("news-tester feed disabled; all-in-one-scanner only")
            await _scanner_tail()

    async def _grader_backfill_loop(self) -> None:
        """Grade tickers that missed GPT due to old rules, state drift, or NBREAK pauses.
        Also periodically sweeps stale PENDING_AI states (ticker stuck because AI call
        failed and no new alert arrived to trigger the inline recovery).
        """
        await asyncio.sleep(8.0)
        try:
            from .grader import reconcile

            n = await reconcile.run_backfill(self)
            if n:
                self._score_count += n
                _log.info("grader backfill complete: %d graded", n)
        except Exception:
            _log.exception("grader backfill loop failed")

        # Periodic PENDING_AI sweep — runs every 2 minutes throughout the session.
        while True:
            await asyncio.sleep(120.0)
            try:
                from .grader import reconcile as _rec

                cleared = _rec.clear_stale_pending_ai()
                if cleared:
                    _log.info("stale PENDING_AI sweep cleared %d ticker(s)", cleared)
            except Exception:
                _log.exception("stale PENDING_AI sweep failed")

    # ── periodic ticker-map refresh (6h TTL is internal) ─────────────────
    async def _ticker_map_refresher(self) -> None:
        while True:
            interval = max(60.0, float(config.ai_t212_instrument_map_ttl_seconds()))
            await asyncio.sleep(interval)
            try:
                if not config.trading_enabled():
                    continue
                await t212_ai.refresh_ticker_map(force=False)
            except Exception:
                _log.exception("ticker_map_refresher iteration failed")

    async def _stop_monitor_for_slot_ix(self, slot_ix: int) -> None:
        t = self._monitor_tasks.pop(slot_ix, None)
        if t and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def _close_open_trade_external(
        self, *, trade_id: int, slot_ix: int, slot_obj: Slot | None, reason: str
    ) -> None:
        row = db.fetchone("SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,))
        if not row:
            return
        row_d = dict(row)
        t212_tkr = str(row_d.get("ticker") or "").strip().upper()
        if t212_tkr:
            qty_live = await t212_ai.broker_long_quantity(t212_tkr, retries=4)
            if qty_live > 1e-6:
                if 0 <= slot_ix < config.SLOT_COUNT:
                    slot_use = slot_obj or self.mgr.state.slots[slot_ix]
                    if slot_use.state != "ACTIVE" or slot_use.trade_id != trade_id:
                        await self._try_activate_monitored_trade(
                            row_d,
                            qty_live,
                            resume_reason="reconcile_broker_still_long",
                        )
                        _log.warning(
                            "AI RECONCILE aborted external close id=%s — broker still long %s qty=%.4f",
                            trade_id,
                            t212_tkr,
                            qty_live,
                        )
                return

        await self._stop_monitor_for_slot_ix(slot_ix)
        ts_done = time.time()
        try:
            db.trade_audit_note_external_close(
                trade_id,
                reason=reason,
                exit_ts=ts_done,
                risk_extra={"path": "engine_reconcile"},
            )
        except Exception:
            _log.exception("trade_audit external close failed id=%s", trade_id)
        db.execute(
            """UPDATE trades SET status='CLOSED', exit_ts=?, exit_reason=?,
                  pnl_pct=NULL, pnl_gbp=NULL
               WHERE id=? AND status='OPEN'""",
            (ts_done, reason[:500], trade_id),
        )
        if slot_obj is not None:
            await self.mgr.force_reset_slot(slot_obj)
        _log.warning("external trade reconcile id=%s slot=%s reason=%s", trade_id, slot_ix, reason)

    async def _try_activate_monitored_trade(
        self,
        row: dict[str, Any],
        qty_live: float,
        *,
        resume_reason: str = "resumed_after_restart",
    ) -> None:
        """Assign slot + spawn :func:`position_monitor.run_slot` for a verified OPEN trade row."""
        tid = int(row["id"])
        s_ix = int(row["slot"])
        slot_o = self.mgr.state.slots[s_ix]
        t212_tkr = str(row["ticker"] or "").strip().upper()

        db_qty = float(row.get("quantity") or 0.0)
        if abs(db_qty - qty_live) > 1e-4 and qty_live > 0:
            try:
                db.execute(
                    "UPDATE trades SET quantity=? WHERE id=?",
                    (qty_live, tid),
                )
            except Exception:
                pass

        entry_f = float(row.get("entry_price") or 0.0)
        peak_f = entry_f
        try:
            stored_peak = row.get("peak_price")
            if stored_peak is not None:
                peak_f = max(peak_f, float(stored_peak))
        except (TypeError, ValueError):
            pass
        try:
            pos_row = await t212_ai.position_row_for_ticker(t212_tkr, bypass_cache=False)
            if pos_row:
                live_px = float(pos_row.get("currentPrice") or 0.0)
                if live_px > 0:
                    peak_f = max(peak_f, live_px)
        except Exception:
            pass
        if peak_f > entry_f:
            try:
                db.trade_update_peak_price(tid, peak_f)
            except Exception:
                pass
        try:
            tp_row = float(row.get("tp") or 0.0)
        except (TypeError, ValueError):
            tp_row = 0.0
        tp_v = tp_row if tp_row > 0 else config.profit_target_price(entry_f)
        stop_v = float(row.get("stop") or 0.0)
        cap_gb = float(row.get("capital_gbp") or 0.0)
        quote_head = t212_tkr.split("_")[0]

        alert_reload: dict[str, Any] = {}
        alert_pk = row.get("alert_id")
        if alert_pk:
            alert_row = db.fetchone(
                "SELECT parsed_json FROM alerts WHERE id=?",
                (int(alert_pk),),
            )
            if alert_row is not None:
                try:
                    pj = alert_row["parsed_json"]
                except (KeyError, IndexError, TypeError):
                    pj = None
                if pj:
                    try:
                        alert_reload = json.loads(pj) or {}
                    except Exception:
                        pass

        raw_tkr = quote_head
        if isinstance(alert_reload.get("ticker"), str) and alert_reload["ticker"].strip():
            raw_tkr = alert_reload["ticker"].strip().upper().lstrip("$")

        setup = {
            "ticker": t212_tkr,
            "raw_ticker": raw_tkr,
            "entry": entry_f,
            "tp": tp_v,
            "stop": stop_v,
            "capital_gbp": cap_gb or config.usd_notionals_to_gbp(qty_live * entry_f),
            "entry_pattern": None,
            "reason": resume_reason,
            "risk_flags": [],
            "alert": alert_reload,
            "highest_price": peak_f,
        }
        if "reconcile" in str(resume_reason or "").lower() or "reconciled" in str(
            row.get("exit_reason") or ""
        ):
            setup["stop_grace_until"] = time.time() + float(config.RECONCILE_STOP_GRACE_SECONDS)
        await self.mgr.assign(
            slot_o,
            ticker=t212_tkr,
            trade_id=tid,
            entry=entry_f,
            tp=tp_v,
            stop=stop_v,
            capital_gbp=float(setup["capital_gbp"]),
        )
        task = asyncio.create_task(
            position_monitor.run_slot(slot_o, self.mgr, setup),
            name=f"ai-resumed-{slot_o.index}-{t212_tkr}",
        )
        self._monitor_tasks[slot_o.index] = task
        try:
            db.trade_audit_ensure_open_for_resume(tid)
        except Exception:
            _log.exception("trade_audit ensure open resume trade_id=%s", tid)
        _log.info(
            "ACTIVATE slot=%d trade_id=%s t212=%s entry=%.4f qty_live=%.4f",
            s_ix,
            tid,
            t212_tkr,
            entry_f,
            qty_live,
        )

    async def _capture_orphan_broker_position(
        self,
        *,
        t212_ticker: str,
        qty_live: float,
        avg_price: float | None,
    ) -> bool:
        """Attach a broker-open position to SQL + slot manager when state has no owner."""
        tkr = (t212_ticker or "").strip().upper()
        qty = float(qty_live or 0.0)
        if not tkr or qty <= 1e-6:
            return False

        existing = db.fetchone(
            "SELECT * FROM trades WHERE status='OPEN' AND ticker=? ORDER BY open_ts DESC LIMIT 1",
            (tkr,),
        )
        if existing:
            row_d = dict(existing)
            try:
                slot_ix = int(row_d["slot"])
            except (TypeError, ValueError, KeyError):
                return False
            if 0 <= slot_ix < config.SLOT_COUNT:
                slot_o = self.mgr.state.slots[slot_ix]
                if slot_o.state == "COOLING":
                    await self.mgr.force_reset_slot(slot_o)
                await self._try_activate_monitored_trade(
                    row_d,
                    qty,
                    resume_reason="reconcile_existing_sql_open",
                )
                return True
            return False

        for sl in self.mgr.state.slots:
            if sl.state == "ACTIVE" and str(sl.ticker or "").strip().upper() == tkr:
                return True

        slot = await self.mgr.find_adopt_slot()
        if not slot:
            _log.warning(
                "AI RECONCILE orphan broker position ticker=%s qty=%.4f has no free slot",
                tkr,
                qty,
            )
            return False

        if not config.reconcile_orphan_positions():
            _log.info(
                "AI RECONCILE ignoring orphan broker position ticker=%s qty=%.4f "
                "(AI_RECONCILE_ORPHAN_POSITIONS=0)",
                tkr,
                qty,
            )
            return False

        entry = float(avg_price) if avg_price is not None and avg_price > 0 else 0.0
        if entry <= 0:
            try:
                px_b, _ = await t212_ai.broker_quote_long_qty(tkr, bypass_cache=False)
                if px_b is not None and px_b > 0:
                    entry = float(px_b)
            except Exception:
                pass
        if entry <= 0:
            _log.warning(
                "AI RECONCILE orphan broker position ticker=%s qty=%.4f has no usable entry price",
                tkr,
                qty,
            )
            return False

        stop = round(entry * (1.0 - config.MAX_STOP_LOSS_PCT / 100.0), 6)
        tp = config.profit_target_price(entry)
        cap_gbp = config.usd_notionals_to_gbp(qty * entry)
        now_ts = time.time()
        trade_id = db.insert(
            """INSERT INTO trades(slot, ticker, score_id, alert_id, entry_price, tp, stop, capital_gbp,
                                  quantity, open_ts, status, t212_open_order_id, exit_reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(slot.index),
                tkr,
                None,
                None,
                entry,
                tp,
                stop,
                cap_gbp,
                qty,
                now_ts,
                "OPEN",
                "",
                "reconciled_from_broker_position",
            ),
        )
        row_new = db.fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))
        if not row_new:
            return False
        await self._try_activate_monitored_trade(
            dict(row_new),
            qty,
            resume_reason="reconcile_orphan_broker_position",
        )
        _log.warning(
            "AI RECONCILE captured orphan broker position ticker=%s qty=%.4f as trade_id=%s slot=%s",
            tkr,
            qty,
            trade_id,
            slot.index,
        )
        return True

    async def _resume_open_trades_after_restart(self) -> None:
        """Reload OPEN trades from SQLite and reconcile with live T212 positions.

        Matches the Discord bot's ``resume active monitors'' behaviour — without
        this, restarts strand live broker positions behind empty in-memory slots.
        """
        if not config.t212_credentials_ok():
            return
        async with self._trade_lock:
            rows = db.fetchall(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY open_ts DESC",
            )
            seen_slot: dict[int, int] = {}  # slot_ix -> winner trade row id

            def _close_dup_sql(tid: int) -> None:
                ts_done = time.time()
                try:
                    db.trade_audit_note_external_close(
                        tid,
                        reason="duplicate_open_cleanup",
                        exit_ts=ts_done,
                        risk_extra={"path": "resume_dedupe"},
                    )
                except Exception:
                    _log.exception("trade_audit resume dup cleanup id=%s", tid)
                db.execute(
                    """UPDATE trades SET status='CLOSED', exit_ts=?, exit_reason=?
                           WHERE id=?""",
                    (ts_done, "duplicate_open_cleanup", tid),
                )

            for row in rows:
                row = dict(row)
                tid = int(row["id"])
                s_ix = int(row["slot"])
                if not (0 <= s_ix < config.SLOT_COUNT):
                    ts_done = time.time()
                    try:
                        db.trade_audit_note_external_close(
                            tid,
                            reason="invalid_slot_cleanup",
                            exit_ts=ts_done,
                            risk_extra={"path": "resume_invalid_slot"},
                        )
                    except Exception:
                        _log.exception("trade_audit invalid slot id=%s", tid)
                    db.execute(
                        """UPDATE trades SET status='CLOSED', exit_ts=?, exit_reason=? WHERE id=?""",
                        (ts_done, "invalid_slot_cleanup", tid),
                    )
                    continue
                if s_ix in seen_slot:
                    _close_dup_sql(tid)
                    _log.warning(
                        "resume dedupe: closed duplicate OPEN trade id=%s slot=%s (keeping id=%s)",
                        tid,
                        s_ix,
                        seen_slot[s_ix],
                    )
                    continue
                seen_slot[s_ix] = tid

                slot_o = self.mgr.state.slots[s_ix]
                t212_tkr = str(row["ticker"] or "").strip().upper()
                if not t212_tkr:
                    ts_done = time.time()
                    try:
                        db.trade_audit_note_external_close(
                            tid,
                            reason="missing_t212_ticker_restart",
                            exit_ts=ts_done,
                            risk_extra={"path": "resume_missing_ticker"},
                        )
                    except Exception:
                        _log.exception("trade_audit missing ticker id=%s", tid)
                    db.execute(
                        """UPDATE trades SET status='CLOSED', exit_ts=?, exit_reason=? WHERE id=?""",
                        (ts_done, "missing_t212_ticker_restart", tid),
                    )
                    continue

                qty_live = await t212_ai.broker_long_quantity(t212_tkr, retries=4)
                if qty_live <= 0:
                    ts_done = time.time()
                    try:
                        db.trade_audit_note_external_close(
                            tid,
                            reason="startup_flat_or_unfilled:zombie_sql_open",
                            exit_ts=ts_done,
                            risk_extra={"path": "resume_startup_flat"},
                        )
                    except Exception:
                        _log.exception("trade_audit startup flat id=%s", tid)
                    db.execute(
                        """UPDATE trades SET status='CLOSED', exit_ts=?, exit_reason=? WHERE id=?""",
                        (ts_done, "startup_flat_or_unfilled:zombie_sql_open", tid),
                    )
                    _log.warning(
                        "resume DROP trade id=%s — OPEN in DB but broker flat (%s)",
                        tid, t212_tkr,
                    )
                    continue

                await self._try_activate_monitored_trade(row, qty_live, resume_reason="resumed_after_restart")
                _log.info(
                    "RESUME slot=%d trade_id=%s t212=%s qty_live=%.4f",
                    s_ix,
                    tid,
                    t212_tkr,
                    qty_live,
                )

    async def _backfill_broker_pnl_loop(self) -> None:
        """Refresh CLOSED trade P&L from T212 order history (GBP realisedProfitLoss)."""
        await asyncio.sleep(25)
        while True:
            try:
                if not config.t212_credentials_ok():
                    await asyncio.sleep(60)
                    continue
                rows = db.fetchall(
                    """
                    SELECT id, ticker, entry_price, t212_close_order_id, pnl_gbp, exit_reason, status
                      FROM trades
                     WHERE status IN ('SELL_PENDING', 'CLOSED')
                       AND t212_close_order_id IS NOT NULL
                       AND TRIM(t212_close_order_id) != ''
                     ORDER BY exit_ts DESC
                     LIMIT 25
                    """,
                )
                for row in rows:
                    row_d = dict(row)
                    st = str(row_d.get("status") or "").upper()
                    reason = str(row_d.get("exit_reason") or "")
                    if st == "CLOSED" and any(
                        reason.startswith(p) or reason == p
                        for p in config.UNCONFIRMED_CLOSE_REASON_PREFIXES
                    ):
                        continue
                    oid = str(row_d.get("t212_close_order_id") or "").strip()
                    tkr = str(row_d.get("ticker") or "").strip()
                    if not oid or not tkr:
                        continue
                    try:
                        realised = await t212_ai.backfill_closed_trade_pnl_from_broker(
                            int(row_d["id"]),
                            ticker=tkr,
                            close_order_id=oid,
                            entry_price=float(row_d.get("entry_price") or 0.0),
                        )
                        if realised is not None:
                            if st == "SELL_PENDING":
                                db.execute(
                                    """UPDATE trades SET status='CLOSED'
                                       WHERE id=? AND status='SELL_PENDING'""",
                                    (int(row_d["id"]),),
                                )
                            old = row_d.get("pnl_gbp")
                            if old is None or abs(float(old) - float(realised)) > 0.02:
                                _log.info(
                                    "broker P&L backfill trade=%s ticker=%s old=%s new=%.2f",
                                    row_d["id"],
                                    tkr,
                                    old,
                                    realised,
                                )
                    except Exception:
                        _log.exception("broker P&L backfill failed trade=%s", row_d.get("id"))
                    await asyncio.sleep(11.0)
            except Exception:
                _log.exception("broker P&L backfill loop failed")
            await asyncio.sleep(180)

    async def _position_reconciler(self) -> None:
        """Poll broker vs slot + SQLite — same principle as Discord ``_position_reconciler``."""
        await asyncio.sleep(3)
        while True:
            had_fast = False
            try:
                if not config.trading_enabled():
                    await asyncio.sleep(config.POSITION_RECONCILE_SLOW_S)
                    continue
                if not config.t212_credentials_ok():
                    await asyncio.sleep(config.POSITION_RECONCILE_SLOW_S)
                    continue

                positions = await t212_ai.get_positions(bypass_cache=False)

                broker_by_tkr: dict[str, float] = {}
                broker_avgp: dict[str, float | None] = {}
                for pos in positions or []:
                    pt = str(pos.get("ticker") or "").strip().upper()
                    qt = float(pos.get("quantity") or 0.0)
                    if not pt:
                        continue
                    if qt > 0:
                        broker_by_tkr[pt] = qt
                        ap = None
                        try:
                            raw_ap = pos.get("averagePrice")
                            if raw_ap is not None:
                                ap = float(raw_ap)
                        except (TypeError, ValueError):
                            ap = None
                        broker_avgp[pt] = ap

                async with self._trade_lock:
                    tracked_tickers: set[str] = set()

                    for sl in self.mgr.state.slots:
                        if sl.state != "SELL_PENDING" or not sl.ticker or sl.trade_id is None:
                            continue
                        tkr_sp = sl.ticker.strip().upper()
                        qty_sp = broker_by_tkr.get(tkr_sp, 0.0)
                        if qty_sp <= 1e-6:
                            await self.mgr.release_after_sell(sl)
                            try:
                                from .position_monitor import _try_confirm_close_from_broker

                                await _try_confirm_close_from_broker(int(sl.trade_id), tkr_sp)
                            except Exception:
                                _log.exception("reconcile confirm pending trade=%s", sl.trade_id)
                            had_fast = True

                    for sl in self.mgr.state.slots:
                        if sl.state != "ACTIVE" or not sl.ticker or sl.trade_id is None:
                            continue
                        tkr_sl = sl.ticker.strip().upper()
                        tracked_tickers.add(tkr_sl)

                        row_open = db.fetchone(
                            "SELECT id, status, open_ts FROM trades WHERE id=?",
                            (int(sl.trade_id),),
                        )
                        if not row_open or str(row_open["status"] or "").upper() != "OPEN":
                            _log.warning(
                                "AI RECONCILE ghost ACTIVE slot=%s t212=%s trade_id=%s (missing OPEN DB row) — clearing",
                                sl.index,
                                tkr_sl,
                                sl.trade_id,
                            )
                            await self._close_open_trade_external(
                                trade_id=int(sl.trade_id),
                                slot_ix=int(sl.index),
                                slot_obj=sl,
                                reason="state_drift:slot_active_without_open_db_trade",
                            )
                            had_fast = True
                            continue

                        qty_b = broker_by_tkr.get(tkr_sl, 0.0)
                        if qty_b <= 1e-6:
                            open_ts = float(dict(row_open).get("open_ts") or 0.0)
                            if (
                                open_ts > 0
                                and time.time() - open_ts < config.OPEN_RECONCILE_GRACE_SECONDS
                            ):
                                _log.debug(
                                    "AI RECONCILE skip flat slot=%s t212=%s — within open grace %.0fs",
                                    sl.index,
                                    tkr_sl,
                                    config.OPEN_RECONCILE_GRACE_SECONDS,
                                )
                                continue
                            _log.warning(
                                "AI RECONCILE broker flat slot=%s t212=%s — releasing slot (await broker confirm)",
                                sl.index,
                                tkr_sl,
                            )
                            pending_tid = int(sl.trade_id)
                            db.execute(
                                """UPDATE trades SET status='SELL_PENDING', exit_ts=COALESCE(exit_ts, ?),
                                          exit_reason=COALESCE(exit_reason, 'reconcile_broker_flat')
                                   WHERE id=? AND status='OPEN'""",
                                (time.time(), pending_tid),
                            )
                            await self._stop_monitor_for_slot_ix(int(sl.index))
                            await self.mgr.release_after_sell(sl)
                            try:
                                from .position_monitor import _try_confirm_close_from_broker

                                await _try_confirm_close_from_broker(pending_tid, tkr_sl)
                            except Exception:
                                _log.exception("reconcile confirm after flat trade=%s", pending_tid)
                            had_fast = True
                            continue

                        row = db.fetchone(
                            "SELECT quantity FROM trades WHERE id=? AND status='OPEN'",
                            (int(sl.trade_id),),
                        )
                        if row:
                            db_q = float(row["quantity"] or 0)
                            if abs(db_q - qty_b) > 1e-4:
                                db.execute(
                                    "UPDATE trades SET quantity=? WHERE id=? AND status='OPEN'",
                                    (qty_b, int(sl.trade_id)),
                                )
                                _log.info(
                                    "AI RECONCILE qty sync trade=%s ticker=%s db=%.6f broker=%.6f",
                                    sl.trade_id,
                                    tkr_sl,
                                    db_q,
                                    qty_b,
                                )

                    sql_open = db.fetchall(
                        "SELECT * FROM trades WHERE status='OPEN' ORDER BY open_ts DESC",
                    )
                    for row in sql_open:
                        row_d = dict(row)
                        tid = int(row_d["id"])
                        s_ix = int(row_d["slot"])
                        if not (0 <= s_ix < config.SLOT_COUNT):
                            continue
                        t212_tkr = str(row_d["ticker"] or "").strip().upper()
                        if not t212_tkr:
                            continue
                        slot_o = self.mgr.state.slots[s_ix]
                        if slot_o.state == "ACTIVE" and slot_o.trade_id == tid:
                            continue
                        if slot_o.state == "ACTIVE":
                            continue
                        if slot_o.state != "OPEN":
                            continue
                        qty_b = broker_by_tkr.get(t212_tkr, 0.0)
                        if qty_b <= 1e-6:
                            continue
                        await self._stop_monitor_for_slot_ix(s_ix)
                        await self._try_activate_monitored_trade(
                            row_d,
                            qty_b,
                            resume_reason="reconcile_orphan_sql_open",
                        )
                        had_fast = True
                        tracked_tickers.add(t212_tkr)
                        _log.warning(
                            "AI RECONCILE healed orphan OPEN trade id=%s slot=%s ticker=%s qty=%.6f",
                            tid,
                            s_ix,
                            t212_tkr,
                            qty_b,
                        )

                    orphan_candidates = [
                        t for t in broker_by_tkr if t not in tracked_tickers
                    ]
                    if orphan_candidates and config.reconcile_orphan_positions():
                        open_n = sum(1 for sl in self.mgr.state.slots if sl.state == "OPEN")
                        need = len(orphan_candidates)
                        if open_n < need:
                            for sl in self.mgr.state.slots:
                                if sl.state == "COOLING" and open_n < need:
                                    await self._stop_monitor_for_slot_ix(int(sl.index))
                                    await self.mgr.force_reset_slot(sl)
                                    open_n += 1
                                    had_fast = True

                    for b_tkr, qty in broker_by_tkr.items():
                        if b_tkr in tracked_tickers:
                            continue
                        captured = await self._capture_orphan_broker_position(
                            t212_ticker=b_tkr,
                            qty_live=qty,
                            avg_price=broker_avgp.get(b_tkr),
                        )
                        if captured:
                            tracked_tickers.add(b_tkr)
                            had_fast = True
                        else:
                            _log.warning(
                                "AI RECONCILE orphaned broker position ticker=%s qty=%.4f avg_px=%s — "
                                "not tracked by sandbox (manual or stale state)",
                                b_tkr,
                                qty,
                                broker_avgp.get(b_tkr),
                            )

                await asyncio.sleep(
                    config.POSITION_RECONCILE_FAST_S if had_fast else config.POSITION_RECONCILE_SLOW_S,
                )

            except Exception:
                _log.exception("AI position reconciler iteration failed")
                await asyncio.sleep(config.POSITION_RECONCILE_SLOW_S)

    async def _handle_news_tester_message(self, msg: dict[str, Any]) -> None:
        """Discord #news-tester: parse headline post and run the main GPT grader loop."""
        if not config.trading_enabled() or not config.news_tester_enabled():
            return

        ch_raw = str(msg.get("channel_id") or "").strip()
        ch_id = int(ch_raw) if ch_raw.isdigit() else None
        tester_ids = config.news_tester_channel_ids()
        if ch_raw == "news_tester" or ch_raw.lower() == "test":
            pass
        elif tester_ids:
            if ch_id is None or ch_id not in tester_ids:
                return
        elif ch_id is not None:
            scanner_ids = config.news_scanner_channel_ids()
            if scanner_ids and ch_id in scanner_ids:
                return

        is_edit = msg.get("event") == "message_edit"
        if is_edit:
            content = (msg.get("content_after") or msg.get("content") or "").strip()
        else:
            content = (msg.get("content") or "").strip()
        if not content:
            return

        parsed = news_scanner_parser.parse_news_scanner_post(content)
        if not parsed:
            _log.info("news-tester parse fail (ignored): %s", content[:120])
            return

        ticker = (parsed.get("ticker") or "").strip().upper()
        if not ticker or ticker == "?":
            return
        if t212_ai.resolve_ticker(ticker) is None:
            try:
                await t212_ai.refresh_ticker_map(force=False)
            except Exception:
                _log.debug("news-tester ticker map refresh failed for %s", ticker)
            if t212_ai.resolve_ticker(ticker) is None:
                _log.info("news-tester ignored — %s not on T212", ticker)
                return

        alert: dict[str, Any] = {
            "type": "SCANNER",
            "ticker": ticker,
            "price": parsed.get("price"),
            "market_cap": parsed.get("market_cap"),
            "news_headline": parsed.get("news_headline"),
            "rank": 1,
            "rv": 50.0,
            "label": "MOMENTUM",
            "source": "news_tester",
            "raw": content,
        }
        mid = (msg.get("message_id") or "").strip()
        if mid:
            alert["discord_message_id"] = mid
        if is_edit:
            alert["discord_message_edit"] = True
        alert["discord_ts"] = msg.get("timestamp")

        await self._run_scanner_grader_path(msg, alert, content, is_edit=is_edit)

    async def _run_scanner_grader_path(
        self,
        msg: dict[str, Any],
        alert: dict[str, Any],
        content: str,
        *,
        is_edit: bool,
    ) -> None:
        ticker = (alert.get("ticker") or "").upper() or None
        atype = alert.get("type", "UNKNOWN")

        self._scan_count += 1
        self._last_event_ts = time.time()

        raw_for_db = json.dumps(msg, ensure_ascii=False) if is_edit else content
        db_type = "NEWS_TESTER" if alert.get("source") == "news_tester" else (
            "SCANNER_EDIT" if is_edit else atype
        )
        alert_id = db.log_alert(ticker, db_type, raw_for_db, alert)
        recent_entry = {"ts": time.time(), "alert_id": alert_id, **alert, "event": msg.get("event")}
        self._scanner_recent.append(recent_entry)
        if len(self._scanner_recent) > self._scanner_recent_max:
            self._scanner_recent.pop(0)

        context: dict[str, Any] = {}
        if ticker:
            context = ticker_context.build(ticker, {**alert, "_alert_id": alert_id})
        recent_entry["context"] = context

        if atype != "SCANNER":
            return

        if ticker:
            if t212_ai.instrument_map_ready() and t212_ai.resolve_ticker(ticker) is None:
                try:
                    from .grader import state as ticker_state

                    ticker_state.update(
                        ticker,
                        {"state": "DISQUALIFIED", "disqualify_reason": "not_on_t212"},
                    )
                except Exception:
                    _log.exception("not_on_t212 ticker_state update failed for %s", ticker)
                recent_entry["grader_state"] = "DISQUALIFIED"
                recent_entry["active_label"] = "FILTERED"
                recent_entry["disqualify_reason"] = "not_on_t212"
                return

            bl = db.t212_blacklist_get(ticker)
            if bl:
                tag = ""
                try:
                    tag = str(bl["reason"]) if bl else ""
                except Exception:
                    pass
                reason = f"blacklist:{tag}" if tag else "blacklist"
                try:
                    from .grader import state as ticker_state

                    ticker_state.update(
                        ticker,
                        {"state": "DISQUALIFIED", "disqualify_reason": reason},
                    )
                except Exception:
                    _log.exception("blacklist ticker_state update failed for %s", ticker)
                recent_entry["grader_state"] = "DISQUALIFIED"
                recent_entry["active_label"] = "FILTERED"
                recent_entry["disqualify_reason"] = reason
                return

        news_class: str | None = None
        if alert.get("news_headline"):
            news_class = news_classify.classify_headline(alert["news_headline"])
            db.set_alert_news_class(alert_id, news_class)
            recent_entry["news_class"] = news_class
            if news_class == "NEGATIVE":
                try:
                    from .grader import state as ticker_state

                    ticker_state.update(
                        ticker,
                        {"state": "DISQUALIFIED", "disqualify_reason": "negative_news"},
                    )
                except Exception:
                    _log.exception("negative_news ticker_state update failed for %s", ticker)
                recent_entry["grader_state"] = "DISQUALIFIED"
                recent_entry["active_label"] = "FILTERED"
                recent_entry["disqualify_reason"] = "negative_news"
                return

        paused, why = self.mgr.entries_paused()
        if paused:
            recent_entry["active_label"] = "FILTERED"
            recent_entry["disqualify_reason"] = f"paused:{why}"
            return

        decision = await grader_processor.process_scanner_alert(
            ticker=ticker or "?",
            alert=alert,
            alert_id=alert_id,
            recent_entry=recent_entry,
        )
        if not decision:
            return

        self._score_count += 1
        _normalize_scorer_decision(decision)

        dec = decision.get("decision")
        if dec == "TRADE":
            await self._try_open_trade(
                ticker,
                alert,
                decision,
                alert_id,
                fail_if_no_slot=True,
            )
            return

        if dec == "WATCH":
            try:
                db.watch_episode_ensure_open(
                    ticker or "?",
                    alert_id=alert_id,
                    added_ts=time.time(),
                    event={
                        "kind": "grader_monitor",
                        "ts": time.time(),
                        "alert_id": alert_id,
                        "decision": dec,
                        "grade": decision.get("grade"),
                        "reason": decision.get("reason"),
                    },
                )
            except Exception:
                _log.exception("watch_episode_ensure_open failed for %s", ticker)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if not config.trading_enabled():
            return
        is_edit = msg.get("event") == "message_edit"
        if is_edit:
            content = (msg.get("content_after") or msg.get("content") or "").strip()
        else:
            content = (msg.get("content") or "").strip()
        if not content:
            return

        alert = alert_parser.parse(content)
        if alert.get("type") in ("FIRE", "WHALE"):
            return
        mid = (msg.get("message_id") or "").strip()
        if mid:
            alert["discord_message_id"] = mid
        if is_edit:
            alert["discord_message_edit"] = True
        alert["discord_ts"] = msg.get("timestamp")
        ticker = (alert.get("ticker") or "").upper() or None

        await self._run_scanner_grader_path(msg, alert, content, is_edit=is_edit)

    def _record_slots_full_rejection(
        self,
        ticker: str,
        alert: dict[str, Any],
        decision: dict[str, Any],
        alert_id: int,
    ) -> None:
        reason = "slots_full"
        entry = float(alert.get("price") or decision.get("entry") or 0.0)
        t212_code = t212_ai.resolve_ticker(ticker) or ticker.upper()
        rid = db.insert(
            """INSERT INTO trades(slot, ticker, score_id, alert_id, entry_price, tp, stop,
                  capital_gbp, quantity, open_ts, status, t212_open_order_id, t212_error,
                  exit_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                0,
                t212_code,
                None,
                alert_id,
                entry,
                decision.get("tp"),
                round(entry * (1.0 - config.MAX_STOP_LOSS_PCT / 100.0), 6) if entry > 0 else None,
                0.0,
                0.0,
                time.time(),
                "REJECTED",
                "",
                "Unable due to Slots Full",
                reason,
            ),
        )
        try:
            db.trade_audit_failed(
                trade_id=int(rid),
                alert_id=alert_id,
                ticker_t212=t212_code,
                added_ts=time.time(),
                audit={
                    "reason": "Unable due to Slots Full",
                    "scorer_decision": decision,
                    "alert": alert,
                },
                final_reason="Unable due to Slots Full",
            )
        except Exception:
            _log.exception("slots_full audit failed ticker=%s", ticker)
        _log.info("TRADE rejected — slots full ticker=%s", ticker)

    async def _try_open_trade(
        self,
        ticker: str | None,
        alert: dict[str, Any],
        decision: dict[str, Any],
        alert_id: int,
        *,
        fail_if_no_slot: bool = False,
    ) -> None:
        if not ticker:
            return
        async with self._trade_lock:
            slot = await self.mgr.find_open_slot()
            if not slot:
                self._record_slots_full_rejection(ticker, alert, decision, alert_id)
                return

            entry = float(alert.get("price") or decision.get("entry") or 0.0)
            stop_raw = float(decision.get("stop") or 0.0)
            if entry <= 0:
                _log.info(
                    "rejecting trade %s — bad entry level entry=%s",
                    ticker, entry,
                )
                return
            tp_plan = config.profit_target_price(entry, decision)

            # ── HARD 10% STOP CAP ────────────────────────────────────────
            min_stop = round(entry * (1.0 - config.MAX_STOP_LOSS_PCT / 100.0), 6)
            if stop_raw <= 0 or stop_raw >= entry:
                stop = min_stop
                _log.info(
                    "stop %s missing/invalid — defaulting to 10%% floor %.4f",
                    ticker, stop,
                )
            elif stop_raw < min_stop:
                _log.info("stop %s clamped %.4f → %.4f (10%% floor)", ticker, stop_raw, min_stop)
                stop = min_stop
            else:
                stop = stop_raw

            max_entry_raw = decision.get("max_entry")
            try:
                max_entry = float(max_entry_raw) if max_entry_raw is not None else 0.0
            except (TypeError, ValueError):
                max_entry = 0.0
            entry_cap = config.entry_limit_cap_price(entry)
            if max_entry <= entry:
                max_entry = entry_cap
            else:
                max_entry = min(max_entry, entry_cap)

            phase = config.market_phase()

            t212_code = t212_ai.resolve_ticker(ticker)
            if not t212_code:
                try:
                    await t212_ai.refresh_ticker_map(force=True)
                except Exception:
                    pass
                t212_code = t212_ai.resolve_ticker(ticker)

            def _reject_row(err_blob: str, short: str, *, attempted_qty: float = 0.0) -> int:
                cap_gb = (
                    config.usd_notionals_to_gbp(float(attempted_qty) * float(entry))
                    if attempted_qty > 0 and entry > 0
                    else 0.0
                )
                return db.insert(
                    """INSERT INTO trades(slot, ticker, score_id, alert_id, entry_price, tp, stop,
                          capital_gbp, quantity, open_ts, status, t212_open_order_id, t212_error,
                          exit_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        slot.index,
                        t212_code or ticker.upper(),
                        None,
                        alert_id,
                        entry,
                        tp_plan,
                        stop,
                        cap_gb,
                        0.0,
                        time.time(),
                        "REJECTED",
                        "",
                        err_blob,
                        short[:500] if short else "t212_reject",
                    ),
                )

            def _record_failed(
                rid: int,
                brief: str,
                *,
                kind: str,
                http_status: int = 0,
                body: Any = None,
                qty: float = 0.0,
            ) -> None:
                br: dict[str, Any] = {
                    "kind": kind,
                    "brief": (brief or "")[:500],
                    "http_status": int(http_status),
                }
                if body is not None:
                    try:
                        br["detail_json"] = json.dumps(body, default=str)[:8000]
                    except Exception:
                        br["detail_json"] = str(body)[:8000]
                try:
                    aud = _failed_trade_audit_blob(
                        raw_ticker=ticker,
                        t212_code=t212_code,
                        alert=alert,
                        decision=decision,
                        alert_id=alert_id if alert_id else None,
                        entry=entry,
                        tp_plan=tp_plan,
                        stop=stop,
                        max_entry=max_entry,
                        quantity=float(qty),
                        phase=phase,
                        broker_rejection=br,
                    )
                    db.trade_audit_failed(
                        trade_id=int(rid),
                        alert_id=alert_id if alert_id else None,
                        ticker_t212=t212_code or ticker.upper(),
                        added_ts=time.time(),
                        audit=aud,
                        final_reason=brief,
                    )
                except Exception:
                    _log.exception("trade_audit_failed rid=%s ticker=%s", rid, ticker)

            if not t212_code:
                blob = json.dumps(
                    {"http_status": 0, "body": {"detail": "ticker not in T212 instrument map"}}
                )
                rid = _reject_row(blob, "ticker_not_on_t212")
                _record_failed(
                    rid,
                    "ticker_not_on_t212",
                    kind="not_on_t212",
                    http_status=0,
                    body={"detail": "ticker not in T212 instrument map"},
                    qty=0.0,
                )
                try:
                    db.t212_blacklist_add(
                        ticker,
                        reason="NOT_ON_T212",
                        detail="ticker_not_on_t212",
                        t212_instrument=None,
                    )
                except Exception:
                    _log.exception("t212_blacklist_add failed for %s", ticker)
                _log.warning(
                    "T212 ticker map has no code for %s — recorded REJECTED row=%s",
                    ticker, rid,
                )
                return

            bl = db.t212_blacklist_get(ticker)
            if bl:
                br = str(bl["reason"] or "").upper()
                det = str(bl["detail"] or "").lower()
                if (
                    "CLOSE_ONLY" in br
                    or "close_only" in det
                    or "close only" in det
                    or "close-only" in det
                ):
                    blob = json.dumps(
                        {
                            "http_status": 0,
                            "body": {"detail": "instrument in close-only mode (blacklist)"},
                        }
                    )
                    rid = _reject_row(blob, "close_only_mode", attempted_qty=0.0)
                    _record_failed(
                        rid,
                        "close_only_mode",
                        kind="close_only_mode",
                        http_status=0,
                        body={"detail": "instrument in close-only mode (blacklist)"},
                        qty=0.0,
                    )
                    _log.warning(
                        "TRADE blocked — close-only blacklist %s (row=%s)",
                        ticker,
                        rid,
                    )
                    return

            precision = t212_ai.quantity_precision(t212_code)
            slot_gbp = config.slot_capital_gbp_for_trade(db=db)
            capital_usd = slot_gbp * config.GBP_USD_RATE
            base_qty = t212_ai.snap_quantity(capital_usd / entry, precision)
            min_q = t212_ai.minimum_buy_quantity(t212_code)
            if base_qty < min_q:
                base_qty = min_q
            quantity = await t212_ai.cap_order_buy_quantity(t212_code, base_qty)
            if quantity <= 0:
                _log.info(
                    "rejecting OPEN %s — broker max-open headroom exhausted for new buys",
                    t212_code,
                )
                return

            if phase == "closed":
                _log.info("market closed — skip TRADE %s (no queue)", ticker)
                rid = db.insert(
                    """INSERT INTO trades(slot, ticker, alert_id, entry_price, open_ts, status, exit_reason, t212_error)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        0,
                        t212_code or ticker.upper(),
                        alert_id,
                        entry,
                        time.time(),
                        "REJECTED",
                        "market_closed",
                        "Market closed — TRADE not sent",
                    ),
                )
                try:
                    db.trade_audit_failed(
                        trade_id=int(rid),
                        alert_id=alert_id,
                        ticker_t212=t212_code or ticker.upper(),
                        added_ts=time.time(),
                        audit={"reason": "Market closed", "scorer_decision": decision},
                        final_reason="Market closed — TRADE not sent",
                    )
                except Exception:
                    pass
                return

            try:
                _log.info("entry %s — market buy (all sessions)", t212_code)
                order = await t212_ai.place_market(t212_code, quantity)
            except t212_ai.T212AIError as exc:
                blob = _t212_error_blob(exc.body, exc.status)
                brief = _t212_detail_short(exc.body, exc.status)
                rid = _reject_row(blob, brief, attempted_qty=quantity)
                is_co = t212_ai.is_close_only_error(exc.body)
                if is_co:
                    try:
                        db.t212_blacklist_add(
                            ticker,
                            reason="CLOSE_ONLY",
                            detail=brief[:500] if brief else None,
                            t212_instrument=t212_code or None,
                        )
                    except Exception:
                        _log.exception("t212_blacklist_add CLOSE_ONLY failed for %s", ticker)
                _record_failed(
                    rid,
                    brief,
                    kind="close_only_mode" if is_co else "order_reject",
                    http_status=exc.status,
                    body=exc.body,
                    qty=quantity,
                )
                _log.warning(
                    "T212 rejected OPEN %s qty=%s phase=%s short=%s (trade row id=%s)",
                    ticker, quantity, phase, brief, rid,
                )
                return

            if order.get("stub"):
                _log.info(
                    "AI order suppressed (trading disabled / no creds) — no trade row ticker=%s",
                    ticker,
                )
                return

            oid = order.get("id")
            if oid in (None, "", 0, "0"):
                blob = _t212_error_blob(order, 200)
                brief = _t212_detail_short(order, 200)
                rid = _reject_row(blob, brief or "missing_order_id", attempted_qty=quantity)
                _record_failed(
                    rid,
                    brief or "missing_order_id",
                    kind="bad_order_response",
                    http_status=200,
                    body=order,
                    qty=quantity,
                )
                _log.warning("T212 missing order id for %s (row=%s)", ticker, rid)
                return

            request_qty_live = float(order.get("quantity") or quantity)

            fq, favg = await entry_fill.wait_market_fill(
                t212_code,
                quantity,
                timeout_sec=config.FILL_WAIT_TIMEOUT_SECONDS,
            )
            if not fq or fq <= 0:
                blob = json.dumps({"detail": "market_entry_positions_timeout"})
                rid = _reject_row(blob, "entry_unfilled_market_timeout", attempted_qty=quantity)
                _record_failed(
                    rid,
                    "entry_unfilled_market_timeout",
                    kind="entry_timeout",
                    body={"detail": "market_entry_positions_timeout"},
                    qty=quantity,
                )
                _log.warning("market entry not confirmed positions %s qty_req=%s", t212_code, quantity)
                return
            filled_qty, fill_avg = fq, favg

            filled_prec = t212_ai.snap_quantity(float(filled_qty), precision)

            fill_avg_bf = fill_avg if (fill_avg and fill_avg > 0 and fill_avg == fill_avg) else None
            broker_ap = await t212_ai.position_average_entry_usd(t212_code, bypass_cache=True)
            if broker_ap is not None:
                eff_entry = round(float(broker_ap), 6)
            elif fill_avg_bf is not None:
                eff_entry = round(float(fill_avg_bf), 6)
            else:
                eff_entry = round(float(entry), 6)
            deployed_capital_gbp = config.usd_notionals_to_gbp(float(filled_prec) * float(eff_entry))
            tp = config.profit_target_price(eff_entry, decision)
            scorer_tp = decision.get("tp")
            tp_pct_eff = config.resolve_take_profit_pct(decision)

            trade_id = db.insert(
                """INSERT INTO trades(slot, ticker, score_id, alert_id, entry_price, tp, stop, capital_gbp,
                                      quantity, open_ts, status, t212_open_order_id, peak_price)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    slot.index,
                    t212_code,
                    None,
                    alert_id,
                    eff_entry,
                    tp,
                    stop,
                    deployed_capital_gbp,
                    filled_prec,
                    time.time(),
                    "OPEN",
                    str(order.get("id") or ""),
                    eff_entry,
                ),
            )
            score_chain = db.scores_for_alert(int(alert_id)) if alert_id else []
            ar = (
                db.fetchone(
                    "SELECT id, ts, type, raw, parsed_json, news_class FROM alerts WHERE id=?",
                    (int(alert_id),),
                )
                if alert_id
                else None
            )
            alert_row_summary: dict[str, Any] | None = None
            if ar:
                pj = None
                if ar["parsed_json"]:
                    try:
                        pj = json.loads(ar["parsed_json"])
                    except Exception:
                        pj = None
                alert_row_summary = {
                    "id": int(ar["id"]),
                    "ts": float(ar["ts"]),
                    "type": ar["type"],
                    "news_class": ar["news_class"],
                    "parsed": pj,
                    "raw_excerpt": (str(ar["raw"] or "")[:800]),
                }
            open_audit_ts = time.time()
            audit_blob = {
                "opened_ts": open_audit_ts,
                "market_phase": phase,
                "raw_scanner_ticker": ticker,
                "t212_instrument": t212_code,
                "scorer_decision": decision,
                "alert_at_trade": alert,
                "alert_row": alert_row_summary,
                "scores_for_alert": score_chain,
                "entry": {
                    "planned_entry": entry,
                    "effective_entry": eff_entry,
                    "tp": tp,
                    "scorer_tp": scorer_tp,
                    "take_profit_pct": tp_pct_eff,
                    "take_profit_pct_cap": config.AI_TAKE_PROFIT_PCT,
                    "take_profit_pct_floor": config.AI_TAKE_PROFIT_PCT_MIN,
                    "stop": stop,
                    "max_entry_limit": max_entry,
                    "quantity_requested": quantity,
                    "quantity_filled": float(filled_prec),
                    "fill_avg_broker": fill_avg_bf,
                    "t212_open_order_id": str(order.get("id") or ""),
                    "entry_order_kind": "market",
                },
            }
            try:
                db.trade_audit_open(
                    trade_id=int(trade_id),
                    alert_id=int(alert_id) if alert_id else None,
                    ticker_t212=t212_code,
                    added_ts=open_audit_ts,
                    audit=audit_blob,
                )
            except Exception:
                _log.exception("trade_audit_open failed trade_id=%s", trade_id)
            await self.mgr.assign(
                slot,
                ticker=t212_code,
                trade_id=trade_id,
                entry=eff_entry,
                tp=tp,
                stop=stop,
                capital_gbp=deployed_capital_gbp,
            )
            _log.info(
                "OPEN slot=%d raw=%s t212=%s entry_eff=%.4f tp=%.4f stop=%.4f qty_filled=%s",
                slot.index, ticker, t212_code, eff_entry, tp, stop, filled_prec,
            )

            setup = {
                "ticker": t212_code,
                "raw_ticker": ticker,
                "entry": eff_entry,
                "tp": tp,
                "stop": stop,
                "take_profit_pct": tp_pct_eff,
                "capital_gbp": deployed_capital_gbp,
                "entry_pattern": decision.get("entry_pattern"),
                "reason": decision.get("reason"),
                "risk_flags": decision.get("risk_flags"),
                "alert": alert,
            }
            task = asyncio.create_task(
                position_monitor.run_slot(slot, self.mgr, setup),
                name=f"ai-slot-{slot.index}-{t212_code}",
            )
            self._monitor_tasks[slot.index] = task
