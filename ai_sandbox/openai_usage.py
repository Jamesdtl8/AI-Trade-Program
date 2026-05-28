"""OpenAI Responses API usage logging and cost estimates."""

from __future__ import annotations

import logging
from typing import Any

from . import config, db

_log = logging.getLogger("ai_sandbox.openai_usage")


def extract_token_counts(resp: Any) -> tuple[int, int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0, 0
    pt = int(getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None) or 0)
    ot = int(
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", None)
        or 0
    )
    tt = int(getattr(usage, "total_tokens", None) or 0)
    if tt <= 0 and (pt > 0 or ot > 0):
        tt = pt + ot
    return pt, ot, tt


def usd_cost_for_tokens(model: str, prompt_tokens: int, output_tokens: int) -> float:
    in_m, out_m = config.openai_token_price_usd_per_million(model)
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
) -> float:
    try:
        db.init()
    except Exception:
        pass
    pt, ot, tt = extract_token_counts(resp)
    cost = gbp_cost_for_tokens(model, pt, ot)
    if pt == 0 and ot == 0 and tt == 0 and cost <= 0:
        return 0.0
    try:
        db.openai_usage_insert(
            source=source,
            call_kind=call_kind,
            model=model,
            input_tokens=pt,
            output_tokens=ot,
            total_tokens=tt,
            cost_gbp=cost,
            extra=extra,
        )
    except Exception:
        _log.debug("openai_usage_insert failed", exc_info=True)
    return cost


def stats_since(ts: float) -> dict[str, Any]:
    try:
        db.init()
    except Exception:
        return {"sum_gbp": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    return db.openai_usage_stats_since(ts)
