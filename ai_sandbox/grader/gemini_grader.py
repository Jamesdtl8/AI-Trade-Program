"""Scanner grader via Gemini (default: gemini-3.1-flash-lite, thinking off)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .. import config, json_parse
from .. import gemini_usage
from . import messages, prompt

_log = logging.getLogger("ai_sandbox.grader.gemini_grader")


def _extract_text(resp: Any) -> str:
    text = getattr(resp, "text", None)
    return str(text or "").strip()


async def grade_alert(state: dict[str, Any]) -> tuple[dict[str, Any] | None, int, float]:
    """Return (result_dict, latency_ms, cost_gbp)."""
    from google import genai
    from google.genai import types

    key = config.gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing in environment")

    model = config.grader_model()
    user_message = messages.build_user_message(state)
    use_thinking = config.grader_use_thinking()
    thinking_level = config.grader_thinking_level()

    cfg_kwargs: dict[str, Any] = {
        "system_instruction": prompt.SYSTEM_PROMPT,
        "temperature": 0.2,
        "max_output_tokens": config.grader_max_output_tokens(),
        "response_mime_type": "application/json",
    }
    if use_thinking:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)

    gen_cfg = types.GenerateContentConfig(**cfg_kwargs)
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=120_000))

    start = time.time()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=user_message,
            config=gen_cfg,
        )
    except Exception as exc:
        if use_thinking:
            _log.warning("grader Gemini thinking=%s failed, retry without: %s", thinking_level, exc)
            gen_cfg = types.GenerateContentConfig(
                system_instruction=prompt.SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=config.grader_max_output_tokens(),
                response_mime_type="application/json",
            )
            resp = client.models.generate_content(
                model=model,
                contents=user_message,
                config=gen_cfg,
            )
        else:
            raise

    latency_ms = int((time.time() - start) * 1000)
    cost_gbp = gemini_usage.record_from_response(
        resp,
        source="ai_sandbox",
        call_kind="scanner_grader",
        model=model,
        extra={
            "ticker": state.get("ticker"),
            "thinking": use_thinking,
        },
    )

    text_out = _extract_text(resp)
    result = json_parse.parse_decision_json(text_out)
    if result is None:
        _log.warning(
            "grader JSON parse failed ticker=%s model=%s raw=%s",
            state.get("ticker"),
            model,
            text_out[:400],
        )
        return None, latency_ms, cost_gbp
    return result, latency_ms, cost_gbp
