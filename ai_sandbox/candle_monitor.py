"""Candle monitor: waiting for entry → unprotected → ramping trail → closed.

Stop loss: 1-minute candle **close** at/below E×0.90 (unprotected). Emergency −18% on 1s tick.
Ramping trail: 1s tick when AI_CANDLE_PROFIT_ON_TICK=1 (+7.5% arm, ratchet floor exit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import candle_model as cm
from . import config, db, entry_fill, massive_bridge, price_data, ramping_trail as rt, t212_ai, tick_bars
from .position_monitor import _send_market_sell, run_slot
from .slot_manager import Slot, SlotManager

_log = logging.getLogger("ai_sandbox.candle_monitor")


def _hydrate_trade(row: dict[str, Any]) -> cm.CandleTradeState:
    levels = cm.CandleLevels.from_dict(json.loads(row.get("levels_json") or "{}"))
    state = str(row.get("state") or cm.STATE_WAITING)
    if state in (cm.STATE_PROTECTED, cm.STATE_RUNNER):
        state = cm.STATE_RAMPING
    return cm.CandleTradeState(state=state, levels=levels)


def _levels_payload(trade: cm.CandleTradeState, entry_meta: dict[str, Any] | None) -> str:
    payload = trade.levels.to_dict()
    if entry_meta:
        payload["entry_meta"] = entry_meta
    return json.dumps(payload)


def _persist_setup(
    setup_id: int,
    trade: cm.CandleTradeState,
    *,
    extra: dict[str, Any] | None = None,
    entry_meta: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "state": trade.state,
        "levels_json": _levels_payload(trade, entry_meta),
    }
    if extra:
        payload.update(extra)
    db.candle_setup_update(setup_id, **payload)


def _append_tick(trade_id: int, *, entry: float, price: float) -> None:
    if trade_id <= 0 or entry <= 0 or price <= 0:
        return
    try:
        row = db.fetchone("SELECT open_ts, quantity FROM trades WHERE id=?", (int(trade_id),))
        open_ts = float(row["open_ts"] or time.time()) if row else time.time()
        qty = float(row["quantity"] or 0) if row else 0.0
        upct = cm.unrealized_pct(entry, price)
        unreal_gbp = round(config.usd_notionals_to_gbp(qty * (price - entry)), 4) if qty > 0 else None
        db.trade_tick_append(
            int(trade_id),
            open_ts=open_ts,
            price=price,
            unreal_pct=upct,
            unreal_gbp=unreal_gbp,
        )
    except Exception:
        _log.debug("candle tick append failed trade_id=%s", trade_id)


async def _apply_exit(
    *,
    setup_id: int,
    mgr: SlotManager,
    trade: cm.CandleTradeState,
    step: cm.CandleStepResult,
    ticker: str,
    trade_id: int | None,
    exit_px: float,
) -> None:
    if step.action != "exit" or not trade_id:
        return
    slot = _slot_by_trade(mgr, int(trade_id))
    if slot and exit_px > 0:
        await _send_market_sell(
            slot,
            mgr,
            str(slot.ticker or ticker),
            exit_px,
            step.outcome or "candle_exit",
            audit_extra={
                "candle_model": True,
                "setup_id": setup_id,
                "outcome": step.outcome,
                "exit_close": exit_px,
                "levels": step.levels.to_dict(),
            },
        )
    trade.state = cm.STATE_CLOSED
    _persist_setup(
        setup_id,
        trade,
        extra={"outcome": step.outcome, "state": cm.STATE_CLOSED},
    )


async def run_setup(
    setup_id: int,
    mgr: SlotManager,
    *,
    trade_lock: asyncio.Lock,
) -> None:
    """Poll T212 ticks until setup reaches a terminal state."""
    row = db.candle_setup_get(setup_id)
    if not row:
        return

    ticker = str(row.get("ticker") or "")
    gap = config.candle_entry_gap_pct()
    trade = _hydrate_trade(row)
    trade_id = row.get("trade_id")
    slot_ix: int | None = None
    alert_price = float(row.get("alert_price") or trade.levels.alert_price or 0)
    levels_raw = json.loads(row.get("levels_json") or "{}")
    entry_meta: dict[str, Any] = levels_raw.get("entry_meta") or {}
    entry_ctx: dict[str, Any] = entry_meta.get("context") or entry_meta
    alert_number = int(entry_ctx.get("alert_number") or entry_meta.get("alert_number") or 99)
    entry_ts_track = float(row.get("entry_ts") or 0)
    last_profit_bar_ts = float(row.get("last_candle_ts") or 0)
    last_stop_1m_ts = entry_ts_track if entry_ts_track > 0 else 0.0
    last_stop_bar_ver = massive_bridge.bar_version(ticker)
    last_stop_1m_end_ms = 0
    peak_unreal_pct = 0.0
    scratch_checked = False
    last_quote_px: float | None = None
    stale_quote_since: float | None = None
    slot_monitor_started = False
    t212_code = t212_ai.resolve_ticker(ticker) or ticker

    profit_builder = tick_bars.TickBarBuilder(config.candle_profit_bar_seconds())

    if (
        trade.state == cm.STATE_WAITING
        and not trade_id
        and config.candle_entry_from_alert()
        and alert_price > 0
    ):
        seed_levels = cm.levels_after_entry(alert_price, alert_price, gap_pct=gap)
        opened = await _open_position(
            ticker=ticker,
            entry_close=alert_price,
            levels=seed_levels,
            alert_id=int(row["alert_id"]) if row.get("alert_id") else None,
            mgr=mgr,
            trade_lock=trade_lock,
        )
        if opened is None:
            adopted = await adopt_broker_fill_for_setup(
                setup_id=int(setup_id),
                ticker=ticker,
                alert_price=alert_price,
                alert_id=int(row["alert_id"]) if row.get("alert_id") else None,
                mgr=mgr,
                trade_lock=trade_lock,
                entry_meta=entry_meta or None,
            )
            if adopted is None:
                _persist_setup(
                    setup_id,
                    trade,
                    extra={"state": cm.STATE_NOT_FILLED, "outcome": "entry_failed"},
                    entry_meta=entry_meta or None,
                )
                return
            trade_id, slot_ix, eff_entry = adopted
            entry_ts_track = time.time()
            last_stop_1m_ts = entry_ts_track
            trade = cm.CandleTradeState(
                state=cm.STATE_UNPROTECTED,
                levels=cm.levels_after_entry(eff_entry, alert_price, gap_pct=gap),
            )
            slot = _slot_by_trade(mgr, int(trade_id))
            if slot:
                slot_monitor_started = True
            _log.info(
                "candle setup #%s %s ENTERED (broker adopt) A=%.4f E=%.4f",
                setup_id,
                ticker,
                alert_price,
                eff_entry,
            )
        else:
            trade_id, slot_ix, eff_entry = opened
            entry_ts_track = time.time()
            last_stop_1m_ts = entry_ts_track
            trade = cm.CandleTradeState(
                state=cm.STATE_UNPROTECTED,
                levels=cm.levels_after_entry(eff_entry, alert_price, gap_pct=gap),
            )
            db.candle_setup_update(
                setup_id,
                trade_id=trade_id,
                entry_price=eff_entry,
                entry_ts=entry_ts_track,
            )
            _persist_setup(setup_id, trade, entry_meta=entry_meta or None)
            slot = _slot_by_trade(mgr, int(trade_id))
            if slot:
                _start_slot_monitor(
                    slot,
                    mgr,
                    raw_ticker=ticker,
                    entry=eff_entry,
                    levels=trade.levels,
                    capital_gbp=config.candle_stake_gbp(),
                )
                slot_monitor_started = True
            _log.info(
                "candle setup #%s %s ENTERED A=%.4f E=%.4f (1m stop / %s ramp)",
                setup_id,
                ticker,
                alert_price,
                eff_entry,
                "1s tick" if config.candle_profit_on_tick_enabled() else f"{config.candle_profit_bar_seconds():.0f}s bar",
            )
    elif trade.state == cm.STATE_WAITING and not trade_id:
        _log.info(
            "candle setup #%s %s waiting 30s close >= %.4f (A=%.4f +%.1f%%)",
            setup_id,
            ticker,
            trade.levels.entry_trigger,
            trade.levels.alert_price,
            gap,
        )

    poll_sec = min(config.candle_poll_seconds(), config.monitor_poll_seconds())

    while trade.state not in (cm.STATE_CLOSED, cm.STATE_NOT_FILLED):
        try:
            await asyncio.sleep(poll_sec)

            if config.us_after_hours_complete():
                eod_px: float | None = None
                try:
                    eod_px, _ = await t212_ai.broker_quote_long_qty(t212_code, bypass_cache=True)
                except Exception:
                    eod_px = None
                if eod_px and eod_px > 0:
                    profit_builder.add(float(eod_px))
                final_close = float(eod_px or trade.levels.entry_price or 0)
                if final_close > 0:
                    step = cm.end_of_day_close(trade, final_close)
                    if step.action == "exit" and trade_id:
                        await _apply_exit(
                            setup_id=setup_id,
                            mgr=mgr,
                            trade=trade,
                            step=step,
                            ticker=ticker,
                            trade_id=int(trade_id),
                            exit_px=final_close,
                        )
                        return
                    trade.state = step.state
                    _persist_setup(setup_id, trade, extra={"outcome": step.outcome}, entry_meta=entry_meta or None)
                else:
                    _persist_setup(setup_id, trade, extra={"outcome": cm.OUTCOME_NOT_FILLED}, entry_meta=entry_meta or None)
                    trade.state = cm.STATE_NOT_FILLED
                break

            t212_px: float | None = None
            try:
                t212_px, _ = await t212_ai.broker_quote_long_qty(t212_code, bypass_cache=True)
            except Exception:
                t212_px = None

            massive_px = massive_bridge.live_price(ticker)
            px_f = float(massive_px or t212_px or 0)
            if px_f <= 0:
                continue

            now = time.time()
            if trade_id and trade.state == cm.STATE_UNPROTECTED:
                if massive_bridge.enabled():
                    await massive_bridge.wait_bar_update(
                        ticker, since_version=last_stop_bar_ver, timeout=poll_sec
                    )
                    ver_now = massive_bridge.bar_version(ticker)
                    if ver_now > last_stop_bar_ver:
                        last_stop_bar_ver = ver_now
                        bar = massive_bridge.last_closed_1m_bar(ticker)
                        close_1m = massive_bridge.last_closed_1m_close(ticker)
                        end_ms = int(bar.get("e") or 0) if bar else 0
                        if (
                            close_1m
                            and end_ms > last_stop_1m_end_ms
                            and end_ms / 1000.0 > last_stop_1m_ts
                        ):
                            last_stop_1m_end_ms = end_ms
                            last_stop_1m_ts = end_ms / 1000.0
                            step = cm.process_stop_candle_1m(trade, close=float(close_1m))
                            trade.state = step.state
                            trade.levels = step.levels
                            if step.action == "exit":
                                _log.warning(
                                    "candle setup #%s %s Massive 1m close %.4f <= stop %.4f — exit",
                                    setup_id,
                                    ticker,
                                    close_1m,
                                    trade.levels.initial_stop,
                                )
                                await _apply_exit(
                                    setup_id=setup_id,
                                    mgr=mgr,
                                    trade=trade,
                                    step=step,
                                    ticker=ticker,
                                    trade_id=int(trade_id),
                                    exit_px=float(close_1m),
                                )
                                return
                else:
                    for bar in price_data.candles_1m_after(
                        ticker, last_stop_1m_ts, t212_price=px_f
                    ):
                        bar_ts = float(bar.get("ts") or 0)
                        close_1m = float(bar.get("c") or bar.get("close") or 0)
                        if bar_ts <= last_stop_1m_ts or close_1m <= 0:
                            continue
                        last_stop_1m_ts = bar_ts
                        step = cm.process_stop_candle_1m(trade, close=close_1m)
                        trade.state = step.state
                        trade.levels = step.levels
                        if step.action == "exit":
                            await _apply_exit(
                                setup_id=setup_id,
                                mgr=mgr,
                                trade=trade,
                                step=step,
                                ticker=ticker,
                                trade_id=int(trade_id),
                                exit_px=close_1m,
                            )
                            return

            massive_bridge.register_symbols([ticker])
            if last_quote_px is not None and abs(px_f - last_quote_px) < 1e-8:
                if stale_quote_since is None:
                    stale_quote_since = now
            else:
                last_quote_px = px_f
                stale_quote_since = None

            if (
                trade_id
                and stale_quote_since is not None
                and trade.state == cm.STATE_RAMPING
                and now - stale_quote_since >= config.candle_stale_quote_seconds()
            ):
                _log.warning(
                    "candle setup #%s %s stale quote %.4f for %.0fs — halt flatten",
                    setup_id,
                    ticker,
                    px_f,
                    now - stale_quote_since,
                )
                step = cm.CandleStepResult(
                    state=cm.STATE_CLOSED,
                    levels=trade.levels,
                    action="exit",
                    outcome=cm.OUTCOME_STALE_QUOTE,
                    exit_close=px_f,
                )
                await _apply_exit(
                    setup_id=setup_id,
                    mgr=mgr,
                    trade=trade,
                    step=step,
                    ticker=ticker,
                    trade_id=int(trade_id),
                    exit_px=px_f,
                )
                return

            if config.candle_intrabar_ramp_exit_enabled() and trade_id:
                intrabar = cm.check_intrabar_exit(trade, price=px_f)
                if intrabar and intrabar.action == "exit":
                    await _apply_exit(
                        setup_id=setup_id,
                        mgr=mgr,
                        trade=trade,
                        step=intrabar,
                        ticker=ticker,
                        trade_id=int(trade_id),
                        exit_px=px_f,
                    )
                    return

            if trade_id and trade.levels.entry_price:
                entry_px = float(trade.levels.entry_price)
                _append_tick(int(trade_id), entry=entry_px, price=px_f)
                peak_unreal_pct = max(peak_unreal_pct, cm.unrealized_pct(entry_px, px_f))

                emerg = cm.process_emergency_stop_tick(trade, price=px_f)
                if emerg.action == "exit":
                    _log.warning(
                        "candle setup #%s %s emergency tick %.4f (≤−%.0f%%) — market sell",
                        setup_id,
                        ticker,
                        px_f,
                        config.candle_emergency_stop_pct(),
                    )
                    await _apply_exit(
                        setup_id=setup_id,
                        mgr=mgr,
                        trade=trade,
                        step=emerg,
                        ticker=ticker,
                        trade_id=int(trade_id),
                        exit_px=px_f,
                    )
                    return

            if (
                config.candle_profit_on_tick_enabled()
                and trade_id
                and trade.state in (cm.STATE_UNPROTECTED, cm.STATE_RAMPING)
            ):
                step = cm.process_profit_tick(trade, price=px_f, gap_pct=gap)
                trade.state = step.state
                trade.levels = step.levels
                if step.action == "exit":
                    await _apply_exit(
                        setup_id=setup_id,
                        mgr=mgr,
                        trade=trade,
                        step=step,
                        ticker=ticker,
                        trade_id=int(trade_id),
                        exit_px=px_f,
                    )
                    return
                if trade.state == cm.STATE_RAMPING:
                    _persist_setup(setup_id, trade, entry_meta=entry_meta or None)
                    _update_trade_levels(int(trade_id), trade.levels, trade.state)
                    slot = _slot_by_trade(mgr, int(trade_id))
                    if slot:
                        _set_ramping_slot_decision(slot, trade.levels)

            if (
                config.candle_scratch_90s_enabled()
                and trade_id
                and not scratch_checked
                and entry_ts_track > 0
                and now - entry_ts_track >= 90.0
            ):
                scratch_checked = True
                if _should_scratch_90s(
                    peak_unreal_pct=peak_unreal_pct,
                    alert_number=alert_number,
                    entry_ctx=entry_ctx,
                ):
                    slot = _slot_by_trade(mgr, int(trade_id))
                    if slot:
                        await _send_market_sell(
                            slot,
                            mgr,
                            str(slot.ticker or ticker),
                            px_f,
                            "momentum_scratch_90s",
                            audit_extra={
                                "candle_model": True,
                                "setup_id": setup_id,
                                "peak_unreal_pct": peak_unreal_pct,
                                "alert_number": alert_number,
                            },
                        )
                    trade.state = cm.STATE_CLOSED
                    _persist_setup(
                        setup_id,
                        trade,
                        extra={"outcome": "momentum_scratch_90s", "state": cm.STATE_CLOSED},
                        entry_meta=entry_meta or None,
                    )
                    return

            profit_bar = None
            if not (config.candle_profit_on_tick_enabled() and trade_id):
                profit_bar = profit_builder.add(px_f, now)
            if profit_bar is not None:
                bar = profit_bar
                bar_ts = float(bar.ts)
                if bar_ts > last_profit_bar_ts:
                    last_profit_bar_ts = bar_ts
                    db.candle_setup_update(setup_id, last_candle_ts=bar_ts)

                if bar.period_sec <= config.candle_profit_bar_seconds() + 0.5:
                    step = cm.process_profit_bar(
                        trade, close=bar.close, avg=bar.avg, gap_pct=gap
                    )
                    trade.state = step.state
                    trade.levels = step.levels

                    if step.action == "enter":
                        opened = await _open_position(
                            ticker=ticker,
                            entry_close=float(bar.close),
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
                                entry_meta=entry_meta or None,
                            )
                            return
                        trade_id, slot_ix, eff_entry = opened
                        entry_ts_track = time.time()
                        last_stop_1m_ts = entry_ts_track
                        trade.levels = cm.levels_after_entry(
                            eff_entry, trade.levels.alert_price, gap_pct=gap
                        )
                        db.candle_setup_update(
                            setup_id,
                            trade_id=trade_id,
                            entry_price=eff_entry,
                            entry_ts=entry_ts_track,
                        )
                        _persist_setup(setup_id, trade, entry_meta=entry_meta or None)
                        slot = _slot_by_trade(mgr, int(trade_id))
                        if slot and not slot_monitor_started:
                            _start_slot_monitor(
                                slot,
                                mgr,
                                raw_ticker=ticker,
                                entry=eff_entry,
                                levels=trade.levels,
                                capital_gbp=config.candle_stake_gbp(),
                            )
                            slot_monitor_started = True
                        continue

                    if step.action == "exit" and trade_id:
                        await _apply_exit(
                            setup_id=setup_id,
                            mgr=mgr,
                            trade=trade,
                            step=step,
                            ticker=ticker,
                            trade_id=int(trade_id),
                            exit_px=float(bar.close),
                        )
                        return

                    if trade.state == cm.STATE_RAMPING:
                        _persist_setup(setup_id, trade, entry_meta=entry_meta or None)
                        if trade_id:
                            _update_trade_levels(int(trade_id), trade.levels, trade.state)
                        slot = _slot_by_trade(mgr, int(trade_id)) if trade_id else None
                        if slot:
                            _set_ramping_slot_decision(slot, trade.levels)

        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception("candle monitor setup #%s %s failed", setup_id, ticker)
            await asyncio.sleep(5)


def _set_ramping_slot_decision(slot: Slot, levels: cm.CandleLevels) -> None:
    lv = levels
    ratchet = lv.ratcheted_peak_pct
    width = lv.trail_width_pp
    floor_g = lv.trail_floor_gain_pct
    sample = "1s tick" if config.candle_profit_on_tick_enabled() else f"{config.candle_profit_bar_seconds():.0f}s avg"
    if ratchet is not None and width is not None and floor_g is not None:
        slot.last_decision = (
            f"RAMPING (+{ratchet:.1f}% peak, {width:.1f}% trail, "
            f"floor +{floor_g:.1f}% @{sample})"
        )
    else:
        slot.last_decision = f"RAMPING (+7.5% armed, ratchet trail @{sample})"


def _should_scratch_90s(
    *,
    peak_unreal_pct: float,
    alert_number: int,
    entry_ctx: dict[str, Any],
) -> bool:
    """90s dead-trade scratch; squeeze names scratch if never went green."""
    squeeze = bool(
        entry_ctx.get("zero_borrow")
        or entry_ctx.get("reg_sho")
        or entry_ctx.get("potential_squeeze")
    )
    if alert_number > config.candle_scratch_max_alert():
        return False
    if peak_unreal_pct >= config.candle_scratch_peak_pct():
        return False
    if peak_unreal_pct > config.candle_scratch_green_pct():
        return False
    if squeeze:
        if not config.candle_squeeze_dead_scratch_enabled():
            return False
        return peak_unreal_pct < config.candle_squeeze_dead_scratch_peak_pct()
    return True


def _start_slot_monitor(
    slot: Slot,
    mgr: SlotManager,
    *,
    raw_ticker: str,
    entry: float,
    levels: cm.CandleLevels,
    capital_gbp: float,
) -> None:
    """Spawn position monitor for sell-fill watchdog on candle trades."""
    if slot.state not in ("ACTIVE", "SELL_PENDING"):
        return
    setup: dict[str, Any] = {
        "ticker": str(slot.ticker or raw_ticker),
        "raw_ticker": raw_ticker,
        "entry": float(entry),
        "tp": _arm_trigger_price(float(entry)),
        "stop": float(levels.initial_stop or config.candle_initial_stop_price(entry)),
        "capital_gbp": float(capital_gbp),
        "highest_price": float(entry),
        "candle_model": True,
    }
    asyncio.create_task(
        run_slot(slot, mgr, setup),
        name=f"candle-slot-{slot.index}-{raw_ticker}",
    )


def _slot_by_trade(mgr: SlotManager, trade_id: int) -> Slot | None:
    for s in mgr.state.slots:
        if s.trade_id == trade_id:
            return s
    return None


def _arm_trigger_price(entry: float) -> float:
    return round(float(entry) * (1.0 + rt.ARM_GAIN_PCT / 100.0), 6)


def _update_trade_levels(trade_id: int, levels: cm.CandleLevels, state: str) -> None:
    entry = levels.entry_price
    if not entry or not levels.initial_stop:
        return
    peak_px = float(entry)
    if levels.peak_gain_pct:
        peak_px = round(float(entry) * (1.0 + float(levels.peak_gain_pct) / 100.0), 6)
    tp = (
        float(levels.trail_floor_price)
        if state == cm.STATE_RAMPING and levels.trail_floor_price
        else _arm_trigger_price(float(entry))
    )
    db.execute(
        "UPDATE trades SET stop=?, tp=?, peak_price=? WHERE id=?",
        (levels.initial_stop, tp, peak_px, int(trade_id)),
    )


async def adopt_broker_fill_for_setup(
    *,
    setup_id: int,
    ticker: str,
    alert_price: float,
    alert_id: int | None,
    mgr: SlotManager,
    trade_lock: asyncio.Lock,
    entry_meta: dict[str, Any] | None = None,
    qty_hint: float | None = None,
    entry_hint: float | None = None,
) -> tuple[int, int, float] | None:
    """Broker shows a long but SQL/setup missed the fill — adopt and link candle setup."""
    t212_code = t212_ai.resolve_ticker(ticker) or ticker
    min_q = t212_ai.minimum_buy_quantity(t212_code)
    fq: float | None = None
    favg: float | None = None
    if qty_hint is not None and float(qty_hint) > 0:
        fq = float(qty_hint)
        favg = float(entry_hint) if entry_hint and entry_hint > 0 else None
    if not fq or fq <= 0:
        fq_r, favg_r = await entry_fill.recover_position_fill(
            t212_code, min_qty=min_q, requested_qty=0.0
        )
        fq, favg = fq_r, favg_r
    if not fq or fq <= 0:
        return None

    gap = config.candle_entry_gap_pct()
    async with trade_lock:
        setup_row = db.candle_setup_get(setup_id)
        if not setup_row or setup_row.get("trade_id"):
            return None

        slot = await mgr.find_open_slot()
        if not slot:
            _log.warning("candle adopt skipped — no open slot %s", ticker)
            return None

        precision = t212_ai.quantity_precision(t212_code)
        broker_ap = await t212_ai.position_average_entry_usd(t212_code, bypass_cache=False)
        eff_entry = round(float(broker_ap or favg or entry_hint or alert_price), 6)
        filled_qty = t212_ai.snap_quantity(float(fq), precision)
        deployed_gbp = config.usd_notionals_to_gbp(float(filled_qty) * eff_entry)
        lv = cm.levels_after_entry(eff_entry, alert_price, gap_pct=gap)
        trade_state = cm.CandleTradeState(state=cm.STATE_UNPROTECTED, levels=lv)

        trade_id = db.insert(
            """INSERT INTO trades(slot, ticker, alert_id, entry_price, tp, stop, capital_gbp,
                                  quantity, open_ts, status, peak_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slot.index,
                t212_code,
                alert_id,
                eff_entry,
                _arm_trigger_price(eff_entry),
                lv.initial_stop,
                deployed_gbp,
                filled_qty,
                time.time(),
                "OPEN",
                eff_entry,
            ),
        )
        await mgr.assign(
            slot,
            ticker=t212_code,
            trade_id=int(trade_id),
            entry=eff_entry,
            tp=_arm_trigger_price(eff_entry),
            stop=float(lv.initial_stop or config.candle_initial_stop_price(eff_entry)),
            capital_gbp=deployed_gbp,
        )
        entry_ts = time.time()
        db.candle_setup_update(
            setup_id,
            trade_id=int(trade_id),
            entry_price=eff_entry,
            entry_ts=entry_ts,
            state=cm.STATE_UNPROTECTED,
            outcome=None,
        )
        _persist_setup(setup_id, trade_state, entry_meta=entry_meta)
        _start_slot_monitor(
            slot,
            mgr,
            raw_ticker=ticker,
            entry=eff_entry,
            levels=lv,
            capital_gbp=deployed_gbp,
        )
        t212_ai.clear_pending_entry(t212_code)
        _log.warning(
            "candle setup #%s %s ADOPTED broker fill E=%.4f qty=%.4f (missed fill watchdog)",
            setup_id,
            ticker,
            eff_entry,
            filled_qty,
        )
        return int(trade_id), slot.index, eff_entry


