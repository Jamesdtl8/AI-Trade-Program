"""Per-slot monitor polling loop (default 1s via ``AI_MONITOR_POLL_SECONDS``).

All entries and exits use **market** orders regardless of session.

Exit flow:
1. Send market sell → slot **SELL_PENDING**, trade row **SELL_PENDING**.
2. When the ticker disappears from ``GET /equity/positions`` → release slot (OPEN).
3. When T212 order history returns ``realisedProfitLoss`` → trade **CLOSED** with broker P&L.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import config, db, t212_ai, trail_stop
from .slot_manager import Slot, SlotManager

_log = logging.getLogger("ai_sandbox.position_monitor")


def _trade_open_ts(trade_id: int, setup: dict[str, Any]) -> float:
    cached = float(setup.get("open_ts") or 0.0)
    if cached > 0:
        return cached
    row = db.fetchone("SELECT open_ts FROM trades WHERE id=?", (int(trade_id),))
    open_ts = float(row["open_ts"] or time.time()) if row else time.time()
    setup["open_ts"] = open_ts
    return open_ts


def _append_trade_tick(
    trade_id: int,
    setup: dict[str, Any],
    *,
    entry: float,
    price: float,
    unreal_pct: float | None,
) -> None:
    try:
        open_ts = _trade_open_ts(trade_id, setup)
        unreal_gbp: float | None = None
        qty = float(setup.get("quantity") or 0.0)
        if qty <= 0:
            row = db.fetchone("SELECT quantity FROM trades WHERE id=?", (int(trade_id),))
            if row and row["quantity"] is not None:
                qty = float(row["quantity"] or 0.0)
                setup["quantity"] = qty
        if qty > 0 and entry > 0:
            unreal_gbp = round(config.usd_notionals_to_gbp(qty * (price - entry)), 4)
        db.trade_tick_append(
            int(trade_id),
            open_ts=open_ts,
            price=price,
            unreal_pct=unreal_pct,
            unreal_gbp=unreal_gbp,
        )
    except Exception:
        _log.debug("trade_tick_append failed trade_id=%s", trade_id)


def _paper_mode() -> bool:
    return not config.trading_enabled() or not config.t212_credentials_ok()


async def _try_confirm_close_from_broker(trade_id: int, ticker: str) -> bool:
    """Promote SELL_PENDING → CLOSED when order history has realised GBP P&L."""
    row = db.fetchone(
        "SELECT entry_price, t212_close_order_id, status FROM trades WHERE id=?",
        (int(trade_id),),
    )
    if not row or str(row["status"] or "").upper() != "SELL_PENDING":
        return False
    oid = str(row["t212_close_order_id"] or "").strip()
    if not oid:
        return False
    entry_px = float(row["entry_price"] or 0.0)
    realised = await t212_ai.backfill_closed_trade_pnl_from_broker(
        int(trade_id),
        ticker=ticker,
        close_order_id=oid,
        entry_price=entry_px,
    )
    if realised is None:
        return False
    db.execute(
        """UPDATE trades SET status='CLOSED', exit_reason=COALESCE(exit_reason, 'market_sell')
           WHERE id=? AND status='SELL_PENDING'""",
        (int(trade_id),),
    )
    _log.info("broker confirmed close trade=%s ticker=%s pnl_gbp=%.2f", trade_id, ticker, realised)
    return True


async def _send_market_sell(
    slot: Slot,
    mgr: SlotManager,
    ticker: str,
    price: float,
    reason: str,
    *,
    audit_extra: dict[str, Any] | None = None,
) -> bool:
    """Place market sell and mark slot + SQL as SELL_PENDING."""
    trade_id = slot.trade_id
    if not trade_id:
        return False

    if _paper_mode():
        exit_ts_wall = time.time()
        row_trade = db.fetchone("SELECT entry_price, quantity FROM trades WHERE id=?", (trade_id,))
        entry_px = float(row_trade["entry_price"] or 0) if row_trade else 0.0
        db_qty = float(row_trade["quantity"] or 0) if row_trade else 0.0
        usd_mv = ((float(price) - entry_px) * db_qty) if entry_px > 0 and db_qty > 0 else 0.0
        pnl_gb = config.usd_notionals_to_gbp(usd_mv)
        db.execute(
            """UPDATE trades SET exit_price=?, exit_ts=?, exit_reason=?, status='CLOSED',
                                  pnl_pct=ROUND((?-entry_price)/NULLIF(entry_price,0)*100, 4),
                                  pnl_gbp=?, t212_close_order_id=?
               WHERE id=?""",
            (price, exit_ts_wall, reason, price, pnl_gb, "", trade_id),
        )
        await mgr.release_after_sell(slot)
        _trade_audit_after_close(
            trade_id=trade_id,
            exit_ts=exit_ts_wall,
            reason=reason,
            close_order_id="",
            slot=slot,
            audit_extra=audit_extra,
        )
        return True

    db_qty = _qty_for_trade(trade_id)
    try:
        live = await t212_ai.broker_long_quantity(ticker, retries=2)
    except Exception as exc:
        _log.warning("market sell: positions read failed %s: %s — DB qty fallback", ticker, exc)
        live = db_qty

    if live <= 1e-6:
        _log.warning("market sell skipped — broker already flat %s", ticker)
        await mgr.release_after_sell(slot)
        db.execute(
            """UPDATE trades SET status='SELL_PENDING', exit_ts=?, exit_reason=?
               WHERE id=? AND status='OPEN'""",
            (time.time(), reason, trade_id),
        )
        await _try_confirm_close_from_broker(int(trade_id), ticker)
        return True

    prec = t212_ai.quantity_precision(ticker)
    sell_abs = t212_ai.snap_quantity(float(live), prec)
    sell_signed = -t212_ai.round_qty(sell_abs)
    try:
        res = await t212_ai.place_market(ticker, sell_signed)
    except Exception as exc:
        _log.error("market sell %s signed=%s failed: %s", ticker, sell_signed, exc)
        return False
    if res.get("error"):
        _log.error("market sell %s broker error: %s", ticker, res.get("error"))
        return False
    if not res.get("stub") and res.get("id") in (None, "", 0, "0"):
        _log.error("market sell %s missing order id body=%s", ticker, res)
        return False

    close_oid = str(res.get("id") or "")
    exit_ts_wall = time.time()
    db.execute(
        """UPDATE trades SET status='SELL_PENDING', exit_ts=?, exit_reason=?,
                              t212_close_order_id=?, exit_price=?
           WHERE id=? AND status='OPEN'""",
        (exit_ts_wall, reason, close_oid, float(price), trade_id),
    )
    await mgr.mark_sell_pending(slot, reason=reason)
    _log.info(
        "SELL_PENDING slot=%d ticker=%s reason=%s close_oid=%s qty=%.4f",
        slot.index,
        ticker,
        reason,
        close_oid,
        sell_abs,
    )
    return True


async def run_slot(slot: Slot, mgr: SlotManager, setup: dict[str, Any]) -> None:
    ticker = setup["ticker"]
    entry = float(setup["entry"])
    tp = float(setup["tp"])
    stop_loss_pct = float(config.MAX_STOP_LOSS_PCT)
    highest = float(setup.get("highest_price") or entry)
    if slot.trade_id:
        stored = db.trade_peak_price(int(slot.trade_id))
        if stored is not None and stored > highest:
            highest = stored
            setup["highest_price"] = highest
    # Track the highest stop level seen so far — the stop can never go backward.
    # Without this, tier-boundary crossings (e.g. +9.99% → +10%) widen the trail
    # pct, which would drop the stop level despite peak price going up.
    highest_stop: float = float(setup.get("highest_stop") or 0.0)

    while True:
        try:
            await asyncio.sleep(config.MONITOR_POLL_SECONDS)
            if slot.state == "OPEN":
                return

            if slot.state == "SELL_PENDING":
                trade_id = slot.trade_id
                try:
                    live = await t212_ai.broker_long_quantity(ticker, retries=2)
                except Exception:
                    live = -1.0
                if live >= 0 and live <= 1e-6:
                    await mgr.release_after_sell(slot)
                    if trade_id:
                        await _try_confirm_close_from_broker(int(trade_id), ticker)
                    return
                if trade_id and not _paper_mode():
                    pos_row = await t212_ai.position_row_for_ticker(ticker, bypass_cache=False)
                    if pos_row:
                        try:
                            px = float(pos_row.get("currentPrice") or entry or 0.0)
                        except (TypeError, ValueError):
                            px = entry
                        upct = await t212_ai.position_unrealized_pct(ticker, bypass_cache=False)
                        if upct is None and entry > 0 and px > 0:
                            upct = (px - entry) / entry * 100.0
                        if upct is not None:
                            _append_trade_tick(
                                int(trade_id),
                                setup,
                                entry=entry,
                                price=px,
                                unreal_pct=float(upct),
                            )
                if trade_id:
                    await _try_confirm_close_from_broker(int(trade_id), ticker)
                continue

            if slot.state != "ACTIVE":
                return
            if not config.trading_enabled():
                continue

            broker_entry = await t212_ai.position_average_entry_usd(ticker, bypass_cache=False)
            if broker_entry is not None and broker_entry > 0:
                new_entry = round(float(broker_entry), 6)
                if abs(new_entry - entry) > 0.01 and entry > 0:
                    # Broker is reporting a materially different average entry — log it
                    # so we can audit trail arm-point drift (the trail arm level shifts
                    # with a different entry, which can cause unexpected exits).
                    _log.debug(
                        "broker entry drift %s: DB=%.4f broker=%.4f diff=%.4f (trail arm was %.4f → now %.4f)",
                        ticker, entry, new_entry, new_entry - entry,
                        entry * 1.075, new_entry * 1.075,
                    )
                entry = new_entry

            pos_row = await t212_ai.position_row_for_ticker(ticker, bypass_cache=False)
            if pos_row:
                try:
                    price = float(pos_row.get("currentPrice") or entry or 0.0)
                except (TypeError, ValueError):
                    price = entry
            else:
                price = entry

            unreal_pct = await t212_ai.position_unrealized_pct(ticker, bypass_cache=False)
            if unreal_pct is None and entry > 0 and price > 0:
                unreal_pct = (price - entry) / entry * 100.0

            if unreal_pct is not None:
                if price > highest:
                    highest = price
                    setup["highest_price"] = highest
                    if slot.trade_id:
                        try:
                            db.trade_update_peak_price(int(slot.trade_id), highest)
                        except Exception:
                            pass
                stop_level, trail_active, trail_pct = trail_stop.calculate_stop(
                    entry,
                    highest,
                    float(unreal_pct),
                    hard_stop_pct=stop_loss_pct,
                    highest_stop=highest_stop,
                )
                if trail_active and stop_level > highest_stop:
                    highest_stop = stop_level
                    setup["highest_stop"] = highest_stop
                trail_note = (
                    f" trail={trail_pct}% stop={stop_level:.4f}"
                    if trail_active
                    else f" hard={stop_level:.4f}"
                )
                await mgr.update_slot_pnl(
                    slot,
                    price,
                    unreal_pct,
                    f"pnl={unreal_pct:.2f}%{trail_note}",
                )
                if slot.trade_id:
                    _append_trade_tick(
                        int(slot.trade_id),
                        setup,
                        entry=entry,
                        price=price,
                        unreal_pct=float(unreal_pct),
                    )
                    last_log = float(setup.get("last_monitor_log_ts") or 0.0)
                    if time.time() - last_log >= 30.0:
                        try:
                            db.monitor_log_append(
                                int(slot.trade_id),
                                price=price,
                                unreal_pct=float(unreal_pct),
                                decision=slot.last_decision,
                            )
                            setup["last_monitor_log_ts"] = time.time()
                        except Exception:
                            pass
            else:
                stop_level, trail_active, trail_pct = trail_stop.calculate_stop(
                    entry, highest, 0.0,
                    hard_stop_pct=stop_loss_pct,
                    highest_stop=highest_stop,
                )

            stop_grace_until = float(setup.get("stop_grace_until") or 0.0)

            # Hard stop: -10% unrealized P&L → market sell.
            if (
                unreal_pct is not None
                and unreal_pct <= -stop_loss_pct
                and not (stop_grace_until > 0 and time.time() < stop_grace_until)
            ):
                if await _send_market_sell(
                    slot,
                    mgr,
                    ticker,
                    price,
                    "stop_loss_10pct",
                    audit_extra={
                        "unreal_pct": unreal_pct,
                        "stop_loss_pct": stop_loss_pct,
                        "broker_entry": entry,
                        "highest_price": highest,
                    },
                ):
                    continue
                return

            # Trailing stop ladder (arms permanently on first +7.5% peak; tier from peak gain).
            if (
                not _paper_mode()
                and trail_active
                and price > 0
                and price <= stop_level
                and not (stop_grace_until > 0 and time.time() < stop_grace_until)
            ):
                peak_g = trail_stop.peak_gain_pct(entry, highest)
                trail_reason = (
                    f"trail_breach_{trail_pct:.0f}pct"
                    f"_peak{peak_g:.1f}pct"
                )
                if await _send_market_sell(
                    slot,
                    mgr,
                    ticker,
                    price,
                    trail_reason,
                    audit_extra={
                        "unreal_pct": unreal_pct,
                        "trail_pct": trail_pct,
                        "stop_level": stop_level,
                        "highest_price": highest,
                        "highest_stop": highest_stop,
                        "broker_entry": entry,
                        "peak_gain_pct": peak_g,
                    },
                ):
                    continue
                return

        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception("monitor loop %s crashed", ticker)
            await asyncio.sleep(5)


def _trade_audit_after_close(
    *,
    trade_id: int | None,
    exit_ts: float,
    reason: str,
    close_order_id: str | None,
    slot: Slot,
    audit_extra: dict[str, Any] | None,
) -> None:
    if not trade_id:
        return
    risk: dict[str, Any] = {
        "slot_tp": slot.tp,
        "slot_stop": slot.stop,
        "slot_entry": slot.entry,
        "last_ai_decision": slot.last_decision,
    }
    if audit_extra:
        risk["snapshot"] = audit_extra
    try:
        db.trade_audit_finalize(
            int(trade_id),
            exit_ts=float(exit_ts),
            exit_reason=reason,
            risk_at_exit=risk,
            close_order_id=(close_order_id or None),
        )
    except Exception:
        _log.exception("trade_audit_finalize failed trade_id=%s", trade_id)


def _qty_for_trade(trade_id: int | None) -> float:
    if not trade_id:
        return 0.0
    row = db.fetchone("SELECT quantity FROM trades WHERE id=?", (trade_id,))
    if not row:
        return 0.0
    try:
        return float(row["quantity"] or 0.0)
    except (TypeError, ValueError):
        return 0.0
