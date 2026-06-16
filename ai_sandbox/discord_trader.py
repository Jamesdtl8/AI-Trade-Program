"""Discord Trader — copies trades from James' trader-tracker Discord channel.

Uses the SAME scanner_feed.jsonl that the AI scanner already tails.
No second Discord bot needed — just filters messages by channel_id.

Add the trader-tracker channel ID to DISCORD_TRADER_CHANNEL_ID and the
system will pick up messages automatically from the existing relay.

Message patterns handled
────────────────────────
1. Break confirmed  (🟢 TICKER — Break confirmed · 1-min candle closed at $X … Placing buy.)
   → Market buy immediately (James' bot already confirmed the break on candle close).

2. Order placed  (🟢 TICKER — Buy/Break-entry order placed · Limit buy N shares at $X)
   → Limit buy in regular hours at James' limit price; market buy extended hours.
   → Resting TP limit at entry × (1 + DISCORD_TRADER_TP_PCT) once filled.
   → Cancel/re-arm TP limit when live P&L crosses DISCORD_TRADER_TP_ARM_PCT (default 5%).

3. Stop-loss breach  (🔴 TICKER — Stop loss breached on candle close)
   → Market sell immediately, no candle-close wait.

Exit rules
──────────
• Take-profit: resting limit sell in regular hours; tick market sell in extended hours.
• TP limit cancelled when unrealised P&L < DISCORD_TRADER_TP_ARM_PCT; re-armed when back above.
• Emergency stop: market sell on any 1s tick when unrealised P&L ≤ DISCORD_TRADER_EMERGENCY_STOP_PCT (default -20%).
• Stop breach message from James' bot → market sell immediately.
• Extended hours (UK pre 09:00–14:30, post 21:00–01:00 ≈ US pre/post): market buy at fill;
  take-profit % is adjusted from James' signal limit so the TP price matches limit × (1+TP%)
  despite slippage; floor DISCORD_TRADER_SLIPPAGE_TP_FLOOR_PCT (default 1.5%) if slippage
  would make that a loss. Emergency stop always vs actual fill price.
• Regular US hours: limit buy at James' limit; limit orders use GOOD_TILL_CANCEL.

Configuration (env vars)
────────────────────────
DISCORD_TRADER_ENABLED=1
DISCORD_TRADER_CHANNEL_ID=<channel snowflake from James' server>
T212_ENV_DISCORD=live|demo
TRADING_212_KEY_DISCORD=<api key for second T212 account>
DISCORD_TRADER_STAKE_GBP=10000
DISCORD_TRADER_TP_PCT=8
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

_log = logging.getLogger("discord_trader")

# ── config ─────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def enabled() -> bool:
    return _env("DISCORD_TRADER_ENABLED", "0").lower() in ("1", "true", "yes")


def channel_id() -> int | None:
    raw = _env("DISCORD_TRADER_CHANNEL_ID", "")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def stake_gbp() -> float:
    try:
        return max(1.0, float(_env("DISCORD_TRADER_STAKE_GBP", "10000")))
    except ValueError:
        return 10_000.0


def tp_pct() -> float:
    try:
        return max(0.1, float(_env("DISCORD_TRADER_TP_PCT", "8")))
    except ValueError:
        return 8.0


def tp_arm_pct() -> float:
    """Cancel resting TP limit when live unrealised P&L falls below this (default 5%)."""
    try:
        return float(_env("DISCORD_TRADER_TP_ARM_PCT", "5"))
    except ValueError:
        return 5.0


def emergency_stop_pct() -> float:
    """Market sell immediately when live unrealised P&L hits this (default -20%)."""
    try:
        return float(_env("DISCORD_TRADER_EMERGENCY_STOP_PCT", "-20"))
    except ValueError:
        return -20.0


def slippage_tp_floor_pct() -> float:
    """Minimum TP % from fill when slippage would exceed James' intended TP level."""
    try:
        return max(0.1, float(_env("DISCORD_TRADER_SLIPPAGE_TP_FLOOR_PCT", "1.5")))
    except ValueError:
        return 1.5


def effective_tp_pct(
    entry_price: float,
    signal_limit: float | None,
    base_tp: float | None = None,
) -> float:
    """TP % from actual fill so target price ≈ James' limit × (1 + base_tp%)."""
    base = base_tp if base_tp is not None else tp_pct()
    floor = slippage_tp_floor_pct()
    if entry_price <= 0:
        return base
    if not signal_limit or signal_limit <= 0:
        return base
    james_tp_price = signal_limit * (1 + base / 100)
    if entry_price >= james_tp_price:
        return floor
    eff = (james_tp_price / entry_price - 1) * 100
    return max(floor, eff)


def _trade_tp_pct(trade: dict | None) -> float:
    if trade and trade.get("tp_pct") is not None:
        return float(trade["tp_pct"])
    return tp_pct()


def _is_regular_hours() -> bool:
    from . import config
    return config.market_phase() == "regular"


def _t212_env() -> str:
    return _env("T212_ENV_DISCORD", "live")


def _t212_key() -> str:
    return _env("TRADING_212_KEY_DISCORD")


# ── message parser — James' trader-tracker formatted messages ──────────────────
#
# James' trading bot posts structured audit messages to trader-tracker channel
# (1505170365910093956). We mirror the AI decisions:
#
#   🟢 TICKER — Break-entry / Buy order placed · Limit buy N shares at $X
#   → limit buy in regular hours; market buy extended hours
#
#   🟢 TICKER — Break confirmed · (informational — wait for order placed)
#
#   🔴 TICKER — Stop loss breached on candle close · HH:MM:SS
#   → sell immediately (if we hold that ticker)
#
#   🧠 TICKER — AI decision: Exit (market sell) · HH:MM:SS
#   → market sell immediately (mirror James' exit)

# Discord bot posts use **bold** markdown: "🧠 **MU** — AI decision: ..."
# Ticker pattern handles both plain "MU" and bold "**MU**"
_TICKER_PAT = r"\*{0,2}(?P<ticker>[A-Z]{1,8})\*{0,2}"

