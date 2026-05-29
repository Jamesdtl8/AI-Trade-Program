"""Build ALERT HISTORY user message for the GPT grader."""

from __future__ import annotations

import json
from typing import Any

from .. import db
from . import hard_rules, reentry


def fmt_number(val: float | int | None) -> str:
    if val is None:
        return "N/A"
    v = float(val)
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


def build_user_message(state: dict[str, Any]) -> str:
    alerts: list[dict[str, Any]] = state.get("alerts") or []
    latest_news = None
    for a in reversed(alerts):
        if a.get("news"):
            latest_news = a["news"]
            break

    lines = ["ALERT HISTORY:\n"]
    for i, a in enumerate(alerts, 1):
        news_text = a.get("news") or latest_news or "none"
        label_text = a.get("label") or "none"
        ind = a.get("indicators") or []
        if not ind and a.get("tags"):
            ind = list(a.get("tags") or [])
        rs_text = a.get("reverse_split") or "none"
        line = (
            f"#{i} | {a.get('timestamp', '')} | ${round(float(a.get('price') or 0), 2)} | "
            f"FT {fmt_number(a.get('float_shares'))} | "
            f"MC {fmt_number(a.get('market_cap'))} | "
            f"RV {a.get('rv', 0)}x | "
            f"R/S {rs_text} | "
            f"IND: {', '.join(ind) or 'none'} | "
            f"Label: {label_text} | "
            f"Change: {a.get('change_pct', 0)}% | "
            f"News: {news_text}"
        )
        lines.append(line)

    alert_count = len(alerts)
    if alert_count == 2:
        lines.append(
            "\nALERT 2 INITIAL GRADE — early entry look. "
            "STRONG/TRADE is allowed when float is tight, squeeze flags present, "
            "price rising, and MOMENTUM/BREAKOUT label. Gate2 may be weak."
        )
    elif alert_count == 3:
        lines.append(
            "\nALERT 3 STANDARD GRADE — reassess fully; do not anchor on any prior alert-2 decision."
        )
    has_news = any(a.get("news") for a in alerts) or any(
        str(a.get("news") or "").strip() and str(a.get("news")).lower() not in ("none", "same")
        for a in alerts
    )
    if alert_count >= 3 and has_news:
        lines.append(
            "NEWS MOMENTUM OVERRIDE — if float is tight, Gate2 news PASS, RV≥90x, and last 2 alerts "
            "are rising MOMENTUM/BREAKOUT, STRONG/TRADE even without 0 Borrow/Reg SHO "
            "(Gate 3 FAIL ok). Do not wait for RV to hit 100x or for alert 6."
        )
    if alert_count >= 4:
        lines.append(
            "\nCONTINUATION REGRADE (alert "
            f"{alert_count}) — reassess from full history. "
            "Do not anchor on a prior WATCH. "
            "entry_price = current alert price × 1.03. "
            "Apply momentum override if 4+ consecutive MOMENTUM/BREAKOUT, "
            "strictly rising price, RV > 100x, and Gates 1/2 pass (Gate4 does not block)."
        )

    ticker = str(state.get("ticker") or "").strip().upper()
    if ticker:
        last_review = db.last_ai_decision_for_ticker(
            ticker,
            within_sec=hard_rules.REVIEW_REGRADE_WINDOW_SEC,
        )
        if (
            hard_rules.is_positive_ai_review(last_review)
            and alert_count > int((last_review or {}).get("alert_number") or 0)
        ):
            lines.append(
                "\nWATCHLIST REGRADE — this ticker had a positive GPT review within the "
                f"last {int(hard_rules.REVIEW_REGRADE_WINDOW_SEC / 3600)} hours "
                f"(alert #{last_review.get('alert_number')} → {last_review.get('grade')}/"
                f"{last_review.get('action')}). "
                f"You now have {alert_count} alerts. Reassess fully; do not anchor on the "
                "prior WATCH if momentum, price, or RV have improved."
            )
    if alert_count >= 3 and any(
        t in ("Known Runner", "KnownRunner")
        for a in alerts
        for t in (a.get("indicators") or []) + list(a.get("tags") or [])
    ):
        lines.append(
            "KNOWN RUNNER OVERRIDE — if RV > 100x and either (a) last 3 alerts are rising "
            "MOMENTUM/BREAKOUT or (b) REV V/NBREAK dip then price reclaims the pre-dip high with "
            "3 rising momentum prints in the last 6 alerts, STRONG/TRADE is allowed even "
            "without news (Gate 2 FAIL or PARTIAL ok — auto-upgraded PARTIAL does not satisfy "
            "the named-news bar, treat it the same as FAIL for this override)."
        )

    lines.append(
        "Include a brief plain-English summary (max 2 short sentences, ~40 words) in the summary field. "
        "Say TRADE / WATCH / PASS in normal language — no Gate 1 / PASS_STRONG jargon."
    )

    if str(state.get("state") or "") == "WATCH" and state.get("ai_context"):
        ctx = state["ai_context"]
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {"raw": ctx}
        lines.append("\nPRIOR AI DECISION:")
        lines.append(json.dumps(ctx, indent=2, default=str))

    prior = reentry.resolve_prior_trade(state, scanner_ticker=state.get("ticker"))
    if prior:
        lines.extend(reentry.format_prior_trade_block(prior))
        lines.append(
            "\nRE-ENTRY GRADE — this is a second (or later) episode today after a closed trade. "
            "Do not treat prior-run alerts as current context. "
            "Default PASS or MONITOR unless the new episode independently re-validates momentum "
            f"with RV ≥ {reentry.REENTRY_RV_MIN:.0f}x and no chase above "
            f"{reentry.REENTRY_PRICE_EXIT_MULT:.1f}× the prior exit."
        )

    return "\n".join(lines)
