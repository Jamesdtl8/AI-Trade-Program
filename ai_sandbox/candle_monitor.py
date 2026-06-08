"""1m candle-close monitor: waiting for entry → unprotected → protected → runner → closed.

Uses yfinance 1-minute candle closes (via :mod:`price_data`). Broker execution uses
market orders when a candle-close rule fires (live) or paper-close (disabled).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import candle_model as cm
from . import config, db, entry_fill, price_data, t212_ai
from .position_monitor import _send_market_sell
from .slot_manager import Slot, SlotManager

_log = logging.getLogger("ai_sandbox.candle_monitor")


def _hydrate_trade(row: dict[str, Any]) -> cm.CandleTradeState:
    levels = cm.CandleLevels.from_dict(json.loads(row.get("levels_json") or "{}"))
    return cm.CandleTradeState(state=str(row.get("state") or cm.STATE_WAITING), levels=levels)


def _persist_setup(setup_id: int, trade: cm.CandleTradeState, *, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "state": trade.state,
        "levels_json": json.dumps(trade.levels.to_dict()),
    }
    if extra:
        payload.update(extra)
    db.candle_setup_update(setup_id, **payload)


async def run_setup(
    setup_id: int,
    mgr: SlotManager,
    *,
    trade_lock: asyncio.Lock,
) -> None:
    """Poll 1m candles until setup reaches a terminal state."""
    row = db.candle_setup_get(setup_id)
    if not row:
        return

    ticker = str(row.get("ticker") or "")
    ysym = str(row.get("yahoo_symbol") or price_data.yahoo_symbol(ticker))
    gap = config.candle_entry_gap_pct()
    trade = _hydrate_trade(row)
    last_candle_ts = float(row.get("last_candle_ts") or row.get("alert_ts") or 0)
    trade_id = row.get("trade_id")
    slot_ix: int | None = None

    _log.info(
        "candle setup #%s %s waiting trigger=%.4f (A=%.4f +%.1f%%)",
        setup_id,
        ticker,
        trade.levels.entry_trigger,
        trade.levels.alert_price,
        gap,
    )

    while trade.state not in (cm.STATE_CLOSED, cm.STATE_NOT_FILLED):
        try:
            await asyncio.sleep(config.candle_poll_seconds())

            if config.us_after_hours_complete():
                bars = price_data.candles_1m(ysym, count=5)
                if bars:
                    final_close = float(bars[-1].get("c") or 0)
                    step = cm.end_of_day_close(trade, final_close)
                    await _apply_step(
                        setup_id,
                        mgr,
                        trade_lock=trade_lock,
                        trade=trade,
                        step=step,
                        ticker=ticker,
                        ysym=ysym,
                        alert_id=row.get("alert_id"),
                        trade_id=trade_id,
                        slot_ix=slot_ix,
                    )
                    trade.state = step.state
                    if step.levels:
                        trade.levels = step.levels
                else:
                    _persist_setup(setup_id, trade, extra={"outcome": cm.OUTCOME_NOT_FILLED})
                    trade.state = cm.STATE_NOT_FILLED
                break

            new_bars = price_data.candles_1m_after(ysym, last_candle_ts, count=120)
            for bar in new_bars:
                close = float(bar.get("c") or 0)
                bar_ts = float(bar.get("ts") or 0)
                if close <= 0 or bar_ts <= last_candle_ts:
                    continue

                step = cm.process_candle_close(trade, close, gap_pct=gap)
                trade.state = step.state
                trade.levels = step.levels
                last_candle_ts = bar_ts
                db.candle_setup_update(setup_id, last_candle_ts=bar_ts)

                if step.action == "enter":
                    opened = await _open_position(
                        ticker=ticker,
                        entry_close=close,
                        levels=step.levels,
                        alert_id=int(row["alert_id"]) if row.get("alert_id") else None,
                        mgr=mgr,
                        trade_lock=trade_lock,
                    )
                    if opened is None:
                        _persist_setup(
                            setup_id,
                            trade,
                            extra={"state": cm.STATE_NOT_FILLED, "outcome": "entry_failed"},
                        )
                        return
                    trade_id, slot_ix, eff_entry = opened
                    trade.levels = cm.levels_after_entry(eff_entry, trade.levels.alert_price, gap_pct=gap)
                    db.candle_setup_update(
                        setup_id,
                        trade_id=trade_id,
                        entry_price=eff_entry,
                        entry_ts=time.time(),
                    )
                    _persist_setup(setup_id, trade)
                    continue

                if step.action == "exit" and trade_id:
                    slot = _slot_by_trade(mgr, int(trade_id))
                    if slot:
                        await _send_market_sell(
                            slot,
                            mgr,
                            str(slot.ticker or ticker),
                            close,
                            step.outcome or "candle_exit",
                            audit_extra={
                                "candle_model": True,
                                "setup_id": setup_id,
                                "outcome": step.outcome,
                                "exit_close": close,
                                "levels": step.levels.to_dict(),
                            },
                        )
                    _persist_setup(
                        setup_id,
                        trade,
                        extra={"outcome": step.outcome, "state": cm.STATE_CLOSED},
                    )
                    return

                if trade.state in (cm.STATE_PROTECTED, cm.STATE_RUNNER):
                    _persist_setup(setup_id, trade)
                    if trade_id:
                        _update_trade_levels(int(trade_id), trade.levels)

        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception("candle monitor setup #%s %s failed", setup_id, ticker)
            await asyncio.sleep(5)


async def _apply_step(
    setup_id: int,
    mgr: SlotManager,
    *,
    trade_lock: asyncio.Lock,
    trade: cm.CandleTradeState,
    step: cm.CandleStepResult,
    ticker: str,
    ysym: str,
    alert_id: Any,
    trade_id: Any,
    slot_ix: int | None,
) -> None:
    trade.state = step.state
    trade.levels = step.levels
    if step.action == "exit" and trade_id:
        bars = price_data.candles_1m(ysym, count=1)
        px = float(step.exit_close or (bars[-1].get("c") if bars else 0) or 0)
        slot = _slot_by_trade(mgr, int(trade_id))
        if slot and px > 0:
            await _send_market_sell(
                slot,
                mgr,
                str(slot.ticker or ticker),
                px,
                step.outcome or "candle_eod",
                audit_extra={"candle_model": True, "setup_id": setup_id, "outcome": step.outcome},
            )
    _persist_setup(setup_id, trade, extra={"outcome": step.outcome})


def _slot_by_trade(mgr: SlotManager, trade_id: int) -> Slot | None:
    for s in mgr.state.slots:
        if s.trade_id == trade_id:
            return s
    return None


def _update_trade_levels(trade_id: int, levels: cm.CandleLevels) -> None:
    if levels.entry_price and levels.initial_stop:
        db.execute(
            "UPDATE trades SET stop=?, tp=?, peak_price=COALESCE(peak_price, ?) WHERE id=?",
            (
                levels.initial_stop,
                levels.runner_trigger or levels.first_trigger,
                levels.entry_price,
                int(trade_id),
            ),
        )


async def _open_position(
    *,
    ticker: str,
    entry_close: float,
    levels: cm.CandleLevels,
    alert_id: int | None,
    mgr: SlotManager,
    trade_lock: asyncio.Lock,
) -> tuple[int, int, float] | None:
    async with trade_lock:
        slot = await mgr.find_open_slot()
        if not slot:
            _log.warning("candle entry skipped — no open slot %s", ticker)
            return None

        t212_code = t212_ai.resolve_ticker(ticker)
        if not t212_code:
            try:
                await t212_ai.refresh_ticker_map(force=False)
            except Exception:
                pass
            t212_code = t212_ai.resolve_ticker(ticker)
        if not t212_code:
            _log.warning("candle entry skipped — %s not on T212", ticker)
            return None

        bl = db.t212_blacklist_get(ticker)
        if bl:
            _log.warning("candle entry skipped — blacklist %s", ticker)
            return None

        entry = float(entry_close)
        stop = float(levels.initial_stop or entry * 0.90)
        stake_gbp = config.candle_stake_gbp()
        capital_usd = stake_gbp * config.GBP_USD_RATE
        precision = t212_ai.quantity_precision(t212_code)
        quantity = t212_ai.snap_quantity(capital_usd / entry, precision)
        min_q = t212_ai.minimum_buy_quantity(t212_code)
        if quantity < min_q:
            quantity = min_q
        quantity = await t212_ai.cap_order_buy_quantity(t212_code, quantity)
        if quantity <= 0:
            _log.warning("candle entry skipped — qty cap zero %s", ticker)
            return None

        if not config.trading_enabled() or not config.t212_credentials_ok():
            trade_id = db.insert(
                """INSERT INTO trades(slot, ticker, alert_id, entry_price, tp, stop, capital_gbp,
                                      quantity, open_ts, status, peak_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    slot.index,
                    t212_code,
                    alert_id,
                    entry,
                    levels.runner_trigger,
                    stop,
                    stake_gbp,
                    quantity,
                    time.time(),
                    "OPEN",
                    entry,
                ),
            )
            await mgr.assign(
                slot,
                ticker=t212_code,
                trade_id=int(trade_id),
                entry=entry,
                tp=float(levels.runner_trigger or entry * 1.25),
                stop=stop,
                capital_gbp=stake_gbp,
            )
            return int(trade_id), slot.index, entry

        try:
            order = await t212_ai.place_market(t212_code, quantity)
        except t212_ai.T212AIError as exc:
            _log.warning("candle entry rejected %s: %s", ticker, exc)
            return None

        if order.get("stub"):
            return None

        fq, favg = await entry_fill.wait_market_fill(
            t212_code,
            quantity,
            timeout_sec=config.FILL_WAIT_TIMEOUT_SECONDS,
        )
        if not fq or fq <= 0:
            _log.warning("candle entry fill timeout %s", ticker)
            return None

        broker_ap = await t212_ai.position_average_entry_usd(t212_code, bypass_cache=True)
        eff_entry = round(float(broker_ap or favg or entry), 6)
        filled_qty = t212_ai.snap_quantity(float(fq), precision)
        deployed_gbp = config.usd_notionals_to_gbp(float(filled_qty) * eff_entry)
        lv = cm.levels_after_entry(eff_entry, levels.alert_price, gap_pct=config.candle_entry_gap_pct())

        trade_id = db.insert(
            """INSERT INTO trades(slot, ticker, alert_id, entry_price, tp, stop, capital_gbp,
                                  quantity, open_ts, status, t212_open_order_id, peak_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slot.index,
                t212_code,
                alert_id,
                eff_entry,
                lv.runner_trigger,
                lv.initial_stop,
                deployed_gbp,
                filled_qty,
                time.time(),
                "OPEN",
                str(order.get("id") or ""),
                eff_entry,
            ),
        )
        await mgr.assign(
            slot,
            ticker=t212_code,
            trade_id=int(trade_id),
            entry=eff_entry,
            tp=float(lv.runner_trigger or eff_entry * 1.25),
            stop=float(lv.initial_stop or eff_entry * 0.9),
            capital_gbp=deployed_gbp,
        )
        _log.info(
            "candle ENTRY #%s %s E=%.4f stop=%.4f protected=%.4f runner=%.4f",
            trade_id,
            ticker,
            eff_entry,
            lv.initial_stop,
            lv.protected_floor,
            lv.runner_trigger,
        )
        return int(trade_id), slot.index, eff_entry