async def _entry_chase_blocks_buy(
    *,
    ticker: str,
    t212_code: str,
    alert_price: float,
    alert_id: int | None,
) -> bool:
    """Return True when live quote exceeds alert by more than the chase cap."""
    cap = config.candle_max_entry_chase_pct()
    alert_px = float(alert_price or 0)
    if cap <= 0 or alert_px <= 0:
        return False
    try:
        live_px, _ = await t212_ai.broker_quote_long_qty(t212_code, bypass_cache=True)
    except Exception:
        return False
    if not live_px or float(live_px) <= 0:
        return False
    chase = (float(live_px) - alert_px) / alert_px * 100.0
    if chase <= cap:
        return False
    if alert_id:
        try:
            db.record_candle_decision_episode(
                ticker,
                int(alert_id),
                {"decision": "TRADE_NOW", "notes": "entry blocked — chase cap"},
                exec_block_reason="entry_chase_exceeded",
            )
        except Exception:
            _log.exception("candle history chase block %s", ticker)
    _log.warning(
        "candle entry skipped — %s chase %.1f%% > %.1f%% cap (alert %.4f live %.4f)",
        ticker,
        chase,
        cap,
        alert_px,
        float(live_px),
    )
    return True


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
            tag = f"blacklist:{bl.get('reason')}"
            if alert_id:
                try:
                    db.record_candle_decision_episode(
                        ticker,
                        int(alert_id),
                        {"decision": "TRADE_NOW", "notes": "entry blocked at broker"},
                        exec_block_reason=tag,
                    )
                except Exception:
                    _log.exception("candle history blacklist block %s", ticker)
            _log.warning("candle entry skipped — blacklist %s", ticker)
            return None

        entry = float(entry_close)
        stop = float(levels.initial_stop or config.candle_initial_stop_price(entry))
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
                    _arm_trigger_price(entry),
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
                tp=_arm_trigger_price(entry),
                stop=stop,
                capital_gbp=stake_gbp,
            )
            return int(trade_id), slot.index, entry

        if await _entry_chase_blocks_buy(
            ticker=ticker,
            t212_code=t212_code,
            alert_price=float(levels.alert_price or entry),
            alert_id=alert_id,
        ):
            return None

        t212_ai.register_pending_entry(t212_code)
        try:
            order = await t212_ai.place_market(t212_code, quantity)
        except t212_ai.T212AIError as exc:
            t212_ai.clear_pending_entry(t212_code)
            brief = str(exc)[:220]
            if isinstance(exc.body, dict):
                for k in ("description", "detail", "message", "errorMessage"):
                    v = exc.body.get(k)
                    if v:
                        brief = str(v)[:220]
                        break
            kind = "close_only_mode" if t212_ai.is_close_only_error(exc.body) else "order_reject"
            if kind == "close_only_mode":
                try:
                    db.t212_blacklist_add(
                        ticker,
                        reason="CLOSE_ONLY",
                        detail=brief[:500] if brief else None,
                        t212_instrument=t212_code or None,
                    )
                except Exception:
                    _log.exception("t212_blacklist_add CLOSE_ONLY %s", ticker)
            try:
                db.record_candle_entry_rejected(
                    slot_index=slot.index,
                    ticker=ticker,
                    t212_code=t212_code,
                    alert_id=alert_id,
                    entry=entry,
                    stop=stop,
                    quantity=float(quantity),
                    brief=brief,
                    kind=kind,
                    http_status=int(exc.status or 0),
                    body=exc.body,
                )
            except Exception:
                _log.exception("candle broker reject history %s", ticker)
            _log.warning("candle entry rejected %s: %s", ticker, brief)
            return None

        if order.get("stub"):
            t212_ai.clear_pending_entry(t212_code)
            return None

        fq, favg = await entry_fill.wait_market_fill(
            t212_code,
            quantity,
            timeout_sec=config.FILL_WAIT_TIMEOUT_SECONDS,
        )
        if not fq or fq <= 0:
            min_q = t212_ai.minimum_buy_quantity(t212_code)
            fq, favg = await entry_fill.recover_position_fill(
                t212_code, min_qty=min_q, requested_qty=float(quantity)
            )
        if not fq or fq <= 0:
            live_q = await t212_ai.broker_long_quantity(t212_code, retries=1)
            if live_q and live_q > 0:
                _log.warning(
                    "candle entry fill timeout %s — broker still long %.4f (pending for reconciler)",
                    ticker,
                    live_q,
                )
                return None
            t212_ai.clear_pending_entry(t212_code)
            _log.warning("candle entry fill timeout %s — broker flat", ticker)
            return None
        t212_ai.clear_pending_entry(t212_code)

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
                _arm_trigger_price(eff_entry),
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
            tp=_arm_trigger_price(eff_entry),
            stop=float(lv.initial_stop or config.candle_initial_stop_price(eff_entry)),
            capital_gbp=deployed_gbp,
        )
        _log.info(
            "candle ENTRY #%s %s E=%.4f stop=%.4f arm=+%.1f%% (%.4f)",
            trade_id,
            ticker,
            eff_entry,
            lv.initial_stop,
            rt.ARM_GAIN_PCT,
            _arm_trigger_price(eff_entry),
        )
        return int(trade_id), slot.index, eff_entry
