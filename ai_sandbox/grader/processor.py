"""Scanner grader state machine orchestration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .. import config, db, human_labels
from . import hard_rules, messages, openai_grader, state as ticker_state

_log = logging.getLogger("ai_sandbox.grader.processor")


def _alert_snapshot(alert: dict[str, Any], *, ts: float | None = None) -> dict[str, Any]:
    when = alert.get("discord_ts") or alert.get("ts") or ts or time.time()
    if isinstance(when, (int, float)):
        iso = datetime.fromtimestamp(float(when), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        iso = str(when)
    indicators = list(alert.get("indicators") or [])
    tags = alert.get("tags") or []
    for t in tags:
        if t == "0Borrow":
            indicators.append("0 Borrow")
        elif t == "RegSHO":
            indicators.append("Reg SHO")
        elif t == "PotSqueeze":
            indicators.append("Potential Squeeze")
        elif t == "KnownRunner":
            indicators.append("Known Runner")
    return {
        "number": alert.get("rank") or alert.get("alert_number"),
        "price": round(float(alert.get("price") or 0), 2),
        "rv": alert.get("rv"),
        "label": alert.get("label"),
        "change_pct": alert.get("pct"),
        "indicators": indicators,
        "news": alert.get("news_headline"),
        "timestamp": iso,
        "float_shares": alert.get("float"),
        "market_cap": alert.get("market_cap"),
    }


def _decision_payload(result: dict[str, Any], *, reason: str) -> dict[str, Any]:
    action = str(result.get("action") or "PASS").upper()
    grade = str(result.get("grade") or "SKIP").upper()
    decision = "TRADE" if action == "TRADE" else ("WATCH" if action == "MONITOR" else "SKIP")
    score_map = {"STRONG": 85, "WATCH": 55, "SKIP": 10}
    return {
        "decision": decision,
        "grade": grade,
        "action": action,
        "score": score_map.get(grade, 40 if decision == "WATCH" else 10),
        "entry": result.get("entry_price"),
        "tp": result.get("target_price"),
        "reason": reason,
        "risk_flags": result.get("risk_flags") or [],
        "context": result.get("context") or {},
        "raw_json": result,
    }


async def process_scanner_alert(
    *,
    ticker: str,
    alert: dict[str, Any],
    alert_id: int,
    recent_entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Run New System grader path. Mutates recent_entry; returns decision dict if scored."""
    tk = ticker.upper()
    st_row = ticker_state.get_or_create(tk)
    st = str(st_row.get("state") or "NEW")

    if st in ("TRADE", "PASS", "DISQUALIFIED"):
        recent_entry["grader_state"] = st
        recent_entry["active_label"] = ticker_state.ui_label(st_row)
        return None

    eliminated, reason = hard_rules.hard_disqualify(alert)
    if eliminated:
        ticker_state.update(
            tk,
            {"state": "DISQUALIFIED", "disqualify_reason": reason},
        )
        recent_entry["filter_reason"] = reason
        recent_entry["grader_state"] = "DISQUALIFIED"
        recent_entry["active_label"] = "FILTERED"
        db.watch_episode_ensure_open(
            tk,
            alert_id=alert_id,
            added_ts=time.time(),
            event={
                "kind": "disqualified",
                "ts": time.time(),
                "reason": human_labels.humanize(reason),
                "alert_id": alert_id,
            },
        )
        return None

    alerts = list(st_row.get("alerts") or [])
    alerts.append(_alert_snapshot(alert))
    float_val = alert.get("float") or st_row.get("float_shares")
    next_state = "WATCHING" if st == "NEW" else st
    ticker_state.update(
        tk,
        {
            "state": next_state,
            "alerts": alerts,
            "float_shares": float_val,
        },
    )
    st_row = ticker_state.get_or_create(tk)
    recent_entry["grader_state"] = st_row.get("state")
    recent_entry["active_label"] = ticker_state.ui_label(st_row)

    ready, why = hard_rules.should_send_to_ai(st_row, alert)
    if not ready:
        recent_entry["defer_reason"] = why
        return None

    ticker_state.update(tk, {"state": "PENDING_AI"})
    recent_entry["grader_state"] = "PENDING_AI"
    recent_entry["active_label"] = "REVIEW"

    try:
        result, latency_ms, cost_gbp = await openai_grader.grade_alert(st_row)
    except Exception as exc:
        _log.exception("AI grader failed %s", tk)
        ticker_state.update(tk, {"state": st_row.get("state") or "WATCHING"})
        recent_entry["filter_reason"] = f"ai_error:{exc}"
        return None

    if not result:
        ticker_state.update(tk, {"state": "WATCHING"})
        recent_entry["filter_reason"] = "ai_parse_failed"
        return None

    user_message = messages.build_user_message(st_row)
    db.ai_decision_insert(
        ticker=tk,
        alert_number=len(alerts),
        alert_id=alert_id,
        ai_input=user_message,
        ai_output=result,
        grade=str(result.get("grade") or ""),
        action=str(result.get("action") or ""),
        entry_price=result.get("entry_price"),
        target_price=result.get("target_price"),
        latency_ms=latency_ms,
        cost_gbp=cost_gbp,
    )

    decision = _decision_payload(result, reason=why)
    action = str(result.get("action") or "PASS").upper()

    if action == "TRADE":
        ticker_state.update(
            tk,
            {
                "state": "TRADE",
                "ai_grade": result.get("grade"),
                "ai_decision": action,
                "entry_price": result.get("entry_price"),
                "target_price": result.get("target_price"),
                "ai_context": result.get("context") or {},
            },
        )
        recent_entry["decision"] = "TRADE"
        recent_entry["grader_state"] = "TRADE"
        recent_entry["active_label"] = "TRADE"
    elif action == "MONITOR":
        ticker_state.update(
            tk,
            {
                "state": "WATCH",
                "ai_grade": result.get("grade"),
                "ai_decision": action,
                "ai_context": result.get("context") or {},
            },
        )
        recent_entry["decision"] = "WATCH"
        recent_entry["grader_state"] = "WATCH"
        recent_entry["active_label"] = "REVIEW WATCH"
    else:
        ticker_state.update(
            tk,
            {
                "state": "PASS",
                "ai_grade": result.get("grade"),
                "ai_decision": action,
            },
        )
        recent_entry["decision"] = "SKIP"
        recent_entry["grader_state"] = "PASS"
        recent_entry["active_label"] = "FILTERED"
        recent_entry["filter_reason"] = "ai_pass"

    recent_entry["score"] = decision.get("score")
    db.log_score(
        alert_id,
        tk,
        decision,
        thinking_used=False,
    )
    return decision