# 🧠 TICKER — [AI TRADE · ] AI decision: Place limit buy
_RE_LIMIT_BUY = re.compile(
    r"🧠\s+" + _TICKER_PAT + r"\s+[—–-].*?AI decision:\s*Place limit buy",
    re.IGNORECASE | re.DOTALL,
)
# Extract signal price from "signal $X" or "Entry $X"
_RE_SIGNAL_PRICE = re.compile(
    r"(?:signal|Entry)\s+\$(?P<price>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# 🟢 TICKER — Break confirmed (candle closed above break — place buy)
_RE_BREAK_CONFIRMED = re.compile(
    r"🟢\s+" + _TICKER_PAT + r"\s+[—–-]\s*Break confirmed",
    re.IGNORECASE,
)
_RE_BREAK_CLOSE = re.compile(
    r"closed at \$(?P<price>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_BREAK_LEVEL = re.compile(
    r"break level \$(?P<level>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# 🟢 TICKER — Break-entry / Buy order placed (execute at James' limit price)
_RE_ENTRY_PLACED = re.compile(
    r"🟢\s+" + _TICKER_PAT + r"\s+[—–-]\s*(?:Break-entry order placed|Buy order placed)",
    re.IGNORECASE,
)
_RE_LIMIT_AT_PRICE = re.compile(
    r"Limit buy\s+[\d,]+\s+shares\s+at\s+\$(?P<price>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_LIMIT_ARROW_PRICE = re.compile(
    r"limit\s+\$(?P<price>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# 🔴 TICKER — Stop loss breached on candle close
_RE_STOP_BREACH = re.compile(
    r"🔴\s+" + _TICKER_PAT + r"\s+[—–-]\s*Stop loss breached",
    re.IGNORECASE,
)

# 🧠 TICKER — AI decision: Exit (market sell)
_RE_MARKET_EXIT = re.compile(
    r"🧠\s+" + _TICKER_PAT + r"\s+[—–-].*?AI decision:\s*Exit(?:\s*\(market sell\))?",
    re.IGNORECASE | re.DOTALL,
)

# 🔴 TICKER — `EXIT` · [EXIT] Market exit — order submitted
_RE_EXIT_SUBMITTED = re.compile(
    r"🔴\s+" + _TICKER_PAT + r"\s+[—–-].*?\bEXIT\b",
    re.IGNORECASE | re.DOTALL,
)


def _parse_price(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


def parse_message(content: str) -> dict[str, Any] | None:
    """Parse trader-tracker bot messages. Returns action dict or None if not actionable."""
    text = content.strip()
    if not text:
        return None

    # 🟢 Order placed with limit price → our entry (primary buy trigger)
    m = _RE_ENTRY_PLACED.search(text)
    if m:
        ticker = m.group("ticker").upper()
        price_m = _RE_LIMIT_AT_PRICE.search(text) or _RE_LIMIT_ARROW_PRICE.search(text)
        try:
            limit_price = _parse_price(price_m.group("price")) if price_m else 0.0
        except (ValueError, AttributeError):
            limit_price = 0.0
        if limit_price > 0:
            et = "break_confirmed" if "break-entry" in text.lower() else "limit_buy"
            return {
                "type": "entry_limit_placed",
                "ticker": ticker,
                "limit_price": limit_price,
                "entry_type": et,
            }

    # 🧠 Place limit buy → informational (execution on order placed message)
    m = _RE_LIMIT_BUY.search(text)
    if m:
        ticker = m.group("ticker").upper()
        price_m = _RE_SIGNAL_PRICE.search(text)
        try:
            price = _parse_price(price_m.group("price")) if price_m else 0.0
        except (ValueError, AttributeError):
            price = 0.0
        return {"type": "limit_buy", "ticker": ticker, "signal_price": price}

    # 🟢 Break confirmed → informational (execution on order placed message)
    m = _RE_BREAK_CONFIRMED.search(text)
    if m:
        ticker = m.group("ticker").upper()
        close_m = _RE_BREAK_CLOSE.search(text)
        level_m = _RE_BREAK_LEVEL.search(text)
        try:
            close_price = _parse_price(close_m.group("price")) if close_m else 0.0
        except (ValueError, AttributeError):
            close_price = 0.0
        try:
            break_level = _parse_price(level_m.group("level")) if level_m else 0.0
        except (ValueError, AttributeError):
            break_level = 0.0
        hint = close_price or break_level
        if hint > 0:
            return {
                "type": "break_confirmed",
                "ticker": ticker,
                "close_price": close_price,
                "break_level": break_level,
                "signal_price": hint,
            }

    # 🔴 Stop loss breached → sell immediately
    m = _RE_STOP_BREACH.search(text)
    if m:
        return {"type": "stop_breach", "ticker": m.group("ticker").upper()}

    # 🧠 Exit (market sell) → sell immediately
    m = _RE_MARKET_EXIT.search(text)
    if m:
        return {"type": "market_exit", "ticker": m.group("ticker").upper()}

    # 🔴 EXIT submitted (backup — James' bot audit trail)
    m = _RE_EXIT_SUBMITTED.search(text)
    if m:
        return {"type": "market_exit", "ticker": m.group("ticker").upper()}

    return None


# ── T212 (Discord account) — see t212_discord.py ─────────────────────────────

from . import t212_discord as _dt212


# ── trade executor ─────────────────────────────────────────────────────────────

class DiscordTrader:
    def __init__(self) -> None:
        self._instrument_cache: dict[str, str] = {}
        self._monitors: dict[int, asyncio.Task] = {}

    async def _resolve(self, ticker: str) -> str | None:
        if ticker not in self._instrument_cache:
            inst = await _dt212.resolve_ticker(ticker)
            if inst:
                self._instrument_cache[ticker] = inst
        return self._instrument_cache.get(ticker)

    async def _canonical_ticker(self, ticker: str) -> str:
        """T212 shortName — what Discord and the broker UI show."""
        inst = await self._resolve(ticker)
        if inst:
            return _dt212.scanner_ticker_from_inst(inst)
        return _dt212.normalize_ticker(ticker)

    def _trade_matches(self, trade: dict, ticker: str, inst: str | None) -> bool:
        t_sym = str(trade.get("ticker") or "").upper()
        if t_sym == ticker.upper():
            return True
        if inst:
            t_inst = _dt212.instrument_for_ticker(t_sym)
            if t_inst and t_inst.upper() == inst.upper():
                return True
        return False

    async def _position_row(self, inst: str) -> dict | None:
        for p in await _dt212.get_positions(bypass_cache=False):
            if str(p.get("ticker") or "").upper() == inst.upper():
                return p
        return None

    async def _buy_quantity(
        self,
        ticker: str,
        inst: str,
        price_hint: float | None = None,
        *,
        use_hint: bool = False,
        limit_price: float | None = None,
    ) -> float:
        """Convert stake GBP → share quantity (use limit_price when placing limits)."""
        import math
        from . import config, price_data

        live = price_data.last_price(ticker)
        live_f = float(live) if live else 0.0
        if limit_price and float(limit_price) > 0:
            price = float(limit_price)
        else:
            price = live_f
            if use_hint and price_hint and float(price_hint) > 0:
                hint = float(price_hint)
                if live_f > 0 and 0.5 * live_f <= hint <= 2.0 * live_f:
                    price = hint
            if price <= 0 and live_f > 0:
                price = live_f
        if price <= 0:
            raise ValueError(f"no live price available for {ticker}")

        cash = _dt212.cash_snapshot() or {}
        free_gbp = float(cash.get("free") or 0)
        budget_gbp = min(stake_gbp(), free_gbp * 0.98)
        if budget_gbp <= 0:
            raise ValueError(f"no free cash on Discord T212 account (free=£{free_gbp:.2f})")

        capital_usd = budget_gbp * config.GBP_USD_RATE
        qty = math.floor(capital_usd / price)

        pos = await self._position_row(inst)
        held = float(pos.get("quantity") or 0) if pos else 0.0
        await _dt212._ensure_instruments()
        meta = _dt212._INST_BY_ROOT.get(ticker.upper()) or {}
        max_open = meta.get("maxOpenQuantity")
        if max_open is not None:
            room = math.floor(float(max_open) - held)
            qty = min(qty, max(0, room))

        if qty <= 0:
            raise ValueError(f"quantity 0 for {ticker} at ${price:.4f}")
        _log.info(
            "dt sizing %s: budget=£%.0f price=$%.4f → qty=%.0f (live=$%.4f)",
            ticker, budget_gbp, price, qty, live_f,
        )
        return float(qty)

    def _finalize_entry_tp(
        self, tid: int, entry_price: float, signal_limit: float | None
    ) -> float:
        from . import db

        eff = effective_tp_pct(entry_price, signal_limit)
        db.dt_set_trade_tp_pct(tid, eff)
        if signal_limit and signal_limit > 0 and entry_price > 0:
            slip = (entry_price - signal_limit) / signal_limit * 100
            target = entry_price * (1 + eff / 100)
            if abs(slip) > 0.05 or abs(eff - tp_pct()) > 0.05:
                _log.info(
                    "dt_trade #%s slippage-adjusted TP: fill=$%.4f limit=$%.4f "
                    "slip=%+.2f%% → TP %.2f%% @ $%.4f",
                    tid, entry_price, signal_limit, slip, eff, target,
                )
        return eff

    # ── entry handlers ───────────────────────────────────────────────────────

    async def handle_entry_limit_placed(
        self,
        ticker: str,
        msg_id: str | None,
        limit_price: float,
        entry_type: str = "limit_buy",
    ) -> None:
        if _is_regular_hours():
            await self.open_limit_buy(
                ticker, limit_price=limit_price, entry_type=entry_type, msg_id=msg_id,
            )
        else:
            await self.open_market_buy(
                ticker, entry_type=entry_type, msg_id=msg_id, price_hint=limit_price,
            )

    # ── stop breach ──────────────────────────────────────────────────────────

    async def handle_stop_breach(self, ticker: str) -> None:
        await self.handle_market_exit(ticker, reason="stop_breach")

    async def handle_market_exit(self, ticker: str, *, reason: str = "market_exit") -> None:
        """Mirror James' market exit — sell OPEN position or flatten broker holding."""
        from . import db

        inst = await self._resolve(ticker)
        ticker = await self._canonical_ticker(ticker)

        exited = False
        for trade in db.dt_open_trades():
            if not self._trade_matches(trade, ticker, inst):
                continue
            self._monitors.pop(int(trade["id"]), None)
            await self._cancel_tp_limit(trade)
            await self._exit_trade(trade, reason=reason, inst=inst)
            exited = True

        if exited:
            _sse_push("trade_update", {"action": "exit", "ticker": ticker, "reason": reason})
            return

        if not inst:
            _log.warning("market_exit %s — instrument not found", ticker)
            return

        pos = await self._position_row(inst)
        qty = float(pos.get("quantity") or 0) if pos else 0.0
        if qty <= 0:
            _log.info("market_exit %s — no open position on Discord account", ticker)
            return

        _log.info("market_exit %s broker-only qty=%.4f reason=%s", ticker, qty, reason)
        result = await _dt212.place_market(inst, -abs(qty))
        if result.get("http_status", 0) not in (200, 201):
            _log.warning("market_exit %s failed: %s", ticker, result.get("body"))
            return
        body = result.get("body") or {}
        order_id = str(body.get("id") or body.get("orderId") or "")
        tid = db.dt_adopt_broker_trade(
            ticker,
            float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0),
            qty,
            entry_type="manual",
            stake_gbp=stake_gbp(),
            tp_pct=tp_pct(),
        )
        db.dt_sell_pending_trade(tid, reason, order_id or None)
        if order_id:
            asyncio.create_task(
                self._confirm_close_from_history(
                    tid,
                    ticker,
                    inst,
                    order_id,
                    float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0),
                ),
                name=f"dt_close_{tid}",
            )
        _sse_push("trade_update", {"action": "exit", "ticker": ticker, "reason": reason})

    # ── core open / exit ─────────────────────────────────────────────────────

    async def open_limit_buy(
        self,
        ticker: str,
        *,
        limit_price: float,
        entry_type: str,
        msg_id: str | None = None,
    ) -> int | None:
        from . import db

        inst = await self._resolve(ticker)
        ticker = await self._canonical_ticker(ticker)
        if not inst:
            _log.warning("open_limit_buy %s — not found on Discord T212 account", ticker)
            tid = db.dt_open_trade(ticker, entry_type, stake_gbp(), tp_pct(), None, msg_id)
            db.dt_reject_trade(tid, "instrument_not_found")
            return None

        lp = float(limit_price)
        tid = db.dt_open_trade(
            ticker, entry_type, stake_gbp(), tp_pct(), None, msg_id,
            signal_limit_price=lp,
        )
        try:
            qty = await self._buy_quantity(ticker, inst, limit_price=lp)
        except Exception as e:
            db.dt_reject_trade(tid, str(e))
            _log.warning("open_limit_buy %s sizing failed: %s", ticker, e)
            return None

        _log.info(
            "Limit buy %s %s qty=%.0f limit=$%.4f stake=£%.0f trade_id=%s",
            ticker, inst, qty, lp, stake_gbp(), tid,
        )
        try:
            result = await _dt212.place_limit(inst, qty, lp)
            order_id = ""
            http_ok = result.get("http_status", 0) in (200, 201)
            if http_ok:
                body = result.get("body") or {}
                order_id = str(body.get("id") or body.get("orderId") or "")
            else:
                err = str(result.get("body", result))
                _log.warning("limit_buy %s rejected: %s", ticker, err)
                db.dt_reject_trade(tid, err)
                return None

            _log.info("limit_buy %s order_id=%s http=%s", ticker, order_id or "?", result.get("http_status"))
            if order_id:
                db.execute(
                    "UPDATE dt_trades SET t212_open_order_id=?, updated_ts=? WHERE id=?",
                    (order_id, time.time(), tid),
                )

            fill = None
            if order_id:
                fill = await _dt212.wait_limit_fill(
                    order_id, inst, requested_qty=qty, timeout_sec=120.0,
                )
            if fill is None:
                fill = await _dt212.wait_entry_fill(
                    inst, qty, order_id=order_id or None, timeout_sec=30.0,
                )

            if fill is None:
                _log.warning("limit_buy %s order=%s — awaiting broker reconcile", ticker, order_id or "?")
                _sse_push("trade_update", {"action": "pending_fill", "ticker": ticker, "trade_id": tid})
                return tid

            filled_qty, entry_price = fill
            if entry_price <= 0:
                pos = await self._position_row(inst)
                if pos:
                    entry_price = float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0)
            db.dt_fill_trade(tid, entry_price, filled_qty, order_id or None)
            self._finalize_entry_tp(tid, entry_price, lp)
            _log.info("dt_trade #%s %s FILLED qty=%.4f entry=$%.4f", tid, ticker, filled_qty, entry_price)

            trade = db.dt_trade_by_id(tid)
            if trade:
                await self._arm_tp_limit(tid, ticker, inst, entry_price, filled_qty, trade)
            self._start_monitor(tid, ticker, inst)
            _sse_push("trade_update", {"action": "filled", "ticker": ticker, "trade_id": tid})
            return tid
        except Exception:
            _log.exception("open_limit_buy %s trade_id=%s failed", ticker, tid)
            db.dt_reject_trade(tid, "order error — see server log")
            return None

    async def open_market_buy(
        self,
        ticker: str,
        *,
        entry_type: str,
        stop_price: float | None = None,
        msg_id: str | None = None,
        price_hint: float | None = None,
    ) -> int | None:
        from . import db
        inst = await self._resolve(ticker)
        ticker = await self._canonical_ticker(ticker)
        if not inst:
            _log.warning("open_market_buy %s — not found on Discord T212 account", ticker)
            tid = db.dt_open_trade(ticker, entry_type, stake_gbp(), tp_pct(), stop_price, msg_id)
            db.dt_reject_trade(tid, "instrument_not_found")
            return None

        signal_limit = float(price_hint) if price_hint and float(price_hint) > 0 else None
        tid = db.dt_open_trade(
            ticker, entry_type, stake_gbp(), tp_pct(), stop_price, msg_id,
            signal_limit_price=signal_limit,
        )

        try:
            qty = await self._buy_quantity(
                ticker, inst, price_hint,
                limit_price=signal_limit,
            )
        except Exception as e:
            db.dt_reject_trade(tid, str(e))
            _log.warning("open_market_buy %s sizing failed: %s", ticker, e)
            return None

        _log.info("Market buy %s %s qty=%.0f stake=£%.0f trade_id=%s", ticker, inst, qty, stake_gbp(), tid)

        try:
            result = await _dt212.place_market(inst, qty)
            order_id = ""
            http_ok = result.get("http_status", 0) in (200, 201)
            if http_ok:
                body = result.get("body") or {}
                order_id = str(body.get("id") or body.get("orderId") or "")
            else:
                err = str(result.get("body", result))
                _log.warning("market_buy %s rejected: %s", ticker, err)
                db.dt_reject_trade(tid, err)
                return None

            _log.info("market_buy %s order_id=%s http=%s", ticker, order_id or "?", result.get("http_status"))

            fill = await _dt212.wait_entry_fill(
                inst, qty, order_id=order_id or None, timeout_sec=90.0,
            )

            if fill is None:
                _log.warning("market_buy %s order=%s — awaiting broker reconcile", ticker, order_id or "?")
                if order_id:
                    db.execute(
                        "UPDATE dt_trades SET t212_open_order_id=?, updated_ts=? WHERE id=?",
                        (order_id, time.time(), tid),
                    )
                _sse_push("trade_update", {"action": "pending_fill", "ticker": ticker, "trade_id": tid})
                return tid

            filled_qty, entry_price = fill
            if entry_price <= 0:
                pos = await self._position_row(inst)
                if pos:
                    entry_price = float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0)
            db.dt_fill_trade(tid, entry_price, filled_qty, order_id or None)
            self._finalize_entry_tp(tid, entry_price, signal_limit)
            _log.info("dt_trade #%s %s FILLED qty=%.4f entry=$%.4f", tid, ticker, filled_qty, entry_price)

            trade = db.dt_trade_by_id(tid)
            if trade and _is_regular_hours():
                await self._arm_tp_limit(tid, ticker, inst, entry_price, filled_qty, trade)
            self._start_monitor(tid, ticker, inst)
            _sse_push("trade_update", {"action": "filled", "ticker": ticker, "trade_id": tid})
            return tid
        except Exception:
            _log.exception("open_market_buy %s trade_id=%s failed", ticker, tid)
            db.dt_reject_trade(tid, "order error — see server log")
            return None

    def _start_monitor(self, tid: int, ticker: str, inst: str) -> None:
        if tid in self._monitors and not self._monitors[tid].done():
            return
        task = asyncio.create_task(
            self._profit_monitor(tid, ticker, inst),
            name=f"dt_mon_{tid}",
        )
        self._monitors[tid] = task

    async def _cancel_tp_limit(self, trade: dict) -> None:
        from . import db

        tid = int(trade["id"])
        tp_oid = str(trade.get("t212_tp_order_id") or "").strip()
        if not tp_oid:
            return
        await _dt212.cancel_order(tp_oid)
        db.dt_clear_tp_order(tid)
        _log.info("dt_trade #%s %s TP limit cancelled oid=%s", tid, trade.get("ticker"), tp_oid)

    async def _arm_tp_limit(
        self,
        tid: int,
        ticker: str,
        inst: str,
        entry_price: float,
        qty: float,
        trade: dict | None = None,
    ) -> bool:
        from . import db
        from . import t212_ai

        if not _is_regular_hours():
            return False
        trade = trade or db.dt_trade_by_id(tid)
        if not trade or str(trade.get("t212_tp_order_id") or "").strip():
            return False
        if entry_price <= 0 or qty <= 0:
            return False
        trade_tp = _trade_tp_pct(trade)
        tp_price = t212_ai._round(entry_price * (1 + trade_tp / 100))
        result = await _dt212.place_limit(inst, -abs(qty), tp_price)
        if result.get("http_status", 0) not in (200, 201):
            _log.warning("tp_limit %s failed: %s", ticker, result.get("body"))
            return False
        body = result.get("body") or {}
        oid = str(body.get("id") or body.get("orderId") or "")
        db.dt_set_tp_order(tid, oid or None, tp_price)
        _log.info(
            "dt_trade #%s %s TP limit @ $%.4f (+%.2f%%) oid=%s",
            tid, ticker, tp_price, trade_tp, oid or "?",
        )
        return True

    async def _profit_monitor(self, tid: int, ticker: str, inst: str) -> None:
        from . import db
        await asyncio.sleep(3)
        entry_price: float | None = None
        arm_pct = tp_arm_pct()

        while True:
            try:
                trade = db.dt_trade_by_id(tid)
                if not trade or trade["status"] != "OPEN":
                    break

                pos = await self._position_row(inst)
                qty = float(trade.get("quantity") or 0)
                if pos:
                    qty = float(pos.get("quantity") or qty)
                    ep = float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0)
                    if ep > 0:
                        entry_price = ep
                        if not trade.get("entry_price"):
                            db.dt_fill_trade(tid, entry_price, qty, trade.get("t212_open_order_id"))
                elif qty > 0:
                    tp_oid = str(trade.get("t212_tp_order_id") or "").strip()
                    close_oid = tp_oid or str(trade.get("t212_close_order_id") or "").strip()
                    ep = float(trade.get("entry_price") or entry_price or 0)
                    if close_oid and ep > 0:
                        db.dt_sell_pending_trade(tid, "tp_hit", close_oid)
                        await self._confirm_close_from_history(
                            tid, ticker, inst, close_oid, ep,
                        )
                    self._monitors.pop(tid, None)
                    return

                if not pos or qty <= 0:
                    await asyncio.sleep(1)
                    continue

                price = float(pos.get("currentPrice") or 0)
                if entry_price is None or entry_price <= 0:
                    entry_price = float(trade.get("entry_price") or 0)

                wm = _dt212.wallet_map().get(inst.upper()) or {}
                unreal_pct = wm.get("unreal_pct")
                if unreal_pct is not None:
                    unreal_pct = float(unreal_pct)

                live_pct = unreal_pct
                if live_pct is None and entry_price > 0 and price > 0:
                    live_pct = (price - entry_price) / entry_price * 100

                emergency = emergency_stop_pct()
                if live_pct is not None and live_pct <= emergency:
                    _log.warning(
                        "dt_trade #%s %s EMERGENCY STOP %.2f%% (limit %.1f%%) — market sell",
                        tid, ticker, live_pct, emergency,
                    )
                    await self._exit_trade(
                        trade, reason="emergency_stop", qty=qty, inst=inst,
                    )
                    return

                tp_oid = str(trade.get("t212_tp_order_id") or "").strip()

                if _is_regular_hours():
                    if tp_oid:
                        order = await _dt212.get_order(tp_oid)
                        st = str(order.get("status") or "").upper()
                        if st == "FILLED":
                            db.dt_sell_pending_trade(tid, "tp_hit", tp_oid)
                            await self._confirm_close_from_history(
                                tid, ticker, inst, tp_oid, entry_price or 0,
                            )
                            self._monitors.pop(tid, None)
                            return
                        if unreal_pct is not None and unreal_pct < arm_pct:
                            await self._cancel_tp_limit(trade)
                            trade = db.dt_trade_by_id(tid) or trade
                    elif (
                        entry_price > 0
                        and unreal_pct is not None
                        and unreal_pct >= arm_pct
                    ):
                        trade = db.dt_trade_by_id(tid) or trade
                        await self._arm_tp_limit(
                            tid, ticker, inst, entry_price, qty, trade,
                        )
                elif entry_price > 0 and price > 0:
                    trade_tp = _trade_tp_pct(trade)
                    gain = (price - entry_price) / entry_price * 100
                    if gain >= trade_tp:
                        _log.info("dt_trade #%s %s TP %.2f%% (target %.2f%%) — market exit", tid, ticker, gain, trade_tp)
                        await self._exit_trade(trade, reason="tp_hit", qty=qty, inst=inst)
                        return

            except Exception:
                _log.exception("profit_monitor error #%s %s", tid, ticker)

            await asyncio.sleep(1)

    async def _exit_trade(
        self, trade: dict, *, reason: str, qty: float | None = None, inst: str | None = None
    ) -> None:
        from . import db
        tid = trade["id"]
        ticker = trade["ticker"]

        await self._cancel_tp_limit(trade)

        if inst is None:
            inst = await self._resolve(ticker)
        if inst is None:
            _log.warning("_exit_trade #%s %s — cannot resolve instrument", tid, ticker)
            return

        if not qty:
            qty = float(trade.get("quantity") or 0)
        if not qty:
            pos = await self._position_row(inst)
            qty = float(pos.get("quantity") or 0) if pos else 0
        if not qty:
            db.dt_close_trade(tid, 0.0, reason, 0.0, 0.0)
            return

        result = await _dt212.place_market(inst, -abs(qty))
        if result.get("http_status", 0) not in (200, 201):
            _log.warning("market_sell %s failed: %s", ticker, result.get("body"))
            return

        body = result.get("body") or {}
        order_id = str(body.get("id") or body.get("orderId") or "")
        pos = await self._position_row(inst)
        exit_price = float(pos.get("currentPrice") or 0) if pos else 0.0
        entry_price = float(trade.get("entry_price") or 0)
        wm = _dt212.wallet_map().get(inst.upper()) or {}
        if wm.get("unreal_gbp") is not None and wm.get("unreal_pct") is not None:
            pnl_gbp = float(wm["unreal_gbp"])
            pnl_pct = float(wm["unreal_pct"])
        elif exit_price > 0 and entry_price > 0:
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            pnl_gbp = (exit_price - entry_price) / entry_price * float(trade.get("stake_gbp") or 0)
        else:
            pnl_pct = pnl_gbp = 0.0

        db.dt_sell_pending_trade(tid, reason, order_id or None)
        db.execute(
            """UPDATE dt_trades SET exit_price=?, pnl_pct=?, pnl_gbp=?, updated_ts=?
               WHERE id=?""",
            (exit_price, pnl_pct, pnl_gbp, time.time(), tid),
        )
        self._monitors.pop(tid, None)
        _log.info("dt_trade #%s %s SELL_PENDING %s exit≈$%.4f", tid, ticker, reason, exit_price)
        _sse_push("trade_update", {"action": "sell_pending", "ticker": ticker, "trade_id": tid})

        if order_id:
            asyncio.create_task(
                self._confirm_close_from_history(tid, ticker, inst, order_id, entry_price),
                name=f"dt_close_{tid}",
            )

    async def _confirm_close_from_history(
        self,
        tid: int,
        ticker: str,
        inst: str,
        order_id: str,
        entry_price: float,
    ) -> None:
        from . import db

        realised = await _dt212.backfill_closed_trade_pnl(
            tid,
            instrument_ticker=inst,
            close_order_id=order_id,
            entry_price=entry_price,
        )
        if realised is None:
            return
        db.dt_confirm_close_trade(tid)
        _log.info("dt_trade #%s %s CLOSED broker confirmed pnl=£%.2f", tid, ticker, realised)
        _sse_push("trade_update", {"action": "closed", "ticker": ticker, "trade_id": tid})

    async def close(self) -> None:
        for t in list(self._monitors.values()):
            t.cancel()


