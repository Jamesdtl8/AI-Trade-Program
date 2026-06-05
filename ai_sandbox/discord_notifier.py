"""Post AI trade decisions to the #ai-trade Discord channel.

Uses the existing Trading Platform Discord relay's ``POST /publish`` endpoint
(same relay the audit-log mirror uses).  No new connection — we just fire-and-
forget an HTTP POST from a background asyncio task so the trade loop is never
blocked.

Configuration (AI Trade Program .env):
    AI_TRADE_DISCORD_CHANNEL_ID   — snowflake for #ai-trade
    DISCORD_RELAY_URL             — defaults to http://127.0.0.1:5061
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

_RELAY_URL = (os.environ.get("DISCORD_RELAY_URL") or "http://127.0.0.1:5061").rstrip("/")
_CHANNEL_ID = (os.environ.get("AI_TRADE_DISCORD_CHANNEL_ID") or "").strip()


def _fmt_price(p: float | None) -> str:
    if not p or p <= 0:
        return "—"
    return f"${p:.2f}" if p < 1000 else f"${p:,.2f}"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return ""
    return f"+{p:.0f}%" if p >= 0 else f"{p:.0f}%"


def build_trade_message(
    ticker: str,
    eff_entry: float,
    tp: float,
    stop: float,
    alert: dict[str, Any],
    decision: dict[str, Any],
    capital_gbp: float = 0.0,
) -> str:
    """Build a compact Discord message for a confirmed TRADE fill."""
    raw_ticker = (alert.get("ticker") or ticker or "?").upper()

    # --- Alert context ---
    rv = alert.get("rv")
    pct = alert.get("pct")
    label = (alert.get("label") or "").upper()
    float_shares = alert.get("float")
    alert_num = alert.get("rank") or alert.get("alert_number") or "?"
    news_class = (alert.get("news_class") or "").strip()

    # --- AI reasoning from decision ---
    reasoning = (decision.get("reasoning") or decision.get("reason") or "").strip()
    # Trim reasoning to a sensible Discord length
    if len(reasoning) > 280:
        reasoning = reasoning[:277] + "…"

    # --- Suggested entry (5% above alert price, shown as reference) ---
    alert_price = float(alert.get("price") or 0)
    suggested = round(alert_price * 1.05, 4) if alert_price > 0 else None

    # --- Risk flags ---
    risk_flags: list[str] = decision.get("risk_flags") or []

    lines: list[str] = []

    # Header
    lines.append(f"🟢 **TRADE — {raw_ticker} — Entry {_fmt_price(eff_entry)}**")

    # Alert snapshot
    snap_parts: list[str] = []
    if rv:
        snap_parts.append(f"RV {rv:,.0f}x")
    if pct:
        snap_parts.append(f"{_fmt_pct(float(pct))} on day")
    if float_shares:
        try:
            fl = float(float_shares)
            snap_parts.append(f"Float {fl/1e6:.1f}M" if fl >= 1e6 else f"Float {fl:,.0f}")
        except (TypeError, ValueError):
            pass
    if label:
        snap_parts.append(label)
    if alert_num and alert_num != "?":
        snap_parts.append(f"Alert #{alert_num}")
    if snap_parts:
        lines.append("📡 " + " · ".join(snap_parts))

    # Targets
    target_parts: list[str] = []
    if tp > 0:
        tp_pct = round((tp / eff_entry - 1) * 100, 1) if eff_entry > 0 else None
        target_parts.append(f"🎯 TP {_fmt_price(tp)}" + (f" (+{tp_pct}%)" if tp_pct else ""))
    if stop > 0:
        stop_pct = round((1 - stop / eff_entry) * 100, 1) if eff_entry > 0 else None
        target_parts.append(f"🛑 Stop {_fmt_price(stop)}" + (f" (-{stop_pct}%)" if stop_pct else ""))
    if suggested and suggested != eff_entry:
        target_parts.append(f"Signal ref {_fmt_price(suggested)}")
    if capital_gbp > 0:
        target_parts.append(f"£{capital_gbp:,.0f} deployed")
    if target_parts:
        lines.append(" · ".join(target_parts))

    # News/catalyst
    if news_class and news_class.lower() not in ("none", "unknown", ""):
        lines.append(f"📰 {news_class}")

    # AI reasoning
    if reasoning:
        lines.append(f"💡 {reasoning}")

    # Risk flags (brief)
    if risk_flags:
        flags_str = ", ".join(str(f) for f in risk_flags[:4])
        lines.append(f"⚠️ Risks: {flags_str}")

    return "\n".join(lines)


async def post_trade(
    ticker: str,
    eff_entry: float,
    tp: float,
    stop: float,
    alert: dict[str, Any],
    decision: dict[str, Any],
    capital_gbp: float = 0.0,
) -> None:
    """Fire-and-forget: build and POST the trade notification to #ai-trade."""
    channel_id = _CHANNEL_ID
    if not channel_id:
        _log.debug("AI_TRADE_DISCORD_CHANNEL_ID not set — trade notification skipped")
        return

    content = build_trade_message(ticker, eff_entry, tp, stop, alert, decision, capital_gbp)

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_RELAY_URL}/publish",
                json={"channel_id": channel_id, "content": content},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    _log.warning(
                        "discord_notifier: relay returned %s — %s", resp.status, body[:200]
                    )
                else:
                    _log.info("discord_notifier: trade notification posted for %s", ticker)
    except Exception:
        _log.exception("discord_notifier: failed to post trade for %s", ticker)
