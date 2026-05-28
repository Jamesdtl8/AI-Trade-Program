"""Build ALERT HISTORY user message for the GPT grader."""

from __future__ import annotations

import json
from typing import Any


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
        line = (
            f"#{i} | {a.get('timestamp', '')} | ${round(float(a.get('price') or 0), 2)} | "
            f"FT {fmt_number(a.get('float_shares'))} | "
            f"MC {fmt_number(a.get('market_cap'))} | "
            f"RV {a.get('rv', 0)}x | "
            f"IND: {', '.join(ind) or 'none'} | "
            f"Label: {label_text} | "
            f"Change: {a.get('change_pct', 0)}% | "
            f"News: {news_text}"
        )
        lines.append(line)

    if str(state.get("state") or "") == "WATCH" and state.get("ai_context"):
        ctx = state["ai_context"]
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {"raw": ctx}
        lines.append("\nPRIOR AI DECISION:")
        lines.append(json.dumps(ctx, indent=2, default=str))

    return "\n".join(lines)