async def _reconcile_broker_positions() -> None:
    """Keep dt_trades in sync with Discord T212 positions (broker source of truth)."""
    from . import db

    while True:
        try:
            await _dt212._ensure_instruments()
            rows = await _dt212.get_positions(bypass_cache=False)
            live_trades = db.dt_live_trades()
            for t in live_trades:
                st = str(t.get("status") or "").upper()
                if st not in ("OPEN", "SELL_PENDING"):
                    continue
                t_inst = _dt212.instrument_for_ticker(str(t.get("ticker") or ""))
                if not t_inst:
                    continue
                canonical = _dt212.scanner_ticker_from_inst(t_inst)
                if str(t.get("ticker") or "").upper() != canonical:
                    db.execute(
                        "UPDATE dt_trades SET ticker=?, updated_ts=? WHERE id=?",
                        (canonical, time.time(), int(t["id"])),
                    )
                    t["ticker"] = canonical
            open_by_ticker = {
                t["ticker"].upper(): t
                for t in live_trades
                if str(t.get("status") or "").upper() == "OPEN"
            }
            pending_by_ticker = {
                t["ticker"].upper(): t
                for t in live_trades
                if str(t.get("status") or "").upper() == "SELL_PENDING"
            }
            trader = get_trader()
            seen: set[str] = set()
            seen_inst: set[str] = set()

            def _live_for_inst(inst_code: str) -> dict | None:
                inst_u = inst_code.upper()
                for t in live_trades:
                    st = str(t.get("status") or "").upper()
                    if st not in ("OPEN", "SELL_PENDING"):
                        continue
                    t_inst = _dt212.instrument_for_ticker(str(t.get("ticker") or ""))
                    if t_inst == inst_u:
                        return t
                return None

            def _open_for_inst(inst_code: str) -> dict | None:
                t = _live_for_inst(inst_code)
                if t and str(t.get("status") or "").upper() == "OPEN":
                    return t
                return None

            # Collapse duplicate OPEN rows for the same instrument (e.g. UGRO + FLZH)
            inst_groups: dict[str, list[dict]] = {}
            for t in live_trades:
                if str(t.get("status") or "").upper() != "OPEN":
                    continue
                t_inst = _dt212.instrument_for_ticker(str(t.get("ticker") or ""))
                if t_inst:
                    inst_groups.setdefault(t_inst, []).append(t)
            for t_inst, group in inst_groups.items():
                if len(group) <= 1:
                    continue
                group.sort(key=lambda x: int(x["id"]))
                keep = group[0]
                for dup in group[1:]:
                    if trader:
                        await trader._cancel_tp_limit(dup)
                        trader._monitors.pop(int(dup["id"]), None)
                    db.dt_close_trade(
                        int(dup["id"]), 0.0, "duplicate_adopt", 0.0, 0.0,
                    )
                    open_by_ticker.pop(str(dup.get("ticker") or "").upper(), None)
                    _log.info(
                        "Closed duplicate OPEN dt_trade #%s %s (same instrument as #%s %s)",
                        dup["id"], dup.get("ticker"), keep["id"], keep.get("ticker"),
                    )
                canonical = _dt212.scanner_ticker_from_inst(t_inst)
                if str(keep.get("ticker") or "").upper() != canonical:
                    db.execute(
                        "UPDATE dt_trades SET ticker=?, updated_ts=? WHERE id=?",
                        (canonical, time.time(), int(keep["id"])),
                    )
                    open_by_ticker.pop(str(keep.get("ticker") or "").upper(), None)
                    keep["ticker"] = canonical
                    open_by_ticker[canonical] = keep

            for pos in rows:
                inst = str(pos.get("ticker") or "").upper()
                if not inst:
                    continue
                seen_inst.add(inst)
                root = _dt212.scanner_ticker_from_inst(inst)
                qty = float(pos.get("quantity") or 0)
                if qty <= 0:
                    continue
                seen.add(root)
                ep = float(pos.get("averagePrice") or pos.get("averagePricePaid") or 0)
                trade = _live_for_inst(inst)
                if trade:
                    root = _dt212.scanner_ticker_from_inst(inst)
                    if str(trade.get("ticker") or "").upper() != root:
                        db.execute(
                            "UPDATE dt_trades SET ticker=?, updated_ts=? WHERE id=?",
                            (root, time.time(), int(trade["id"])),
                        )
                        open_by_ticker.pop(str(trade.get("ticker") or "").upper(), None)
                        trade["ticker"] = root
                        open_by_ticker[root] = trade
                    seen.add(root)
                    if str(trade.get("status") or "").upper() == "OPEN":
                        if not trade.get("entry_price") or float(trade.get("entry_price") or 0) <= 0:
                            db.dt_fill_trade(trade["id"], ep, qty, trade.get("t212_open_order_id"))
                            sig = trade.get("signal_limit_price")
                            if trader and sig and float(sig) > 0:
                                trader._finalize_entry_tp(int(trade["id"]), ep, float(sig))
                                trade = db.dt_trade_by_id(int(trade["id"])) or trade
                        if trader:
                            trader._start_monitor(trade["id"], root, inst)
                    continue
                # Heal latest REJECTED for this ticker (last 2h)
                rej = db.fetchone(
                    """SELECT * FROM dt_trades
                       WHERE ticker=? AND status='REJECTED'
                         AND created_ts > ?
                       ORDER BY id DESC LIMIT 1""",
                    (root, time.time() - 7200),
                )
                if rej:
                    db.dt_restore_trade(rej["id"], ep, qty, rej["t212_open_order_id"])
                    _log.info("Healed REJECTED trade #%s %s from broker position", rej["id"], root)
                    if trader:
                        trader._start_monitor(rej["id"], root, inst)
                    continue
                tid = db.dt_adopt_broker_trade(
                    root, ep, qty, entry_type="manual", stake_gbp=stake_gbp(), tp_pct=tp_pct(),
                )
                _log.info("Adopted broker position %s as dt_trade #%s", root, tid)
                if trader:
                    trader._start_monitor(tid, root, inst)

            # Confirm SELL_PENDING when broker is flat
            for ticker, trade in pending_by_ticker.items():
                if ticker in seen:
                    continue
                oid = str(trade.get("t212_close_order_id") or "").strip()
                inst = await _dt212.resolve_ticker(ticker)
                if oid and inst and trader:
                    await trader._confirm_close_from_history(
                        trade["id"],
                        ticker,
                        inst,
                        oid,
                        float(trade.get("entry_price") or 0),
                    )
                elif not oid:
                    db.dt_confirm_close_trade(trade["id"])

            # Close stale OPEN rows with no broker position (grace period for fills)
            for ticker, trade in open_by_ticker.items():
                if ticker in seen:
                    continue
                inst_u = (_dt212.instrument_for_ticker(ticker) or "").upper()
                if inst_u and inst_u in seen_inst:
                    continue
                st = str(trade.get("status") or "").upper()
                age = time.time() - float(trade.get("created_ts") or 0)
                entry_age = time.time() - float(
                    trade.get("entry_ts") or trade.get("created_ts") or 0
                )
                if st != "OPEN":
                    continue
                if not trade.get("entry_price") or float(trade.get("entry_price") or 0) <= 0:
                    if age < 180:
                        continue
                    oid = str(trade.get("t212_open_order_id") or "").strip()
                    inst = await _dt212.resolve_ticker(ticker)
                    if oid and inst:
                        avg_px, _, fill_qty = await _dt212.fetch_order_fill_from_history(
                            oid, inst, max_attempts=3, initial_delay_s=0.0,
                        )
                        if fill_qty and fill_qty > 0:
                            db.dt_fill_trade(trade["id"], avg_px or 0.0, fill_qty, oid)
                            sig = trade.get("signal_limit_price")
                            if trader and sig and float(sig) > 0:
                                trader._finalize_entry_tp(
                                    int(trade["id"]), float(avg_px or 0), float(sig),
                                )
                            if trader:
                                trader._start_monitor(trade["id"], ticker, inst)
                            continue
                    db.dt_reject_trade(trade["id"], "no broker fill after reconcile")
                elif entry_age >= 120:
                    # OPEN with fill in DB but broker flat — stale/orphan row
                    if trader:
                        await trader._cancel_tp_limit(trade)
                        trader._monitors.pop(int(trade["id"]), None)
                    db.dt_close_trade(
                        int(trade["id"]), 0.0, "broker_flat", 0.0, 0.0,
                    )
                    _log.info(
                        "Closed stale OPEN dt_trade #%s %s — broker flat",
                        trade["id"], ticker,
                    )
        except Exception:
            _log.exception("discord reconcile error")
        await asyncio.sleep(5.0)


