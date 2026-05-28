"""Log Gemini token usage and estimated GBP cost (all app callers).

After ``generate_content``, read ``response.usage_metadata`` (Google API):

- ``prompt_token_count`` — input tokens
- ``candidates_token_count`` — output tokens
- ``total_token_count`` — input + output (and any model-internal counts the API includes)

Optional: ``client.models.count_tokens`` before a call checks input size only; we do not
call it on every request to avoid doubling API traffic — post-response metadata is enough for billing logs.
"""

from __future__ import annotations

import logging
from typing import Any

from . import config, db

_log = logging.getLogger("ai_sandbox.gemini_usage")


def extract_token_counts(resp: Any) -> tuple[int, int, int]:
    """``(prompt_token_count, output_tokens_billed, total_token_count)`` from ``usage_metadata``."""
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return 0, 0, 0
    pt = int(getattr(um, "prompt_token_count", None) or 0)
    ct_raw = int(getattr(um, "candidates_token_count", None) or 0)
    thoughts = int(getattr(um, "thoughts_token_count", None) or 0)
    output_combined = ct_raw + thoughts
    tt = int(getattr(um, "total_token_count", None) or 0)
    if tt <= 0 and (pt > 0 or output_combined > 0):
        tt = pt + output_combined
    return pt, output_combined, tt


def usd_cost_for_tokens(model: str, prompt_tokens: int, output_tokens: int) -> float:
    in_m, out_m = config.gemini_token_price_usd_per_million(model)
    return (max(0, prompt_tokens) / 1_000_000.0) * in_m + (
        max(0, output_tokens) / 1_000_000.0
    ) * out_m


def gbp_cost_for_tokens(model: str, prompt_tokens: int, output_tokens: int) -> float:
    usd = usd_cost_for_tokens(model, prompt_tokens, output_tokens)
    if usd <= 0:
        return 0.0
    return round(config.usd_notionals_to_gbp(usd), 6)


def record_from_response(
    resp: Any,
    *,
    source: str,
    call_kind: str,
    model: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist one call row when the API returns usage metadata."""
    try:
        db.init()
    except Exception:
        pass
    pt, ct, tt = extract_token_counts(resp)
    if pt == 0 and ct == 0 and tt == 0:
        return
    cost = gbp_cost_for_tokens(model, pt, ct)
    try:
        db.gemini_usage_insert(
            source=source,
            call_kind=call_kind,
            model=model,
            input_tokens=pt,
            output_tokens=ct,
            total_tokens=tt,
            cost_gbp=cost,
            extra=extra,
        )
    except Exception:
        _log.debug("gemini_usage_insert failed", exc_info=True)


def stats_since(ts: float) -> dict[str, Any]:
    """Aggregate cost and counts from ``ts`` (unix) onward."""
    try:
        db.init()
    except Exception:
        return {"sum_gbp": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    return db.gemini_usage_stats_since(ts)
