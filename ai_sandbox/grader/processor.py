"""Scanner grader state machine orchestration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .. import config, db
from . import hard_rules, messages, llm_grader, postprocess, state as ticker_state

_log = logging.getLogger("ai_sandbox.grader.processor")


def alert_snapshot(alert: dict[str, Any], *, ts: float | None = None) -> dict[str, Any]:
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
        "reverse_split": alert.get("reverse_split"),
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


def _apply_grader_result(
    *,
    tk: str,
    alerts: list[dict[str, Any]],
    alert_id: int,
    result: dict[str, Any],
    why: str,
    st_row: dict[str, Any],
    recent_entry: dict[str, Any],
    latency_ms: int = 0,
    cost_gbp: float = 0.0,
) -> dict[str, Any]:
    user_message = messages.build_user_message(st_row)
    from .ai_reasoning import clamp_summary_text, finalize_reasoning

    result = dict(result)
    result["reasoning"] = finalize_reasoning(result)
    if result.get("summary"):
        result["summary"] = clamp_summary_text(str(result["summary"]))
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
        recent_entry["disqualify_reason"] = "ai_pass"

    recent_entry["score"] = decision.get("score")
    db.log_score(alert_id, tk, decision, thinking_used=False)
    return decision


async def _run_openai_grade(
    tk: str,
    st_row: dict[str, Any],
    alerts: list[dict[str, Any]],
    alert_id: int,
    recent_entry: dict[str, Any],
    why: str,
) -> dict[str, Any] | None:
    ticker_state.update(tk, {"state": "PENDING_AI"})
    recent_entry["grader_state"] = "PENDING_AI"
    recent_entry["active_label"] = "REVIEW"

    try:
        result, latency_ms, cost_gbp = await llm_grader.grade_alert(st_row)
    except Exception:
        _log.exception("AI grader failed %s", tk)
        back = "WATCHING"
        ticker_state.update(tk, {"state": back})
        st_row = ticker_state.get_or_create(tk)
        recent_entry["grader_state"] = st_row.get("state")
        recent_entry["active_label"] = ticker_state.ui_label(st_row)
        return None

    _log.info(
        "AI grader %s ticker=%s latency_ms=%s cost_gbp=%.4f",
        config.grader_model(),
        tk,
        latency_ms,
        cost_gbp,
    )

    if not result:
        ticker_state.update(tk, {"state": "WATCHING"})
        st_row = ticker_state.get_or_create(tk)
        recent_entry["grader_state"] = "WATCHING"
        recent_entry["active_label"] = "WATCHING"
        return None

    st_row = ticker_state.get_or_create(tk)
    result = postprocess.apply_rules(st_row, result)
    return _apply_grader_result(
        tk=tk,
        alerts=alerts,
        alert_id=alert_id,
        result=result,
        why=why,
        st_row=st_row,
        recent_entry=recent_entry,
        latency_ms=latency_ms,
        cost_gbp=cost_gbp,
    )


async def process_scanner_alert(
    *,
    ticker: str,
    alert: dict[str, Any],
    alert_id: int,
    recent_entry: dict[str, Any],
    skip_append: bool = False,
) -> dict[str, Any] | None:
    """Run New System grader path. Mutates recent_entry; returns decision dict if scored."""
    tk = ticker.upper()
    st_row = ticker_state.get_or_create(tk)
    st = str(st_row.get("state") or "NEW")
    disqual = st_row.get("disqualify_reason")

    if st == "PENDING_AI":
        age = time.time() - float(st_row.get("updated_ts") or 0)
        if age < ticker_state._GRADER_IN_FLIGHT_SEC:
            recent_entry["grader_state"] = st
            recent_entry["active_label"] = ticker_state.ui_label(st_row)
            recent_entry["defer_reason"] = "grader_in_flight"
            return None
        ticker_state.update(tk, {"state": "WATCH"})
        st_row = ticker_state.get_or_create(tk)
        st = "WATCH"

    if st == "TRADE":
        from .. import ticker_identity

        match_sql, match_params = ticker_identity.trades_ticker_where_clause(tk)
        # Include SELL_PENDING: a sell order in flight is still an active position.
        # Without this, the grader resets to NEW with no reentry_active while the
        # broker exit is still confirming — bypassing all reentry guards entirely.
        open_row = db.fetchone(
            f"""SELECT id FROM trades
                WHERE status IN ('OPEN', 'SELL_PENDING')
                  AND {match_sql}
                LIMIT 1""",
            match_params,
        )
        if open_row:
            recent_entry["grader_state"] = st
            recent_entry["active_label"] = ticker_state.ui_label(st_row)
            recent_entry["defer_reason"] = "sell_in_flight"
            return None
        ticker_state.mark_traded(tk)
        ticker_state.reset_traded_for_new_alert(tk)
        st_row = ticker_state.get_or_create(tk)
        st = "NEW"

    if st == "TRADED":
        ticker_state.reset_traded_for_new_alert(tk)
        st_row = ticker_state.get_or_create(tk)
        st = "NEW"

    if hard_rules.is_nbreak_event(alert):
        recent_entry["grader_state"] = st
        recent_entry["active_label"] = "FILTERED"
        recent_entry["disqualify_reason"] = "nbreak_skip"
        recent_entry["defer_reason"] = "nbreak_skip"
        return None

    if st == "DISQUALIFIED":
        prior = list(st_row.get("alerts") or [])
        probe = prior + [alert_snapshot(alert)]
        if hard_rules.is_recoverable_disqualify(disqual) and hard_rules.label_ok_for_grade(
            alert, probe
        ):
            ticker_state.update(tk, {"state": "WATCHING", "disqualify_reason": None})
            st_row = ticker_state.get_or_create(tk)
            st = "WATCHING"
        else:
            recent_entry["grader_state"] = st
            recent_entry["active_label"] = ticker_state.ui_label(st_row)
            return None

    eliminated, reason = hard_rules.hard_disqualify(alert)
    if eliminated:
        ticker_state.update(
            tk,
            {"state": "DISQUALIFIED", "disqualify_reason": reason},
        )
        recent_entry["disqualify_reason"] = reason
        recent_entry["grader_state"] = "DISQUALIFIED"
        recent_entry["active_label"] = "FILTERED"
        return None

    if skip_append:
        alerts = list(st_row.get("alerts") or [])
    else:
        alerts = list(st_row.get("alerts") or [])
        alerts.append(alert_snapshot(alert))
        float_val = alert.get("float") or st_row.get("float_shares")
        next_state = "WATCHING" if st in ("NEW", "PASS") else st
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

    if alert.get("source") == "news_tester":
        ready, why = True, "news_tester_force"
    else:
        ready, why = hard_rules.should_send_to_ai(st_row, alert)
    if not ready:
        recent_entry["defer_reason"] = why
        return None

    return await _run_openai_grade(tk, st_row, alerts, alert_id, recent_entry, why)