# ── feed reader — taps the existing scanner_feed.jsonl ────────────────────────

_trader: DiscordTrader | None = None
_feed_running: bool = False

# SSE event queue — browser clients subscribe and get instant push
import queue as _queue
_sse_queues: list[_queue.SimpleQueue] = []
_sse_lock = __import__("threading").Lock()


def _sse_push(event_type: str, data: dict) -> None:
    """Push a JSON event to all connected SSE clients."""
    import json as _json
    payload = f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


def get_trader() -> DiscordTrader | None:
    return _trader


def is_running() -> bool:
    return _feed_running


async def _replay_recent_exits(trader: DiscordTrader, ch_id: int) -> None:
    """On startup, act on exit signals we missed while code was down or parser was incomplete."""
    from . import db

    cutoff = time.time() - 3600.0
    rows = db.fetchall(
        """SELECT content, received_ts FROM dt_messages
           WHERE channel_id=? AND received_ts >= ?
           ORDER BY received_ts ASC""",
        (str(ch_id), cutoff),
    )
    for row in rows:
        parsed = parse_message(row["content"] or "")
        if not parsed or parsed.get("type") not in ("market_exit", "stop_breach"):
            continue
        ticker = str(parsed.get("ticker") or "").upper()
        if not ticker:
            continue
        canonical = await trader._canonical_ticker(ticker)
        inst = await trader._resolve(ticker)
        opens = [
            t for t in db.dt_open_trades()
            if trader._trade_matches(t, canonical, inst)
            and float(t.get("created_ts") or 0) <= float(row["received_ts"] or 0) + 5.0
        ]
        if not opens:
            continue
        _log.info(
            "Replay missed %s for %s (signal %.0fs ago, %s open row(s))",
            parsed["type"], ticker, time.time() - float(row["received_ts"] or 0), len(opens),
        )
        await trader.handle_market_exit(ticker, reason=str(parsed["type"]))


