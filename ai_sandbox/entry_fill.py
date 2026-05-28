"""Poll Trading 212 for entry fills — same semantics as trading_ai/order_flow."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import config, t212_ai

_log = logging.getLogger("ai_sandbox.entry_fill")


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


async def _check_history_for_fill(order_id: Any, t212_ticker: str) -> tuple[float, float | None] | None:
    want_id = str(order_id).strip()
    try:
        async for page in t212_ai.iter_order_history_pages(t212_ticker, limit=50, max_pages=12):
            for item in page:
                ord_data = item.get("order") or {}
                fill_data = item.get("fill") or {}
                oid = ord_data.get("id")
                st = str(ord_data.get("status") or "").strip().upper()
                if str(oid).strip() != want_id or st != "FILLED":
                    continue
                qty = float(ord_data.get("filledQuantity") or ord_data.get("quantity") or 0)
                price = to_float(fill_data.get("price"))
                if qty > 0:
                    _log.info("AI order %s in history FILLED qty=%.4f @ ~%s", order_id, qty, price)
                    return qty, price
    except Exception as exc:
        _log.warning("AI history probe for order %s failed: %s", order_id, exc)
    return None


async def _check_position_for_fill(
    t212_ticker: str, requested_qty: float, threshold: float
) -> tuple[float, float | None] | None:
    try:
        positions = await t212_ai.get_positions(bypass_cache=False)
        for pos in positions:
            if pos.get("ticker") == t212_ticker:
                qty = float(pos.get("quantity") or 0)
                avg_price = to_float(pos.get("averagePrice"))
                if qty > 0 and (requested_qty <= 0 or (qty / requested_qty) >= threshold):
                    _log.info("AI fill confirmed via positions: %s qty=%.4f @ ~%s", t212_ticker, qty, avg_price)
                    return qty, avg_price
    except Exception as exc:
        _log.warning("AI position check for %s failed: %s", t212_ticker, exc)
    return None


async def wait_market_fill(
    t212_ticker: str, requested_qty: float, *, timeout_sec: float | None = None
) -> tuple[float | None, float | None]:
    """Confirm a market-entry fill via the positions snapshot (best-effort average price)."""
    timeout = float(timeout_sec if timeout_sec is not None else config.FILL_WAIT_TIMEOUT_SECONDS)
    deadline = time.time() + timeout
    await asyncio.sleep(1.2)
    threshold = float(config.FILL_PARTIAL_THRESHOLD)
    requested = float(requested_qty)
    while time.time() < deadline:
        pos_hit = await _check_position_for_fill(t212_ticker, requested, threshold)
        if pos_hit is not None:
            q, avg = pos_hit
            return q, avg
        await asyncio.sleep(2.0)
    return None, None


async def wait_limit_fill(
    order_id: str | int,
    t212_ticker: str,
    *,
    requested_qty: float,
    timeout_sec: float | None = None,
    partial_threshold: float | None = None,
    cancel_if_unfilled: bool = True,
) -> tuple[float | None, str, float | None]:
    """Return (filled_qty, status_or_reason, avg_price). Mirrors production wait_for_fill."""

    timeout = float(timeout_sec if timeout_sec is not None else config.FILL_WAIT_TIMEOUT_SECONDS)
    threshold = float(partial_threshold if partial_threshold is not None else config.FILL_PARTIAL_THRESHOLD)
    deadline = time.time() + timeout
    consec_404 = 0
    requested = float(requested_qty)
    last_status = ""

    while time.time() < deadline:
        try:
            order = await t212_ai.get_order(order_id)
            consec_404 = 0
        except t212_ai.T212AIError as exc:
            if exc.status == 404:
                consec_404 += 1
                if consec_404 >= 2 and t212_ticker:
                    hist = await _check_history_for_fill(order_id, t212_ticker)
                    if hist is not None:
                        return hist[0], "FILLED", hist[1]
                    pos_hit = await _check_position_for_fill(t212_ticker, requested, threshold)
                    if pos_hit is not None:
                        return pos_hit[0], "FILLED_VIA_POSITION", pos_hit[1]
            _log.warning("AI poll order %s failed: %s", order_id, exc)
            await asyncio.sleep(2)
            continue

        last_status = str(order.get("status") or "")
        filled = float(order.get("filledQuantity") or 0)
        order_qty = float(order.get("quantity") or requested)
        avg_v = order.get("filledValue")
        avg_price: float | None = (float(avg_v) / filled) if (avg_v and filled > 0) else None
        if avg_price is None:
            avg_price = to_float(order.get("averagePrice")) or to_float(order.get("filledPrice"))

        if last_status == "FILLED":
            return filled, last_status, avg_price
        if last_status == "PARTIALLY_FILLED" and order_qty > 0:
            if (filled / order_qty) >= threshold:
                _log.info("AI partial fill accepted limit %s %s/%s", order_id, filled, order_qty)
                return filled, last_status, avg_price

        if last_status in ("REJECTED", "CANCELLED", "CANCELED", "EXPIRED"):
            return None, last_status, None

        await asyncio.sleep(2)

    if cancel_if_unfilled:
        ok = await t212_ai.cancel_pending_open_order(order_id)
        _log.warning("AI entry limit %s unfilled (%s); cancel_attempt=%s", order_id, last_status or "TIMEOUT", ok)
    return None, last_status or "TIMEOUT", None
