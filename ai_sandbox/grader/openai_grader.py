"""GPT-5-nano scanner grader via OpenAI Responses API."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import config
from ..gemini_ai import _extract_balanced_json_object, _parse_scorer_json, _strip_json_fences
from . import messages, prompt
from .. import openai_usage

_log = logging.getLogger("ai_sandbox.grader.openai_grader")


def _parse_grader_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    bracket = _extract_balanced_json_object(raw)
    candidates = [_strip_json_fences(raw), raw]
    if bracket:
        candidates.extend([bracket, _strip_json_fences(bracket)])
    seen: set[str] = set()
    for cand in candidates:
        c = cand.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            out = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(out, dict) and (out.get("action") or out.get("grade")):
            return out
    return _parse_scorer_json(text)


async def grade_alert(state: dict[str, Any]) -> tuple[dict[str, Any] | None, int, float]:
    """Return (result_dict, latency_ms, cost_gbp)."""
    from openai import OpenAI

    key = config.openai_api_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing in environment")

    model = config.openai_model_grader()
    user_message = messages.build_user_message(state)
    start = time.time()

    client = OpenAI(api_key=key)
    resp = client.responses.create(
        model=model,
        input=[
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": prompt.SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_message}],
            },
        ],
        text={"format": {"type": "text"}, "verbosity": "medium"},
        reasoning={"effort": "low", "summary": "auto"},
        tools=[],
        store=False,
    )

    latency_ms = int((time.time() - start) * 1000)
    cost_gbp = openai_usage.record_from_response(
        resp,
        source="ai_sandbox",
        call_kind="scanner_grader",
        model=model,
        extra={"ticker": state.get("ticker")},
    )

    text_out = ""
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text":
                text_out += getattr(block, "text", "") or ""

    if not text_out.strip():
        text_out = getattr(resp, "output_text", None) or ""

    result = _parse_grader_json(text_out)
    if result is None:
        _log.warning("grader JSON parse failed ticker=%s raw=%s", state.get("ticker"), text_out[:400])
        return None, latency_ms, cost_gbp
    return result, latency_ms, cost_gbp
