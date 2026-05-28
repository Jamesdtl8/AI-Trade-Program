"""Per-slot monitor polling loop (default 1s via ``AI_MONITOR_POLL_SECONDS``).

Simple exit rules (live T212):

- **Stop loss:** when broker unrealized P&L from ``GET /equity/positions`` is
  at or below ``-MAX_STOP_LOSS_PCT`` (default 10%), cancel any resting TP limit
  and **market sell**.
- **Take profit (regular hours):** one DAY **limit sell** at confirmed entry
  × (1 + ``AI_TAKE_PROFIT_PCT``) — default +7.5%.
- **Take profit (extended / pre-post):** limits are not available — **market sell**
  when unrealized P&L reaches ``AI_TAKE_PROFIT_PCT``.

All monitors read the shared positions snapshot populated by
``t212_ai.run_positions_poller`` (~1 Hz, single HTTP producer app-wide).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import config, db, t212_ai
from .slot_manager import Slot, SlotManager

_log = logging.getLogger("ai_sandbox.position_monitor")


def _paper_mode() -> bool:
    return not config.trading_enabled() or not config.t212_credentials_ok()


def _use_limit_take_profit() -> bool:
    """US regular session with live broker — DAY limit exits are viable on T212."""
    return config.market_phase() == "regular" and not _paper_mode()


async def cancel_pending_tp_limit(slot: Slot) -> None:
    oid = slot.take_profit_order_id
    if not oid:
        return
    slot.take_profit_order_id = None
    if not config.t212_credentials_ok():
        return
    try:
        await t212_ai.cancel_order(oid)
    except Exception:
        _log.warning("cancel pending AI take-profit limit order %s failed", oid)


async def cancel_resting_exit_orders(slot: Slot) -> None:
    """Cancel any resting DAY take-profit limit before a new broker action."""
    await cancel_pending_tp_limit(slot)


async def _place_tp_limit_sell(
    slot: Slot,
    ticker: str,
    entry: float,
    tp: float,
    trade_id: int | None,
) -> bool:
    """Place the single allowed DAY limit sell at *tp* (regular session only)."""
    if slot.take_profit_order_id or not _use_limit_take_profit():
        return False
    db_qty = _qty_for_trade(trade_id)
    try:
        live_q = await t212_ai.account_long_quantity(ticker)
    except Exception as exc:
        _log.warning("TP limit: positions read failed %s: %s — DB qty fallback", ticker, exc)
        live_q = db_qty
    sell_abs = live_q if live_q > 1e-6 else db_qty
    if sell_abs <= 1e-6:
        return False
    prec = t212_ai.quantity_precision(ticker)
    sell_abs = t212_ai.snap_quantity(float(sell_abs), prec)
    sell_signed = -t212_ai.round_qty(sell_abs)
    try:
        res = await t212_ai.place_limit(ticker, sell_signed, tp)
    except Exception as exc:
        _log.error(
            "TP limit sell slot=%s %s qty=%s @ %.4f failed: %s",
            slot.index,
            ticker,
            sell_signed,
            tp,
            exc,
        )
        return False
    if not isinstance(res, dict):
        return False
    oid = res.get("id")
    if res.get("stub") or oid in (None, "", 0, "0"):
        return False
    slot.take_profit_order_id = str(oid)
    _log.info(
        "slot %d %s DAY limit TP @ %.4f (entry %.4f +%.1f%%) qty=%s order=%s",
        slot.index,
        ticker,
        tp,
        entry,
        config.AI_TAKE_PROFIT_PCT,
        sell_signed,
        oid,
    )
    return True


async def run_slot(slot: Slot, mgr: SlotManager, setup: dict[str, Any]) -> None:
    ticker = setup["ticker"]
    entry = float(setup["entry"])
    tp = float(setup["tp"])
    stop_loss_pct = float(config.MAX_STOP_LOSS_PCT)
    tp_limit_attempted = False

    if _use_limit_take_profit() and entry > 0:
        tp_limit_attempted = await _place_tp_limit_sell(slot, ticker, entry, tp, slot.trade_id)

    while True:
        try:
            await asyncio.sleep(config.MONITOR_POLL_SECONDS)
            if slot.state != "ACTIVE":
                return
            if not config.trading_enabled():
                continue

            broker_entry = await t212_ai.position_average_entry_usd(ticker, bypass_cache=False)
            if broker_entry is not None and broker_entry > 0:
                entry = round(float(broker_entry), 6)
                tp = config.profit_target_price(entry)

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
                await mgr.update_slot_pnl(
                    slot,
                    price,
                    unreal_pct,
                    f"pnl={unreal_pct:.2f}% tp={tp:.4f}",
                )

            phase = config.market_phase()

            # Resting TP limit filled → broker flat.
            if slot.take_profit_order_id and not _paper_mode():
                try:
                    live_tp_done = await t212_ai.account_long_quantity(ticker)
                except Exception:
                    live_tp_done = -1.0
                if live_tp_done >= 0 and live_tp_done <= 1e-6:
                    oid_done = slot.take_profit_order_id
                    await _exit(
                        slot,
                        mgr,
                        ticker,
                        price,
                        "tp_limit_filled",
                        audit_extra={
                            "unreal_pct": unreal_pct,
                            "tp_target": tp,
                            "broker_entry": entry,
                            "take_profit_order_id": oid_done,
                        },
                        skip_market_sell=True,
                        close_order_id_override=str(oid_done),
                    )
                    return

            # Hard stop: -10% unrealized P&L → market sell.
            stop_grace_until = float(setup.get("stop_grace_until") or 0.0)
            if (
                unreal_pct is not None
                and unreal_pct <= -stop_loss_pct
                and not (stop_grace_until > 0 and time.time() < stop_grace_until)
            ):
                await _exit(
                    slot,
                    mgr,
                    ticker,
                    price,
                    "stop_loss_10pct",
                    audit_extra={
                        "unreal_pct": unreal_pct,
                        "stop_loss_pct": stop_loss_pct,
                        "broker_entry": entry,
                        "tp_target": tp,
                        "market_phase": phase,
                    },
                )
                return

            # Regular hours: ensure exactly one resting TP limit exists.
            if (
                _use_limit_take_profit()
                and not slot.take_profit_order_id
                and not tp_limit_attempted
                and entry > 0
                and tp > 0
            ):
                tp_limit_attempted = await _place_tp_limit_sell(
                    slot, ticker, entry, tp, slot.trade_id
                )

            # Extended / pre-post: market sell at +7.5% (no limit orders).
            if (
                phase != "regular"
                and not _paper_mode()
                and unreal_pct is not None
                and unreal_pct >= config.AI_TAKE_PROFIT_PCT
            ):
                await _exit(
                    slot,
                    mgr,
                    ticker,
                    price,
                    "tp_extended_market",
                    audit_extra={
                        "unreal_pct": unreal_pct,
                        "take_profit_pct": config.AI_TAKE_PROFIT_PCT,
                        "broker_entry": entry,
                        "market_phase": phase,
                    },
                )
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


async def _poll_until_flat(ticker: str, timeout_sec: float) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            q = await t212_ai.account_long_quantity(ticker)
            if q <= 1e-6:
                return True
        except Exception:
            pass
        await asyncio.sleep(2.0)
    return False


async def _exit(
    slot: Slot,
    mgr: SlotManager,
    ticker: str,
    price: float,
    reason: str,
    *,
    audit_extra: dict[str, Any] | None = None,
    skip_market_sell: bool = False,
    close_order_id_override: str | None = None,
) -> None:
    """Close the trade; T212 long exits normally use negative *market* quantity."""
    trade_id = slot.trade_id
    db_qty = _qty_for_trade(trade_id)
    qty_at_exit_start = float(db_qty)
    qty_closed_for_pnl = 0.0
    row_trade = db.fetchone(
        "SELECT entry_price, t212_open_order_id FROM trades WHERE id=?",
        (trade_id,),
    )
    entry_px = float(row_trade["entry_price"] or 0) if row_trade else 0.0
    open_oid = str(row_trade["t212_open_order_id"] or "").strip() if row_trade else ""

    if not config.trading_enabled() or not config.t212_credentials_ok():
        slot.take_profit_order_id = None
        usd_mv = ((float(price) - entry_px) * db_qty) if entry_px > 0 and db_qty > 0 else 0.0
        pnl_gb = config.usd_notionals_to_gbp(usd_mv)
        exit_ts_wall = time.time()
        db.execute(
            """UPDATE trades SET exit_price=?, exit_ts=?, exit_reason=?, status='CLOSED',
                                  pnl_pct=ROUND((?-entry_price)/NULLIF(entry_price,0)*100, 4),
                                  pnl_gbp=?,
                                  t212_close_order_id=?
               WHERE id=?""",
            (price, exit_ts_wall, reason, price, pnl_gb, "", trade_id),
        )
        await mgr.close(slot, exit_price=price, reason=reason)
        _trade_audit_after_close(
            trade_id=trade_id,
            exit_ts=exit_ts_wall,
            reason=reason,
            close_order_id="",
            slot=slot,
            audit_extra=audit_extra,
        )
        _log.info("EXIT (sim) slot=%d ticker=%s price=%.4f reason=%s", slot.index, ticker, price, reason)
        return

    res: dict[str, Any] = {}

    if skip_market_sell:
        slot.take_profit_order_id = None
        try:
            live_flat = await t212_ai.account_long_quantity(ticker)
        except Exception as exc:
            _log.warning(
                "exit skip_market: positions failed %s: %s — not finalizing", ticker, exc,
            )
            return
        if live_flat > 1e-6:
            _log.error(
                "exit skip_market: broker still long %s qty=%.6f (%s) — abort",
                ticker,
                live_flat,
                reason,
            )
            return

        qty_closed_for_pnl = qty_at_exit_start if qty_at_exit_start > 1e-6 else 0.0
    else:
        await cancel_resting_exit_orders(slot)

        try:
            live = await t212_ai.account_long_quantity(ticker)
        except Exception as exc:
            _log.warning("exit: positions read failed %s: %s — using DB qty fallback", ticker, exc)
            live = db_qty

        if live <= 1e-6 and db_qty > 1e-6:
            _log.warning(
                "exit: broker already flat %s but DB qty=%.4f — reconciler will usually fix this",
                ticker,
                db_qty,
            )

        prec = t212_ai.quantity_precision(ticker)
        sell_abs = live if live > 1e-6 else db_qty
        if sell_abs > 1e-6:
            sell_abs = t212_ai.snap_quantity(float(sell_abs), prec)
            qty_closed_for_pnl = float(sell_abs)
            sell_signed = -t212_ai.round_qty(sell_abs)
            try:
                res = await t212_ai.place_market(ticker, sell_signed)
            except Exception as exc:
                _log.error("market sell %s signed=%s failed: %s", ticker, sell_signed, exc)
                return
            if res.get("error"):
                _log.error("market sell %s broker error: %s", ticker, res.get("error"))
                return
            if not res.get("stub") and res.get("id") in (None, "", 0, "0"):
                _log.error("market sell %s missing order id body=%s", ticker, res)
                return
            if not await _poll_until_flat(ticker, float(config.EXIT_FLAT_POLL_TIMEOUT_S)):
                _log.error(
                    "exit verify failed — %s still showing long after sell (order id=%s)",
                    ticker,
                    res.get("id"),
                )
                return

    exit_ts_wall = time.time()
    close_oid = close_order_id_override or str(res.get("id") or "")
    mv = ((float(price) - entry_px) * qty_closed_for_pnl) if entry_px > 0 and qty_closed_for_pnl > 0 else 0.0
    pnl_gb = config.usd_notionals_to_gbp(mv)
    exit_px = float(price)
    if close_oid and not res.get("stub"):
        try:
            hist_px, realised_gbp, _fq = await t212_ai.fetch_order_fill_from_history(
                close_oid,
                ticker,
                initial_delay_s=1.5,
                max_attempts=6,
            )
            if realised_gbp is not None:
                pnl_gb = float(realised_gbp)
            if hist_px is not None and hist_px > 0:
                exit_px = float(hist_px)
        except Exception as exc:
            _log.warning("exit broker P&L lookup failed order=%s: %s", close_oid, exc)
    db.execute(
        """UPDATE trades SET exit_price=?, exit_ts=?, exit_reason=?, status='CLOSED',
                              pnl_pct=ROUND((?-entry_price)/NULLIF(entry_price,0)*100, 4),
                              pnl_gbp=?,
                              t212_close_order_id=?
           WHERE id=?""",
        (exit_px, exit_ts_wall, reason, exit_px, round(float(pnl_gb), 4), close_oid, trade_id),
    )
    await mgr.close(slot, exit_price=exit_px, reason=reason)
    _trade_audit_after_close(
        trade_id=trade_id,
        exit_ts=exit_ts_wall,
        reason=reason,
        close_order_id=close_oid or None,
        slot=slot,
        audit_extra=audit_extra,
    )
    _log.info(
        "EXIT slot=%d ticker=%s price=%.4f reason=%s pnl_gbp=%.2f close_oid=%s open_oid=%s",
        slot.index,
        ticker,
        price,
        reason,
        pnl_gb,
        close_oid,
        open_oid,
    )


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