async def _feed_loop() -> None:
    """Tail scanner_feed.jsonl, filter for DISCORD_TRADER_CHANNEL_ID messages."""
    global _trader
    from . import scanner_feed, db
    import json

    ch_id = channel_id()
    if not ch_id:
        _log.error("DISCORD_TRADER_CHANNEL_ID not set — Discord Trader cannot start")
        return

    global _feed_running
    _trader = DiscordTrader()
    _feed_running = True
    for w in db.dt_active_break_watches():
        db.dt_cancel_break_watch(int(w["id"]))
        _log.info("Cancelled legacy break watch #%s %s", w["id"], w.get("ticker"))
    _dt212.bind_loop(asyncio.get_running_loop())
    asyncio.create_task(_dt212.run_positions_poller(), name="dt-t212-positions")
    asyncio.create_task(_dt212.run_account_poller(), name="dt-t212-account")
    asyncio.create_task(_reconcile_broker_positions(), name="dt-reconcile")
    _log.info("Discord Trader watching channel_id=%s", ch_id)

    await _replay_recent_exits(_trader, ch_id)

    async for msg in scanner_feed.tail(interval=1.0, start_at_end=True):
        try:
            raw_ch = str(msg.get("channel_id") or "").strip()
            if not raw_ch.isdigit() or int(raw_ch) != ch_id:
                continue

            content = (msg.get("content") or "").strip()
            if not content:
                continue

            msg_id = str(msg.get("message_id") or "")
            author = str(msg.get("author") or "")
            raw_ts = msg.get("timestamp") or ""
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    ts = float(raw_ts)
                except Exception:
                    ts = time.time()

            parsed = parse_message(content)
            parsed_type = parsed["type"] if parsed else "ignored"

            db.dt_save_message(msg_id, raw_ch, author, content, ts,
                               parsed_type, json.dumps(parsed) if parsed else None)

            # Push real-time event to all connected SSE browser clients
            _sse_push("message", {
                "ts": ts, "author": author, "content": content,
                "parsed_type": parsed_type, "parsed": parsed,
            })

            if parsed is None or _trader is None:
                continue

            ptype = parsed["type"]
            ticker = parsed.get("ticker", "")
            if ticker:
                ticker = await _trader._canonical_ticker(ticker)

            if ptype == "entry_limit_placed":
                await _trader.handle_entry_limit_placed(
                    ticker,
                    msg_id,
                    float(parsed.get("limit_price") or 0),
                    str(parsed.get("entry_type") or "limit_buy"),
                )
                _sse_push("trade_update", {"action": "entry_placed", "ticker": ticker})
            elif ptype == "stop_breach":
                await _trader.handle_stop_breach(ticker)
                _sse_push("trade_update", {"action": "stop_breach", "ticker": ticker})
            elif ptype == "market_exit":
                await _trader.handle_market_exit(ticker)
                _sse_push("trade_update", {"action": "market_exit", "ticker": ticker})

        except Exception:
            _log.exception("discord_trader feed_loop error")


def start_in_background() -> None:
    """Launch the Discord Trader on its own asyncio loop in a daemon thread."""
    import threading

    if not enabled():
        _log.info("Discord Trader disabled (DISCORD_TRADER_ENABLED != 1)")
        return
    if not channel_id():
        _log.error("Discord Trader enabled but DISCORD_TRADER_CHANNEL_ID not set")
        return
    if not _t212_key():
        _log.error("Discord Trader enabled but TRADING_212_KEY_DISCORD not set")
        return

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_feed_loop())
        except Exception:
            _log.exception("Discord Trader feed loop crashed")
        finally:
            loop.close()

    t = threading.Thread(target=_runner, name="discord-trader", daemon=True)
    t.start()
    _log.info("Discord Trader thread started (channel=%s)", channel_id())
