"""Gemini entry decision (trading_model_rules_and_prompting.md §3.1)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import config, db, entry_prompt, entry_rules
from .entry_rules import EntryDecision

_log = logging.getLogger("ai_sandbox.entry_grader")

_DECISION_RE = re.compile(r"^Decision:\s*(TRADE_NOW|WATCH_ONLY|SKIP)\s*$", re.I | re.M)
_SETUP_RE = re.compile(r"^Matched setup:\s*(.+?)\s*$", re.I | re.M)
_REJECT_RE = re.compile(r"^Reject reason:\s*(.+?)\s*$", re.I | re.M)


def parse_entry_response(text: str) -> dict[str, Any] | None:
    """Parse §3.2 plain-text AI output."""
    raw = (text or "").strip()
    if not raw:
        return None
    out: dict[str, Any] = {"raw_text": raw}
    m = _DECISION_RE.search(raw)
    if not m:
        return None
    out["decision"] = m.group(1).upper()
    sm = _SETUP_RE.search(raw)
    if sm:
        setup = sm.group(1).strip()
        if setup.lower() not in ("none", "n/a", "-"):
            out["matched_setup"] = setup
    rm = _REJECT_RE.search(raw)
    if rm:
        reason = rm.group(1).strip()
        if reason.lower() not in ("none", "n/a", "-"):
            out["reject_reason"] = reason
    return out


def merge_ai_with_rules(rules: EntryDecision, ai: dict[str, Any] | None) -> EntryDecision:
    """Rules are authoritative on hard rejects; AI may downgrade TRADE_NOW only."""
    if not ai or not ai.get("decision"):
        return rules
    ai_dec = str(ai["decision"]).upper()
    if rules.decision == "SKIP" and rules.reject_reason:
        return rules
    if rules.decision == "TRADE_NOW" and ai_dec in ("WATCH_ONLY", "SKIP"):
        notes = f"AI downgrade {ai_dec}: {ai.get('reject_reason') or 'no setup'}"
        return EntryDecision(
            decision=ai_dec,
            matched_setup=rules.matched_setup,
            reject_reason=ai.get("reject_reason") or "ai_downgrade",
            size_grade=rules.size_grade,
            structure_grade=rules.structure_grade,
            tape_confirmed=rules.tape_confirmed,
            notes=notes,
            context={**rules.context, "ai_decision": ai},
        )
    if rules.decision == "WATCH_ONLY" and ai_dec == "TRADE_NOW":
        return rules
    return rules


async def grade_entry(
    ticker: str,
    alert_id: int,
    alert: dict[str, Any],
    rules: EntryDecision,
    *,
    news_class: str | None = None,
) -> tuple[EntryDecision, dict[str, Any] | None]:
    """Call Gemini with §3.1 prompt; return merged decision + raw AI parse."""
    if not config.entry_ai_enabled():
        return rules, None
    key = config.gemini_api_key()
    if not key:
        return rules, None

    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/London")
    except Exception:
        tz = None
    now = datetime.now(tz=tz) if tz else datetime.utcnow()
    prompt = entry_prompt.build_prompt(
        rules.context,
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
    )

    model = config.entry_ai_model()
    start = time.time()
    text_out = ""
    cost_gbp = 0.0
    try:
        from google import genai
        from google.genai import types

        from . import gemini_usage

        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=90_000))
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        text_out = str(getattr(resp, "text", None) or "").strip()
        cost_gbp = gemini_usage.record_from_response(
            resp,
            source="ai_sandbox",
            call_kind="entry_grader",
            model=model,
            extra={"ticker": ticker, "alert_id": alert_id},
        )
    except Exception as exc:
        _log.warning("entry grader Gemini failed %s: %s", ticker, exc)
        return rules, None

    latency_ms = int((time.time() - start) * 1000)
    parsed = parse_entry_response(text_out)
    merged = merge_ai_with_rules(rules, parsed)

    ai_out = {
        "source": "entry_grader",
        "prompt": prompt,
        "response": text_out,
        "parsed": parsed,
        "rules_decision": rules.decision,
        "final_decision": merged.decision,
        "latency_ms": latency_ms,
        "cost_gbp": cost_gbp,
    }
    try:
        db.ai_decision_insert(
            ticker=ticker,
            alert_number=int(rules.context.get("alert_number") or 0),
            alert_id=alert_id,
            ai_input=prompt,
            ai_output=ai_out,
            grade=merged.decision,
            action=merged.decision,
            entry_price=rules.context.get("price"),
            target_price=None,
            latency_ms=latency_ms,
            cost_gbp=cost_gbp,
        )
    except Exception:
        _log.debug("entry ai_decision_insert failed", exc_info=True)

    return merged, ai_out
