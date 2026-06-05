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
    alert: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    """Build a single-message trade signal for #ai-trade.

    Fires the moment the AI decides TRADE — before broker execution — so the
    channel can act on the signal immediately.
    """
    raw_ticker = (alert.get("ticker") or ticker or "?").upper()

    # Entry price = alert price + 5%
    alert_price = float(alert.get("price") or 0)
    entry_price = round(alert_price * 1.05, 4) if alert_price > 0 else None

    # Alert context
    rv = alert.get("rv")
    pct = alert.get("pct")
    label = (alert.get("label") or "").upper()
    float_shares = alert.get("float")
    alert_num = alert.get("rank") or alert.get("alert_number") or "?"
    news_class = (alert.get("news_class") or "").strip()

    # AI reasoning
    reasoning = (decision.get("reasoning") or decision.get("reason") or "").strip()
    if len(reasoning) > 300:
        reasoning = reasoning[:297] + "…"

    # Risk flags
    risk_flags: list[str] = decision.get("risk_flags") or []

    lines: list[str] = []

    # Line 1 — header with entry price
    header = f"🟢 **TRADE — {raw_ticker}**"
    if entry_price:
        header += f"  ·  Entry Price {_fmt_price(entry_price)}"
    lines.append(header)

    # Line 2 — alert snapshot
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

    # Line 3 — news catalyst if present
    if news_class and news_class.lower() not in ("none", "unknown", ""):
        lines.append(f"📰 {news_class}")

    # Line 4 — AI reasoning
    if reasoning:
        lines.append(f"💡 {reasoning}")

    # Line 5 — risk flags
    if risk_flags:
        lines.append(f"⚠️ {', '.join(str(f) for f in risk_flags[:4])}")

    return "\n".join(lines)


async def post_trade_signal(
    ticker: str,
    alert: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """Fire-and-forget: post the TRADE signal to #ai-trade the instant AI decides."""
    channel_id = _CHANNEL_ID
    if not channel_id:
        _log.debug("AI_TRADE_DISCORD_CHANNEL_ID not set — trade notification skipped")
        return

    content = build_trade_message(ticker, alert, decision)

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_RELAY_URL}/publish",
                json={"channel_id": channel_id, "content": content},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status not in (200, 202):
                    body = await resp.text()
                    _log.warning(
                        "discord_notifier: relay returned %s — %s", resp.status, body[:200]
                    )
                else:
                    _log.info("discord_notifier: signal posted for %s", ticker)
    except Exception:
        _log.exception("discord_notifier: failed to post signal for %s", ticker)
