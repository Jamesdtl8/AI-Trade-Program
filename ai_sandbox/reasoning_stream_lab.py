"""Standalone streamed Gemini lab: synthetic price path + HOLD / SELL_PROFIT / SELL_LOSS.

Each tick uses **one isolated** ``generate_content_stream`` call (no chat history). Prompt carries a
fixed trade briefing, JSON state (updated in Python), the last 15 tick lines, and the current tick —
token use per tick stays ~flat. Transient API errors (503 / overload) retry with backoff on the same
tick before failing. Not wired into live trading.

CLI::

    python -m ai_sandbox.reasoning_stream_lab

Dashboard hits ``POST /api/reasoning-test/stream`` (SSE). Payload fields::

    system_prompt — **full** system instruction (optional). Placeholders replaced each run:
    ``{{RISK_LEVEL}}``, ``{{RISK_BLURB}}``, ``{{ENTRY_PRICE}}``, ``{{STOP_LOSS}}``,
    ``{{TARGET_PRICE}}``, ``{{CRASH_EXIT_PRICE}}`` (USD strings). If omitted, a built-in default is used.

    risk_blurb — optional override for ``{{RISK_BLURB}}``; empty uses tier text from risk_level.

    crash_exit_price — optional USD threshold for crash exit. If omitted, defaults to **5% below stop**
    (stop × 0.95). STATE.crash_exit becomes true when price falls through this level on a tick.

    path_mode: "random" | "controlled"
    path_start_price, path_end_price  — tape anchors at 0% and 100% of simulated time
    path_waypoints: { "25": 102.5, "50": 99 } — optional interior knots at 5,10,...,95.

Tape versus ``entry_price``: entry stays the trade’s baseline for ``pct_vs_entry`` in PRICE_UPDATE;
the synthetic path can diverge (e.g. stress scenarios).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any, Callable, Generator, Iterable

DEFAULT_MODEL = "gemini-2.5-flash-lite"

# Rolling price lines included in each stateless prompt (excluding the current tick line).
ROLLING_TICKS = 15

# When payload omits ``crash_exit_price``, lab uses stop × (1 − this fraction) as crash threshold.
LAB_CRASH_DEFAULT_PCT_BELOW_STOP = 0.05

# Simple trailing-stop latch for STATE.trailing_stop_hit (see ``_update_lab_state``).
LAB_TRAILING_ARM_MIN_PEAK_GAIN_PCT = 3.0
LAB_TRAILING_PULLBACK_FROM_PEAK_PCT = 1.5

# Retries when Gemini returns overload / capacity errors (e.g. 503) mid-stream or on connect.
LAB_TRANSIENT_MAX_RETRIES = 6
LAB_TRANSIENT_BASE_DELAY_S = 2.0
LAB_TRANSIENT_MAX_DELAY_S = 32.0

# In ``stable_chop``, skip Gemini when phase is unchanged vs prior tick and |Δprice| vs prior ≤ this %.
LAB_SKIP_AI_STABLE_CHOP_MOVE_PCT = 0.5

# Minimum unrealized PnL % vs entry before SELL_PROFIT is allowed (server-enforced).
LAB_MIN_PROFIT_SELL_PCT = 4.0
# AI-emitted SELL_LOSS requires this many consecutive ticks below stop (crash exit exempt).
LAB_TICKS_BELOW_INVALIDATION_FOR_LOSS = 30


def _lab_infer_provider(model: str, explicit: str | None) -> str:
    """``provider`` payload overrides; else route ``gpt-*`` / ``o*`` to OpenAI."""
    e = (explicit or "").strip().lower()
    if e in ("openai", "gemini"):
        return e
    m = (model or "").strip().lower()
    if m.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return "openai"
    return "gemini"


# USD per 1M tokens (input, output) for Reasoning Test cost footer — rough list prices.
_OPENAI_LAB_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-nano-2025-08-07": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
}


def _openai_lab_price_rates(model: str) -> tuple[float, float, str]:
    """Return (input $/1M, output $/1M, provenance note)."""
    try:
        inp_e = os.environ.get("OPENAI_LAB_PRICE_IN_PER_MTOK", "").strip()
        out_e = os.environ.get("OPENAI_LAB_PRICE_OUT_PER_MTOK", "").strip()
        if inp_e and out_e:
            return float(inp_e), float(out_e), "OPENAI_LAB_PRICE_* env"
    except ValueError:
        pass
    m = (model or "").strip().lower()
    if m in _OPENAI_LAB_PRICE_PER_MTOK:
        a, b = _OPENAI_LAB_PRICE_PER_MTOK[m]
        return a, b, f"built-in {m}"
    for prefix, rates in _OPENAI_LAB_PRICE_PER_MTOK.items():
        if m.startswith(prefix):
            return rates[0], rates[1], f"built-in prefix {prefix}"
    return 0.50, 2.00, "fallback (set OPENAI_LAB_PRICE_IN_PER_MTOK / OUT)"


def _openai_lab_turn_cost_usd(model: str, usage: dict[str, int] | None) -> float:
    if not usage:
        return 0.0
    it = int(usage.get("prompt_tokens", 0))
    ot = int(usage.get("candidates_tokens", 0))
    inp_r, out_r, _ = _openai_lab_price_rates(model)
    return (it / 1_000_000.0) * inp_r + (ot / 1_000_000.0) * out_r


def _is_transient_openai_error(exc: BaseException) -> bool:
    if _is_transient_api_error(exc):
        return True
    code = getattr(exc, "status_code", None)
    if code is not None:
        try:
            if int(code) in {408, 425, 429, 500, 502, 503, 504}:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _is_transient_api_error(exc: BaseException) -> bool:
    """True for rate limits and short-lived capacity errors worth retrying."""
    msg = str(exc).upper()
    if any(
        x in msg
        for x in (
            "503",
            "502",
            "504",
            "429",
            "UNAVAILABLE",
            "RESOURCE_EXHAUSTED",
            "DEADLINE_EXCEEDED",
            "HIGH DEMAND",
            "OVERLOADED",
            "TRY AGAIN LATER",
            "SERVICE UNAVAILABLE",
            "UNEXPECTED EOF",
            "CONNECTION RESET",
        )
    ):
        return True
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            if int(code) in {429, 502, 503, 504}:
                return True
        except (TypeError, ValueError):
            pass
    if msg.strip() in {"UNAVAILABLE", "RESOURCE_EXHAUSTED"}:
        return True
    return False


def _model_supports_thinking_config(model: str) -> bool:
    """Gemini ``thinking_config`` is not valid on all models (e.g. 2.5 flash-lite)."""
    m = (model or "").strip().lower()
    if "flash-lite" in m and "2.5" in m:
        return False
    return True


DEFAULT_BACK_STORY = """Entered DEMO_US on a constructive reversal setup after a flush into demand,
with confirmation from breadth and sector peers stabilizing. Thesis: squeeze of weaker shorts into
a liquidity pocket toward prior swing resistance; invalidation if we lose the session VWAP reclaim."""

# Full default system instruction (Gemini / OpenAI lab). Price placeholders filled from cfg each run.
# Mechanical exits (crash, trailing ≥ min profit %) are enforced in Python before/after the model.
DEFAULT_FULL_SYSTEM_PROMPT_TEMPLATE = """You are a disciplined LONG trade monitor.

Use only the supplied state, tape, operator notes, and risk profile. Do not invent news, catalysts, volume, or price action.

ENTRY={{ENTRY_PRICE}}
STOP={{STOP_LOSS}}
TARGET={{TARGET_PRICE}}
CRASH_EXIT={{CRASH_EXIT_PRICE}}

RISK PROFILE:
RISK={{RISK_LEVEL}}/10
{{RISK_BLURB}}

Apply the risk profile when judging what is normal noise versus real failure. Higher risk means more volatility may be normal. Lower risk means deterioration matters sooner. Never override hard rules.

Hard rules:
- If position_status is closed, output exactly: POSITION CLOSED
- If price <= CRASH_EXIT, decision must be SELL_LOSS.
- SELL_LOSS is only valid if price <= CRASH_EXIT or ticks_below_invalidation >= 30.
- Price below entry alone is not a sell reason.
- Brief stop touches, wicks, pauses, and small pullbacks are noise unless the risk profile says they are abnormal.

Profit rules:
- TARGET is the intended profit exit.
- Do not sell early just because the trade is green.
- SELL_PROFIT is forbidden when unrealized_pnl_pct < 4.
- +4% is not a sell trigger. It only allows early profit-selling to be considered.
- If price >= TARGET, decision should be SELL_PROFIT unless tape shows strong breakout continuation.
- If trailing_stop_hit is true and unrealized_pnl_pct >= 4, decision must be SELL_PROFIT.

Early profit exit rule:
Before TARGET, SELL_PROFIT should be rare.
Only SELL_PROFIT before TARGET if the move clearly fails beyond normal noise for this risk profile.

Before TARGET, SELL_PROFIT requires ALL:
1. unrealized_pnl_pct >= 4
2. price has moved meaningfully away from recent highs
3. consecutive lower highs
4. consecutive lower lows OR clear support break
5. stalling price action
6. loss of upward momentum
7. downward trend visible across at least 15 ticks
8. the weakness is abnormal for the supplied risk profile

If any condition is missing, HOLD.
If the tape is mixed, HOLD.
If price is still grinding towards TARGET, HOLD.
If the move is normal noise for this risk profile, HOLD.
If unsure, HOLD.

Default:
- Default decision is HOLD.
- The monitor should give winning trades room to reach TARGET.
- HOLD is preferred over early SELL_PROFIT.
- Early SELL_PROFIT is only for clear deterioration, not caution.

Output:
Return exactly one JSON object only:
{"rationale":"max 15 words","decision":"HOLD"}

decision must be one of:
HOLD | SELL_PROFIT | SELL_LOSS"""


# Built-in multi-paragraph risk narratives when payload ``risk_blurb`` is empty (indexed 0–10).
_RISK_PROFILE_BLURBS: tuple[str, ...] = (
    """Ultra-low-risk capital-preservation trade. Expect muted oscillations and a grind toward TARGET rather than explosive bursts.

Normal noise includes shallow dips typically well under ~0.75%, tight sideways chop, single stalled ticks, and shallow fills — hesitation counts against resilience sooner than on higher risk tiers.

Do not treat micro-wiggles as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires unmistakable breakdown beyond normal micro-noise for this profile: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Very-low-risk steady drift trade. Moves should stay orderly; spikes matter only if they reverse cleanly.

Normal noise includes modest dips roughly ~0.5–1.25%, brief flats, single weak ticks, quick retests of tight levels, and modest profit-taking — deterioration spanning multiple ticks matters sooner than at medium risk.

Do not treat orderly digestion as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires clear failure judged strictly for this conservative profile: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Low-risk balanced swing trade. Expect controlled swings without reckless chop.

Normal noise includes ~0.75–1.5% pullbacks, sideways ranges, brief failed pushes, isolated red ticks, polite retests of support, and orderly profit-taking after ticks higher.

Do not treat polite digestion as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires clear structural failure beyond normal noise here across at least 15 ticks.""",
    """Moderate-low momentum trade. Expect occasional bursts followed by orderly consolidation.

Normal noise includes ~1–2% pullbacks, sideways chop, short-lived failed pushes, single soft ticks, predictable retests of breakout/support, and routine profit-taking after thrusts.

Do not treat routine volatility as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires clear failure: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Moderate-preparation momentum trade. Price may accelerate then pause without implying reversal.

Normal noise includes roughly ~1–2.5% pullbacks, uneven chop, brief failed pushes, isolated weak ticks, standard retests of support, and profit-taking after spikes.

Do not treat ordinary digestion as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires clear failure beyond normal noise for this profile across at least 15 ticks.""",
    """Medium-risk momentum trade. This setup may move sharply, pause, pull back, retest support, and continue. Normal noise includes 1-3% pullbacks, sideways chop, brief failed pushes, single red ticks, retests of breakout/support, and profit-taking after spikes.

Do not treat normal volatility as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare and requires clear failure: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Moderate-high momentum trade. Strength may arrive with wider swings and deeper breathers before continuation.

Normal noise includes roughly ~2–4% pullbacks, prolonged sideways chop, multiple failed pushes that reclaim quickly, clusters of soft ticks, messy retests of support, and sharper profit-taking after pops — none alone proves failure.

Do not treat energetic volatility as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be uncommon and requires unmistakable deterioration beyond what this elevated-but-disciplined profile tolerates: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Elevated-risk momentum trade. Expect violent bursts, deep breathers, shakeouts, and violent-looking tape that can still resolve higher.

Normal noise includes ~2.5–5% pullbacks, wide chop, repeated failed pushes that heal, strings of red ticks, aggressive retests of support, and violent profit-taking — demand sustained structural damage before conceding.

Do not treat adrenaline chop as failure by itself. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be uncommon and requires obvious breakdown beyond normal turbulence here: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """High-risk speculative momentum trade. Expect violent swings, extended digestion, and frequent scare ticks.

Normal noise includes ~3–6% pullbacks, extended sideways ranges, repeated fake breakdowns, clusters of weak ticks, violent retests, and heavy profit-taking after spikes.

Do not treat scary-but-contained chop as failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be uncommon and requires extreme clarity of failure — lower highs, lower lows or support break, stalling, and lost momentum sustained across at least 15 ticks.""",
    """Very-high-risk pursuit trade. Expect chaos-looking tape with frequent shakeouts while the thesis plays out.

Normal noise includes ~4–8% pullbacks, long chop zones, many failed pushes that reclaim, sustained strings of soft ticks that recover, brutal retests of support, and violent profit-taking cycles.

Do not treat disorderly tape as automatic failure. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare unless breakdown is undeniable even for this chaos profile: lower highs, lower lows or support break, stalling, and lost momentum across at least 15 ticks.""",
    """Maximum-risk hunt trade. Expect roller-coaster swings, violent chop, and prolonged scare phases while hunting oversized payoff.

Normal noise includes wide pullbacks often spanning several percent, prolonged sideways storms, repeated scare pushes, long stretches of weak ticks that rebound, harsh retests, and aggressive profit-taking — tolerate immense digestion until structure clearly rolls.

Do not treat violent volatility as failure absent structural rollover. HOLD is preferred while price is above crash_exit, ticks_below_invalidation < 30, and the last 15 ticks do not show sustained rollover.

Before TARGET, SELL_PROFIT should be rare even here — reserve it for catastrophic tape failure versus ordinary max-risk noise: lower highs, lower lows or support break, stalling, and lost momentum stretched across at least 15 ticks.""",
)


def _risk_blurb(risk: int) -> str:
    r = max(0, min(10, int(risk)))
    return _RISK_PROFILE_BLURBS[r]


def synthetic_price_path(
    entry: float,
    duration_s: float,
    n_steps: int,
    rng: random.Random,
) -> list[tuple[float, float, str]]:
    """Return ``(elapsed_sec, price, phase)`` for each step (simulated clock)."""
    if entry <= 0 or duration_s <= 0 or n_steps < 1:
        return [(float(duration_s), float(entry), "stable_chop")]

    t_stable = min(60.0, duration_s * 0.38)
    t_soft = min(120.0, duration_s * 0.58)

    out: list[tuple[float, float, str]] = []
    price = float(entry)
    for i in range(1, n_steps + 1):
        elapsed = (i / n_steps) * duration_s
        prev_elapsed = ((i - 1) / n_steps) * duration_s if i > 1 else 0.0
        mid_t = (prev_elapsed + elapsed) * 0.5

        if mid_t <= t_stable:
            step_pct = rng.uniform(-0.18, 0.18) / 100.0
            phase = "stable_chop"
        elif mid_t <= t_soft:
            step_pct = rng.uniform(-0.42, 0.06) / 100.0
            phase = "soft_down"
        else:
            step_pct = rng.uniform(-1.35, -0.08) / 100.0
            phase = "heavy_down"

        price = max(entry * 0.35, price * (1.0 + step_pct))
        out.append((elapsed, price, phase))
    return out


def _coerce_waypoints_map(payload: dict[str, Any]) -> dict[float, float]:
    """Parse ``path_waypoints`` from JSON: dict ``pct -> price`` or list of ``{pct, price}``."""
    raw = payload.get("path_waypoints")
    out: dict[float, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is None or v == "":
                continue
            try:
                pct = float(k)
                px = float(v)
            except (TypeError, ValueError):
                continue
            if 0.0 < pct < 100.0:
                out[pct] = px
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                pct = float(item.get("pct"))
                px = float(item.get("price"))
            except (TypeError, ValueError):
                continue
            if 0.0 < pct < 100.0:
                out[pct] = px
    return out


def _normalize_timeline_points(
    path_start: float,
    path_end: float,
    waypoints: dict[float, float],
) -> list[tuple[float, float]]:
    """Sorted timeline anchors: (percent_along_test_0_to_100, price)."""
    raw: list[tuple[float, float]] = [(0.0, float(path_start)), (100.0, float(path_end))]
    for pct, px in waypoints.items():
        pcf = float(pct)
        if 0.0 < pcf < 100.0:
            raw.append((pcf, float(px)))
    raw.sort(key=lambda t: t[0])
    merged: list[tuple[float, float]] = []
    for pct, px in raw:
        if merged and abs(merged[-1][0] - pct) < 1e-9:
            merged[-1] = (pct, px)
        else:
            merged.append((pct, px))
    return merged


def _price_at_timeline_pct(timeline_pct: float, pts: list[tuple[float, float]]) -> float:
    """Linear interpolate price at ``timeline_pct`` in [0, 100]."""
    t = max(0.0, min(100.0, float(timeline_pct)))
    if not pts:
        return 0.0
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i + 1]
        if t0 <= t <= t1:
            if abs(t1 - t0) < 1e-12:
                return p0
            u = (t - t0) / (t1 - t0)
            return p0 + u * (p1 - p0)
    return pts[-1][1]


def _phase_from_tick_delta(prev_price: float | None, price: float) -> str:
    """Classify last tape tick for ``phase_hint`` (controlled path)."""
    if prev_price is None or prev_price <= 0:
        return "stable_chop"
    d = (price - prev_price) / prev_price * 100.0
    if abs(d) < 0.025:
        return "stable_chop"
    if d >= 0.12:
        return "up_tick"
    if d <= -0.65:
        return "heavy_down"
    if d < 0:
        return "soft_down"
    return "stable_chop"


def controlled_price_path(
    *,
    duration_s: float,
    n_steps: int,
    path_start: float,
    path_end: float,
    waypoints: dict[float, float],
) -> list[tuple[float, float, str]]:
    """Piecewise-linear tape from time 0 (start price) through interior knots to time end (end price)."""
    pts = _normalize_timeline_points(path_start, path_end, waypoints)
    out: list[tuple[float, float, str]] = []
    prev_p: float | None = None
    for i in range(1, n_steps + 1):
        timeline_pct = (i / n_steps) * 100.0
        elapsed = (i / n_steps) * duration_s
        price = _price_at_timeline_pct(timeline_pct, pts)
        phase = _phase_from_tick_delta(prev_p, price)
        prev_p = price
        out.append((elapsed, price, phase))
    return out


def _phase_hint(phase: str) -> str:
    return {
        "stable_chop": "stable",
        "soft_down": "soft_down",
        "heavy_down": "heavy_down",
        "up_tick": "up_tick",
    }.get(
        phase,
        "unknown",
    )


def _dist_tp_pct(price: float, target: float) -> float:
    if price <= 0:
        return 0.0
    return (target - price) / price * 100.0


def _dist_stop_pct(price: float, stop: float) -> float:
    if price <= 0:
        return 0.0
    return (price - stop) / price * 100.0


def _pct_move_vs_prev(prev_price: float | None, price: float) -> float | None:
    """Absolute %-move vs previous tick; None if prev invalid."""
    if prev_price is None or prev_price <= 0:
        return None
    return abs((price - prev_price) / prev_price * 100.0)


def _skip_ai_stable_chop_quiet(
    *,
    tick_index: int,
    phase: str,
    prev_phase: str | None,
    price: float,
    prev_price: float | None,
    crash_exit: bool,
) -> bool:
    """Cost saver: flat chop + tiny tick-to-tick move → auto-HOLD, no API call."""
    if crash_exit:
        return False
    if tick_index <= 1:
        return False
    if phase != "stable_chop" or prev_phase != "stable_chop":
        return False
    mv = _pct_move_vs_prev(prev_price, price)
    if mv is None:
        return False
    return mv <= LAB_SKIP_AI_STABLE_CHOP_MOVE_PCT


def _build_lab_config(payload: dict[str, Any]) -> dict[str, Any]:
    entry = float(payload.get("entry_price") or 50.0)
    target = float(payload.get("target_price") or entry * 1.08)
    stop = float(payload.get("stop_loss") or entry * 0.94)
    duration_s = float(payload.get("duration_sec") or 180)
    poll_s = float(payload.get("poll_seconds") or 2.0)
    risk = int(payload.get("risk_level") or 5)
    ticker = (payload.get("ticker") or "DEMO_US").strip() or "DEMO_US"
    model = (payload.get("model") or DEFAULT_MODEL).strip()
    explicit_provider = (payload.get("provider") or "").strip()
    provider = _lab_infer_provider(model, explicit_provider or None)
    seed = payload.get("seed")
    rng = random.Random(int(seed) if seed is not None else random.randrange(1 << 30))

    duration_s = max(30.0, min(900.0, duration_s))
    poll_s = max(1.0, min(30.0, poll_s))
    max_ticks = int(payload.get("max_ticks") or 200)
    max_ticks = max(10, min(250, max_ticks))
    raw_steps = int(duration_s / poll_s)
    n_steps = min(max_ticks, max(1, raw_steps))
    if n_steps < 1:
        n_steps = 1

    back_story = (payload.get("back_story") or DEFAULT_BACK_STORY).strip()
    system_prompt_template = (payload.get("system_prompt") or "").strip()
    if not system_prompt_template:
        system_prompt_template = DEFAULT_FULL_SYSTEM_PROMPT_TEMPLATE

    use_thinking = bool(payload.get("minimal_thinking", True))
    if provider == "gemini" and use_thinking and not _model_supports_thinking_config(model):
        use_thinking = False
    max_out = int(payload.get("max_output_tokens") or 512)
    max_out = max(64, min(2048, max_out))

    openai_reasoning_effort = "minimal" if bool(payload.get("minimal_thinking", True)) else "low"

    path_mode = str(payload.get("path_mode") or "random").strip().lower()
    if path_mode not in ("random", "controlled"):
        path_mode = "random"

    ps = payload.get("path_start_price")
    pe = payload.get("path_end_price")
    path_start = float(ps) if ps is not None and str(ps).strip() != "" else float(entry)
    path_end = float(pe) if pe is not None and str(pe).strip() != "" else float(path_start)

    path_waypoints = _coerce_waypoints_map(payload)

    risk_blurb = (payload.get("risk_blurb") or "").strip()
    if not risk_blurb:
        risk_blurb = _risk_blurb(risk)

    cep = payload.get("crash_exit_price")
    stop_f = float(stop)
    crash_exit_price: float
    if cep is None or str(cep).strip() == "":
        crash_exit_price = round(stop_f * (1.0 - LAB_CRASH_DEFAULT_PCT_BELOW_STOP), 6)
    else:
        try:
            crash_exit_price = float(cep)
        except (TypeError, ValueError):
            crash_exit_price = round(stop_f * (1.0 - LAB_CRASH_DEFAULT_PCT_BELOW_STOP), 6)
    if crash_exit_price <= 0:
        crash_exit_price = round(stop_f * (1.0 - LAB_CRASH_DEFAULT_PCT_BELOW_STOP), 6)

    return {
        "entry": entry,
        "target": target,
        "stop": stop,
        "duration_s": duration_s,
        "poll_s": poll_s,
        "risk": risk,
        "risk_blurb": risk_blurb,
        "ticker": ticker,
        "model": model,
        "rng": rng,
        "n_steps": n_steps,
        "provider": provider,
        "openai_reasoning_effort": openai_reasoning_effort,
        "back_story": back_story,
        "system_prompt_template": system_prompt_template,
        "use_thinking": use_thinking,
        "max_out": max_out,
        "path_mode": path_mode,
        "path_start": path_start,
        "path_end": path_end,
        "path_waypoints": path_waypoints,
        "crash_exit_price": crash_exit_price,
    }


def _final_system_instruction(cfg: dict[str, Any]) -> str:
    """Apply placeholders then send this string as Gemini ``system_instruction`` / OpenAI ``instructions``."""
    risk = int(cfg["risk"])
    blurb = str(cfg.get("risk_blurb") or "").strip()
    if not blurb:
        blurb = _risk_blurb(risk)
    t = str(cfg.get("system_prompt_template") or DEFAULT_FULL_SYSTEM_PROMPT_TEMPLATE)
    entry = float(cfg["entry"])
    stop = float(cfg["stop"])
    target = float(cfg["target"])
    crash = float(cfg["crash_exit_price"])
    t = t.replace("{{RISK_BLURB}}", blurb)
    t = t.replace("{{RISK_LEVEL}}", str(risk))
    t = t.replace("{{ENTRY_PRICE}}", f"{entry:.4f}")
    t = t.replace("{{STOP_LOSS}}", f"{stop:.4f}")
    t = t.replace("{{TARGET_PRICE}}", f"{target:.4f}")
    t = t.replace("{{CRASH_EXIT_PRICE}}", f"{crash:.4f}")
    return t.strip()


def _trade_briefing_block(cfg: dict[str, Any]) -> str:
    """Static position context sent on every stateless tick (not the moving tape)."""
    tape_extra = ""
    if cfg.get("path_mode") == "controlled":
        nwp = len(cfg.get("path_waypoints") or {})
        tape_extra = (
            "\nSimulated tape mode: CONTROLLED. Path versus elapsed test time is piecewise-linear: "
            f"{cfg['path_start']:.4f} at the start, {cfg['path_end']:.4f} at the finish, "
            f"with {nwp} optional interior knot(s) on 5% steps from 5% through 95%.\n"
        )
    crash_px = float(cfg["crash_exit_price"])
    crash_line = (
        f"\nCrash-exit threshold: immediate exit if price <= {crash_px:.4f} USD on a tick (STATE.crash_exit).\n"
    )
    entry = float(cfg["entry"])
    target = float(cfg["target"])
    stop = float(cfg["stop"])
    if entry > 0:
        tp_pct = round((target / entry - 1.0) * 100.0, 4)
        sl_pct = round((stop / entry - 1.0) * 100.0, 4)
        tp_note = f" (~{tp_pct:+.4g}% vs entry)"
        sl_note = f" (~{sl_pct:+.4g}% vs entry)"
    else:
        tp_note = sl_note = ""
    return (
        f"POSITION (LONG) — {cfg['ticker']}\n\n"
        f"Operator / thesis:\n{cfg['back_story']}\n\n"
        "Trade plan (USD — authoritative levels for target vs stop):\n"
        f"- Entry (basis): {entry:.4f}\n"
        f"- Take-profit target: {target:.4f}{tp_note}\n"
        f"- Stop loss / invalidation: {stop:.4f}{sl_note}"
        f"{crash_line}{tape_extra}\n"
        f"Cadence: ~{cfg['poll_s']:.1f}s between ticks; up to {cfg['n_steps']} ticks over "
        f"~{cfg['duration_s']:.0f}s simulated.\n"
        "Each API request is stateless: you only see TRADE BRIEFING, STATE JSON, rolling tick window, and CURRENT TICK."
    ).strip()


def _initial_lab_state(cfg: dict[str, Any]) -> dict[str, Any]:
    crash_px = float(cfg["crash_exit_price"])
    return {
        "entry": float(cfg["entry"]),
        "stop": float(cfg["stop"]),
        "target": float(cfg["target"]),
        "crash_exit": False,
        "crash_exit_price": crash_px,
        "unrealized_pnl_pct": 0.0,
        "highest_price_seen": float(cfg["entry"]),
        "swing_count": 0,
        "consecutive_lower_highs": 0,
        "ticks_below_invalidation": 0,
        "volatility_regime": "normal",
        "position_status": "open",
        "last_signal": "HOLD",
        "last_reasoning": "",
        "trailing_stop_armed": False,
        "trailing_stop_hit": False,
    }


def _update_lab_state(
    state: dict[str, Any],
    cfg: dict[str, Any],
    *,
    price: float,
    prev_price: float | None,
    prev_prev_price: float | None,
    phase: str,
    prev_phase: str | None,
) -> None:
    entry = float(cfg["entry"])
    stop = float(cfg["stop"])
    if entry > 0:
        state["unrealized_pnl_pct"] = round((price / entry - 1.0) * 100.0, 4)
    else:
        state["unrealized_pnl_pct"] = 0.0

    h = float(state["highest_price_seen"])
    if price > h:
        state["highest_price_seen"] = price
    h = float(state["highest_price_seen"])

    if entry > 0:
        peak_gain_pct = (h / entry - 1.0) * 100.0
        if peak_gain_pct >= LAB_TRAILING_ARM_MIN_PEAK_GAIN_PCT:
            state["trailing_stop_armed"] = True
        if bool(state.get("trailing_stop_armed")) and not bool(state.get("trailing_stop_hit")):
            trig = h * (1.0 - LAB_TRAILING_PULLBACK_FROM_PEAK_PCT / 100.0)
            if price <= trig:
                state["trailing_stop_hit"] = True

    cep = state.get("crash_exit_price")
    if cep is not None and float(cep) > 0 and price <= float(cep):
        state["crash_exit"] = True

    if price < stop:
        state["ticks_below_invalidation"] = int(state["ticks_below_invalidation"]) + 1
    else:
        state["ticks_below_invalidation"] = 0

    if prev_phase is not None:
        if prev_phase == "up_tick" and phase in ("soft_down", "heavy_down"):
            state["swing_count"] = int(state["swing_count"]) + 1
        elif prev_phase in ("soft_down", "heavy_down") and phase == "up_tick":
            state["swing_count"] = int(state["swing_count"]) + 1

    if prev_price is not None and prev_prev_price is not None:
        if prev_price >= prev_prev_price and price < prev_price:
            state["consecutive_lower_highs"] = int(state["consecutive_lower_highs"]) + 1
        elif price > prev_price:
            state["consecutive_lower_highs"] = 0

    sc = int(state["swing_count"])
    state["volatility_regime"] = "elevated" if sc >= 2 else "normal"


def _price_message(
    cfg: dict[str, Any],
    *,
    tick_index: int,
    elapsed: float,
    price: float,
    prev_price: float | None,
    phase: str,
) -> str:
    pct = (price - cfg["entry"]) / cfg["entry"] * 100.0 if cfg["entry"] else 0.0
    move_tick = ""
    if prev_price is not None and prev_price > 0:
        d = (price - prev_price) / prev_price * 100.0
        move_tick = f" change_since_prior_tick_pct={d:+.4f}%"
    hint = _phase_hint(phase)
    return (
        f"PRICE_UPDATE tick={tick_index} elapsed_sec={elapsed:.2f} price_usd={price:.4f} "
        f"pct_vs_entry={pct:+.3f}% "
        f"distance_to_target_pct={_dist_tp_pct(price, cfg['target']):+.3f}% "
        f"cushion_above_stop_pct={_dist_stop_pct(price, cfg['stop']):+.3f}% "
        f"phase_hint={hint}{move_tick}"
    )


def _stateless_user_prompt(
    trade_briefing: str,
    state: dict[str, Any],
    rolling: list[str],
    current_line: str,
    *,
    prior_attempt_fragment: str | None = None,
    api_retry_after_errors: int = 0,
) -> str:
    roll_txt = "\n".join(rolling) if rolling else "(none yet — first tick)"
    st_json = json.dumps(state, indent=2, ensure_ascii=False)
    retry_note = ""
    if api_retry_after_errors > 0:
        retry_note = (
            f"=== API RETRY ===\nThis is attempt {api_retry_after_errors + 1} for the SAME simulated tick "
            "after a transient provider error. STATE and CURRENT TICK below are the authoritative latest "
            "tape; re-evaluate from them.\n\n"
        )
    prior_block = ""
    if prior_attempt_fragment:
        frag = prior_attempt_fragment.strip()
        if len(frag) > 12_000:
            frag = frag[:12_000] + "\n… [truncated]"
        prior_block = (
            "=== PRIOR OUTPUT THIS TICK (STREAM CUT OFF; OPTIONAL CONTINUITY) ===\n"
            f"{frag}\n\n"
            "If still consistent with STATE and CURRENT TICK, shorten rationale within the 15-word JSON cap; "
            "otherwise judge fresh. Output one JSON object only (POSITION CLOSED is the sole non-JSON exception). "
            "Server-side gates enforce crash exit, trailing + profit minimum, and valid SELL_LOSS streaks.\n\n"
        )
    return (
        f"{retry_note}"
        f"=== TRADE BRIEFING ===\n{trade_briefing}\n\n"
        f"=== STATE (JSON) — LATEST ===\n{st_json}\n\n"
        f"=== ROLLING LAST {ROLLING_TICKS} TICKS (oldest → newest; excludes current) ===\n{roll_txt}\n\n"
        f"{prior_block}"
        f"=== CURRENT TICK — LATEST ===\n{current_line}\n\n"
        "Respond per system rules: POSITION CLOSED only when applicable; otherwise exactly one JSON object "
        "with rationale (≤15 words) and decision. Apply the RISK PROFILE to separate noise from failure. "
        "Crash exit, trailing take-profit at +4%+, and invalid loss signals are corrected server-side if mismatched.\n"
    ).strip()


def _extract_chunk_text(chunk: Any) -> str:
    t = getattr(chunk, "text", None)
    if t:
        return str(t)
    return ""


def _usage_from_chunk(chunk: Any) -> dict[str, int] | None:
    """Best-effort token counts from a streamed :class:`GenerateContentResponse` chunk."""
    um = getattr(chunk, "usage_metadata", None)
    if um is None:
        return None
    try:
        pt = int(getattr(um, "prompt_token_count", None) or 0)
        ct = int(getattr(um, "candidates_token_count", None) or 0)
        tt = int(getattr(um, "total_token_count", None) or 0)
    except (TypeError, ValueError):
        return None
    if pt == 0 and ct == 0 and tt == 0:
        return None
    return {"prompt_tokens": pt, "candidates_tokens": ct, "total_tokens": tt}


# Own-line terminal tokens (contract: reply ends with HOLD / SELL_PROFIT / SELL_LOSS).
_TICK_GUARD = re.compile(
    r"^\s*(HOLD|SELL_PROFIT|SELL_LOSS)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_terminal_action(text: str) -> str | None:
    if not text:
        return None
    matches = list(_TICK_GUARD.finditer(text.strip()))
    if not matches:
        return None
    return matches[-1].group(1).upper()


def _reasoning_without_action_line(full: str) -> str:
    body = (full or "").strip()
    if not body:
        return ""
    lines = body.split("\n")
    last = lines[-1].strip()
    if _TICK_GUARD.match(last):
        return "\n".join(lines[:-1]).strip()
    return body


def _strip_markdown_json_fence(s: str) -> str:
    t = (s or "").strip()
    if not t.startswith("```"):
        return t
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _extract_lab_decision_json(text: str) -> dict[str, Any] | None:
    """Parse first JSON object in ``text``; return dict or None."""
    raw = _strip_markdown_json_fence((text or "").strip())
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i = raw.find("{")
    j = raw.rfind("}")
    if i < 0 or j <= i:
        return None
    frag = raw[i : j + 1]
    try:
        obj = json.loads(frag)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_lab_decision(
    decision: str,
    *,
    price: float,
    crash_exit_price: float,
    unrealized_pnl_pct: float,
    trailing_stop_hit: bool,
    ticks_below_invalidation: int,
) -> str:
    """Apply mechanical guardrails to an AI-proposed decision (OPEN positions only)."""
    d = (decision or "HOLD").strip().upper().replace(" ", "_")
    if d not in ("HOLD", "SELL_PROFIT", "SELL_LOSS"):
        d = "HOLD"

    cep = float(crash_exit_price or 0.0)
    if cep > 0 and price <= cep:
        return "SELL_LOSS"

    if trailing_stop_hit and unrealized_pnl_pct + 1e-9 >= LAB_MIN_PROFIT_SELL_PCT:
        return "SELL_PROFIT"

    if d == "SELL_PROFIT" and unrealized_pnl_pct + 1e-9 < LAB_MIN_PROFIT_SELL_PCT:
        return "HOLD"

    if d == "SELL_LOSS" and int(ticks_below_invalidation) < LAB_TICKS_BELOW_INVALIDATION_FOR_LOSS:
        return "HOLD"

    return d


def _lab_correction_rationale(
    from_decision: str,
    to_decision: str,
    *,
    price: float,
    crash_exit_price: float,
    unrealized_pnl_pct: float,
    trailing_stop_hit: bool,
    ticks_below_invalidation: int,
) -> str:
    """Short rationale when server overrides the model."""
    cep = float(crash_exit_price or 0.0)
    if to_decision == "SELL_PROFIT" and trailing_stop_hit and unrealized_pnl_pct + 1e-9 >= LAB_MIN_PROFIT_SELL_PCT:
        return "Trailing stop hit at +4%."
    if to_decision == "SELL_LOSS" and cep > 0 and price <= cep:
        return "Crash exit hit."
    if to_decision == "HOLD" and from_decision == "SELL_PROFIT":
        return "Below profit gate."
    if to_decision == "HOLD" and from_decision == "SELL_LOSS":
        return "Loss exit rules not met."
    return ""


def _finalize_lab_model_turn(
    raw_text: str,
    *,
    price: float,
    state: dict[str, Any],
) -> tuple[str, str]:
    """Normalize provider output to one JSON object and enforce guardrails."""
    crash_px = float(state.get("crash_exit_price") or 0.0)
    upnl = float(state.get("unrealized_pnl_pct") or 0.0)
    ticks_bi = int(state.get("ticks_below_invalidation") or 0)
    trailing = bool(state.get("trailing_stop_hit"))

    parsed = _parse_lab_action(raw_text)
    if parsed == "POSITION_CLOSED":
        base = "HOLD"
    elif parsed in ("HOLD", "SELL_PROFIT", "SELL_LOSS"):
        base = parsed
    else:
        base = "HOLD"

    validated = _validate_lab_decision(
        base,
        price=price,
        crash_exit_price=crash_px,
        unrealized_pnl_pct=upnl,
        trailing_stop_hit=trailing,
        ticks_below_invalidation=ticks_bi,
    )

    obj = _extract_lab_decision_json(raw_text)
    rationale = str((obj or {}).get("rationale") or "").strip()
    if validated != base:
        rationale = _lab_correction_rationale(
            base,
            validated,
            price=price,
            crash_exit_price=crash_px,
            unrealized_pnl_pct=upnl,
            trailing_stop_hit=trailing,
            ticks_below_invalidation=ticks_bi,
        )

    out = {"rationale": rationale, "decision": validated}
    return json.dumps(out, ensure_ascii=False), validated


def _try_lab_mechanical_decision(state: dict[str, Any], price: float) -> tuple[str, str] | None:
    """Hard exits evaluated before any model call. Returns (parsed_action, full_text) or None."""
    status = str(state.get("position_status") or "open").strip().lower()
    if status == "closed":
        return ("POSITION_CLOSED", "POSITION CLOSED")

    cep = float(state.get("crash_exit_price") or 0.0)
    if cep > 0 and price <= cep:
        txt = json.dumps({"rationale": "Crash exit hit.", "decision": "SELL_LOSS"}, ensure_ascii=False)
        return ("SELL_LOSS", txt)

    upnl = float(state.get("unrealized_pnl_pct") or 0.0)
    if bool(state.get("trailing_stop_hit")) and upnl + 1e-9 >= LAB_MIN_PROFIT_SELL_PCT:
        txt = json.dumps(
            {"rationale": "Trailing stop hit at +4%.", "decision": "SELL_PROFIT"},
            ensure_ascii=False,
        )
        return ("SELL_PROFIT", txt)

    return None


def _lab_mechanical_turn_events(
    label: str,
    full_text: str,
    parsed_action: str,
) -> Generator[dict[str, Any], None, None]:
    yield {"type": "turn_start", "label": label}
    yield {"type": "chunk", "label": label, "text": full_text}
    yield {
        "type": "turn_end",
        "label": label,
        "full_text": full_text,
        "parsed_action": parsed_action,
        "mechanical": True,
    }


def _parse_lab_action(text: str) -> str | None:
    """HOLD / SELL_PROFIT / SELL_LOSS / POSITION_CLOSED from JSON ``decision``, plain tokens, or legacy last-line."""
    raw = (text or "").strip()
    if not raw:
        return None
    if re.match(r"^\s*POSITION\s+CLOSED\s*$", raw, re.IGNORECASE):
        return "POSITION_CLOSED"
    obj = _extract_lab_decision_json(raw)
    if obj is not None:
        d_raw = str(obj.get("decision") or "").strip().upper().replace(" ", "_")
        if d_raw in ("HOLD", "SELL_PROFIT", "SELL_LOSS"):
            return d_raw
        if d_raw in ("POSITION_CLOSED", "POSITIONCLOSED"):
            return "POSITION_CLOSED"
    return _parse_terminal_action(raw)


def _lab_reasoning_body(full: str) -> str:
    """Strip terminal action / extract ``rationale`` for STATE.last_reasoning."""
    raw = (full or "").strip()
    if not raw:
        return ""
    if re.match(r"^\s*POSITION\s+CLOSED\s*$", raw, re.IGNORECASE):
        return ""
    if re.match(r"^\s*SELL_PROFIT\s*$", raw, re.IGNORECASE):
        return ""
    obj = _extract_lab_decision_json(raw)
    if obj is not None:
        return str(obj.get("rationale") or "").strip()
    return _reasoning_without_action_line(raw)


def run_lab_events(
    payload: dict[str, Any],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Generator[dict[str, Any], None, None]:
    """Yield structured events (dicts) for one full simulated session."""
    cfg = _build_lab_config(payload)
    if cfg["path_mode"] == "controlled":
        path = controlled_price_path(
            duration_s=cfg["duration_s"],
            n_steps=cfg["n_steps"],
            path_start=cfg["path_start"],
            path_end=cfg["path_end"],
            waypoints=cfg["path_waypoints"],
        )
    else:
        path = synthetic_price_path(cfg["entry"], cfg["duration_s"], cfg["n_steps"], cfg["rng"])

    oa_key_ok = False
    try:
        from . import config as _cfg

        oa_key_ok = bool(_cfg.openai_api_key())
    except Exception:
        pass

    yield {
        "type": "config",
        "provider": cfg["provider"],
        "model": cfg["model"],
        "n_steps": cfg["n_steps"],
        "duration_s": cfg["duration_s"],
        "poll_s": cfg["poll_s"],
        "entry": cfg["entry"],
        "path_mode": cfg["path_mode"],
        "path_start": cfg["path_start"],
        "path_end": cfg["path_end"],
        "path_interior_points": len(cfg["path_waypoints"]),
        "thinking_enabled": bool(cfg["use_thinking"]),
        "system_instruction_chars": len(_final_system_instruction(cfg)),
        "api_mode": "stateless",
        "rolling_window_ticks": ROLLING_TICKS,
        "skip_ai_stable_chop_move_pct": LAB_SKIP_AI_STABLE_CHOP_MOVE_PCT,
        "openai_api_key_configured": oa_key_ok,
    }

    session_out_sum = 0
    session_prompt_sum = 0
    session_total_sum = 0
    last_prompt = 0
    generations = 0
    session_cost_usd = 0.0
    session_cost_note = ""

    trade_briefing = _trade_briefing_block(cfg)
    state = _initial_lab_state(cfg)
    rolling: list[str] = []
    prev_price: float | None = None
    prev_prev_price: float | None = None
    prev_phase: str | None = None

    last_model_text = ""
    last_action: str | None = None

    if cfg["provider"] == "openai":
        from openai import OpenAI

        oa_key = ""
        try:
            from . import config as _cfg_oa

            oa_key = _cfg_oa.openai_api_key()
        except Exception:
            pass
        if not oa_key:
            yield {"type": "error", "message": "OPENAI_API_KEY missing in environment"}
            return

        _, _, session_cost_note = _openai_lab_price_rates(cfg["model"])
        oa_client = OpenAI(api_key=oa_key, timeout=180.0)

        def stream_stateless_turn(
            label: str,
            trade_briefing: str,
            state: dict[str, Any],
            rolling: list[str],
            current_line: str,
            tick_price: float,
        ) -> Generator[dict[str, Any], None, None]:
            nonlocal session_out_sum, session_prompt_sum, session_total_sum, last_prompt, generations
            nonlocal last_model_text, last_action, session_cost_usd

            yield {"type": "turn_start", "label": label}
            transient_tries = 0
            prior_fragment: str | None = None
            use_reasoning_kw = True

            while True:
                user_text = _stateless_user_prompt(
                    trade_briefing,
                    state,
                    rolling,
                    current_line,
                    prior_attempt_fragment=prior_fragment,
                    api_retry_after_errors=transient_tries,
                )
                acc = ""
                last_usage: dict[str, int] | None = None

                # gpt-5-nano and several Responses models reject ``temperature`` (fixed sampler).
                create_kwargs: dict[str, Any] = {
                    "model": cfg["model"],
                    "instructions": _final_system_instruction(cfg),
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": user_text}],
                        }
                    ],
                    "text": {"format": {"type": "text"}, "verbosity": "medium"},
                    "tools": [],
                    "store": False,
                    "stream": True,
                    "max_output_tokens": cfg["max_out"],
                }
                if use_reasoning_kw:
                    create_kwargs["reasoning"] = {
                        "effort": str(cfg.get("openai_reasoning_effort") or "minimal"),
                        "summary": "concise",
                    }

                stream_it = None
                try:
                    stream_it = oa_client.responses.create(**create_kwargs)
                except Exception as exc:
                    if use_reasoning_kw and "reasoning" in create_kwargs:
                        yield {
                            "type": "note",
                            "message": f"OpenAI retry without reasoning block: {exc}",
                        }
                        use_reasoning_kw = False
                        create_kwargs.pop("reasoning", None)
                        try:
                            stream_it = oa_client.responses.create(**create_kwargs)
                        except Exception as exc2:
                            exc = exc2
                        else:
                            exc = None
                    if exc is not None:
                        if _is_transient_openai_error(exc) and transient_tries < LAB_TRANSIENT_MAX_RETRIES:
                            transient_tries += 1
                            delay = min(
                                LAB_TRANSIENT_BASE_DELAY_S * (2 ** (transient_tries - 1)),
                                LAB_TRANSIENT_MAX_DELAY_S,
                            )
                            yield {
                                "type": "note",
                                "message": f"Transient OpenAI error ({exc}); retry {transient_tries}/"
                                f"{LAB_TRANSIENT_MAX_RETRIES} after {delay:.1f}s",
                            }
                            sleep_fn(delay)
                            continue
                        yield {"type": "error", "message": f"openai responses.create failed: {exc}"}
                        return

                try:
                    assert stream_it is not None
                    for event in stream_it:
                        et = getattr(event, "type", "") or ""
                        if et == "response.output_text.delta":
                            delta = getattr(event, "delta", "") or ""
                            if delta:
                                acc += delta
                                yield {"type": "chunk", "label": label, "text": delta}
                        elif et == "response.completed":
                            resp = getattr(event, "response", None)
                            u_obj = getattr(resp, "usage", None) if resp is not None else None
                            if u_obj is not None:
                                it = int(getattr(u_obj, "input_tokens", None) or 0)
                                ot = int(getattr(u_obj, "output_tokens", None) or 0)
                                tt = int(getattr(u_obj, "total_tokens", None) or 0)
                                if tt <= 0 and (it > 0 or ot > 0):
                                    tt = it + ot
                                last_usage = {
                                    "prompt_tokens": it,
                                    "candidates_tokens": ot,
                                    "total_tokens": tt,
                                }
                        elif et == "response.failed":
                            resp = getattr(event, "response", None)
                            err = getattr(resp, "error", None) if resp is not None else None
                            msg = str(err) if err else "response.failed"
                            yield {"type": "error", "message": f"openai {msg}"}
                            return
                        elif et == "error":
                            msg = getattr(event, "message", str(event))
                            yield {"type": "error", "message": f"openai stream error: {msg}"}
                            return
                except Exception as exc:
                    if _is_transient_openai_error(exc) and transient_tries < LAB_TRANSIENT_MAX_RETRIES:
                        transient_tries += 1
                        if acc.strip():
                            prior_fragment = acc.strip()
                        delay = min(
                            LAB_TRANSIENT_BASE_DELAY_S * (2 ** (transient_tries - 1)),
                            LAB_TRANSIENT_MAX_DELAY_S,
                        )
                        yield {
                            "type": "note",
                            "message": f"Transient OpenAI stream error ({exc}); retry {transient_tries}/"
                            f"{LAB_TRANSIENT_MAX_RETRIES} after {delay:.1f}s",
                        }
                        sleep_fn(delay)
                        continue
                    yield {"type": "error", "message": f"openai stream error: {exc}"}
                    return
                break

            acc_final, action = _finalize_lab_model_turn(acc, price=tick_price, state=state)
            last_model_text = acc_final
            last_action = action
            turn_end = {
                "type": "turn_end",
                "label": label,
                "full_text": acc_final,
                "parsed_action": action,
            }
            if last_usage:
                turn_end["usage"] = last_usage
                c_turn = _openai_lab_turn_cost_usd(cfg["model"], last_usage)
                session_cost_usd += c_turn
                turn_end["cost_usd_turn_estimate"] = round(c_turn, 8)
            yield turn_end
            if last_usage:
                u = last_usage
                pt = int(u.get("prompt_tokens", 0))
                ct = int(u.get("candidates_tokens", 0))
                tt = int(u.get("total_tokens", 0))
                if tt <= 0 and (pt > 0 or ct > 0):
                    tt = pt + ct
                session_prompt_sum += pt
                session_out_sum += ct
                session_total_sum += tt
                last_prompt = pt
                generations += 1
                yield {
                    "type": "session_usage",
                    "turns": generations,
                    "prompt_tokens_sum": session_prompt_sum,
                    "output_tokens_sum": session_out_sum,
                    "total_tokens_sum": session_total_sum,
                    "last_prompt_tokens": last_prompt,
                    "last_turn_total_tokens": tt,
                    "session_cost_usd_sum_estimate": round(session_cost_usd, 8),
                }
    else:
        from google import genai
        from google.genai import types

        api_key = ""
        try:
            from . import config as _cfg

            api_key = _cfg.gemini_api_key()
        except Exception:
            pass
        if not api_key:
            yield {"type": "error", "message": "GEMINI_API_KEY missing in environment"}
            return

        thinking_cfg: types.ThinkingConfig | None
        if cfg["use_thinking"]:
            thinking_cfg = types.ThinkingConfig(thinking_level="MINIMAL")
        else:
            thinking_cfg = None

        def build_gen_cfg(with_thinking: bool) -> types.GenerateContentConfig:
            kwargs: dict[str, Any] = {
                "system_instruction": _final_system_instruction(cfg),
                "temperature": 0.15,
                "max_output_tokens": cfg["max_out"],
            }
            if with_thinking and thinking_cfg is not None:
                kwargs["thinking_config"] = thinking_cfg
            return types.GenerateContentConfig(**kwargs)

        thinking_effective = bool(cfg["use_thinking"] and thinking_cfg is not None)
        gen_cfg = build_gen_cfg(thinking_effective)

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=180_000))

        def stream_stateless_turn(
            label: str,
            trade_briefing: str,
            state: dict[str, Any],
            rolling: list[str],
            current_line: str,
            tick_price: float,
        ) -> Generator[dict[str, Any], None, None]:
            nonlocal gen_cfg, thinking_effective, session_out_sum, session_prompt_sum, session_total_sum, last_prompt, generations
            nonlocal last_model_text, last_action

            yield {"type": "turn_start", "label": label}
            transient_tries = 0
            prior_fragment: str | None = None

            while True:
                user_text = _stateless_user_prompt(
                    trade_briefing,
                    state,
                    rolling,
                    current_line,
                    prior_attempt_fragment=prior_fragment,
                    api_retry_after_errors=transient_tries,
                )
                acc = ""
                last_usage: dict[str, int] | None = None
                stream_it = None
                try:
                    stream_it = client.models.generate_content_stream(
                        model=cfg["model"],
                        contents=user_text,
                        config=gen_cfg,
                    )
                except Exception as exc:
                    if thinking_effective and not _is_transient_api_error(exc):
                        thinking_effective = False
                        gen_cfg = build_gen_cfg(False)
                        yield {
                            "type": "note",
                            "message": f"Retrying without thinking_config: {exc}",
                        }
                        continue
                    if _is_transient_api_error(exc) and transient_tries < LAB_TRANSIENT_MAX_RETRIES:
                        transient_tries += 1
                        delay = min(
                            LAB_TRANSIENT_BASE_DELAY_S * (2 ** (transient_tries - 1)),
                            LAB_TRANSIENT_MAX_DELAY_S,
                        )
                        yield {
                            "type": "note",
                            "message": f"Transient API error on connect ({exc}); retry {transient_tries}/"
                            f"{LAB_TRANSIENT_MAX_RETRIES} after {delay:.1f}s (rebuilding prompt with latest STATE)",
                        }
                        sleep_fn(delay)
                        continue
                    yield {"type": "error", "message": f"generate_content_stream failed: {exc}"}
                    return
                try:
                    assert stream_it is not None
                    for chunk in stream_it:
                        u = _usage_from_chunk(chunk)
                        if u is not None:
                            last_usage = u
                        piece = _extract_chunk_text(chunk)
                        if piece:
                            acc += piece
                            yield {"type": "chunk", "label": label, "text": piece}
                except Exception as exc:
                    if _is_transient_api_error(exc) and transient_tries < LAB_TRANSIENT_MAX_RETRIES:
                        transient_tries += 1
                        if acc.strip():
                            prior_fragment = acc.strip()
                        delay = min(
                            LAB_TRANSIENT_BASE_DELAY_S * (2 ** (transient_tries - 1)),
                            LAB_TRANSIENT_MAX_DELAY_S,
                        )
                        yield {
                            "type": "note",
                            "message": f"Transient API error during stream ({exc}); retry {transient_tries}/"
                            f"{LAB_TRANSIENT_MAX_RETRIES} after {delay:.1f}s (prompt refreshed; partial output preserved for model)",
                        }
                        sleep_fn(delay)
                        continue
                    yield {"type": "error", "message": f"stream error: {exc}"}
                    return
                break

            acc_final, action = _finalize_lab_model_turn(acc, price=tick_price, state=state)
            last_model_text = acc_final
            last_action = action
            turn_end: dict[str, Any] = {
                "type": "turn_end",
                "label": label,
                "full_text": acc_final,
                "parsed_action": action,
            }
            if last_usage:
                turn_end["usage"] = last_usage
            yield turn_end
            if last_usage:
                u = last_usage
                pt = int(u.get("prompt_tokens", 0))
                ct = int(u.get("candidates_tokens", 0))
                tt = int(u.get("total_tokens", 0))
                if tt <= 0 and (pt > 0 or ct > 0):
                    tt = pt + ct
                session_prompt_sum += pt
                session_out_sum += ct
                session_total_sum += tt
                last_prompt = pt
                generations += 1
                yield {
                    "type": "session_usage",
                    "turns": generations,
                    "prompt_tokens_sum": session_prompt_sum,
                    "output_tokens_sum": session_out_sum,
                    "total_tokens_sum": session_total_sum,
                    "last_prompt_tokens": last_prompt,
                    "last_turn_total_tokens": tt,
                }

    for i, row in enumerate(path, start=1):
        elapsed, price, phase = row
        if str(state.get("position_status")) != "open":
            break
        if i > 1:
            sleep_fn(cfg["poll_s"])

        _update_lab_state(
            state,
            cfg,
            price=price,
            prev_price=prev_price,
            prev_prev_price=prev_prev_price,
            phase=phase,
            prev_phase=prev_phase,
        )
        current_line = _price_message(
            cfg,
            tick_index=i,
            elapsed=elapsed,
            price=price,
            prev_price=prev_price,
            phase=phase,
        )

        pct_vs_prev = _pct_move_vs_prev(prev_price, price)
        skip_ai = _skip_ai_stable_chop_quiet(
            tick_index=i,
            phase=phase,
            prev_phase=prev_phase,
            price=price,
            prev_price=prev_price,
            crash_exit=bool(state.get("crash_exit")),
        )

        mech = _try_lab_mechanical_decision(state, price)
        if mech is not None:
            mech_action, mech_text = mech
            last_model_text = mech_text
            last_action = mech_action
            yield {"type": "tick", "index": i, "elapsed": elapsed, "price": price, "phase": phase}
            for ev in _lab_mechanical_turn_events(f"tick_{i}", mech_text, mech_action):
                yield ev
        elif skip_ai:
            last_action = "HOLD"
            last_model_text = '{"rationale":"","decision":"HOLD"}'
            yield {
                "type": "skip_ai",
                "label": f"tick_{i}",
                "tick_index": i,
                "elapsed": elapsed,
                "price": price,
                "reason": "stable_chop_quiet",
                "phase": phase,
                "prev_phase": prev_phase,
                "pct_move_vs_prev": round(pct_vs_prev, 6) if pct_vs_prev is not None else None,
                "threshold_pct": LAB_SKIP_AI_STABLE_CHOP_MOVE_PCT,
                "auto_action": "HOLD",
                "message": "Price stable — no AI needed",
            }
        else:
            yield {"type": "tick", "index": i, "elapsed": elapsed, "price": price, "phase": phase}
            stream_failed = False
            for ev in stream_stateless_turn(
                f"tick_{i}", trade_briefing, state, rolling, current_line, tick_price=price
            ):
                yield ev
                if ev.get("type") == "error":
                    stream_failed = True
                    break
            if stream_failed:
                return

        state["last_reasoning"] = _lab_reasoning_body(last_model_text)
        if last_action:
            state["last_signal"] = last_action
        if last_action in ("SELL_PROFIT", "SELL_LOSS"):
            state["position_status"] = "closed"

        rolling.append(current_line)
        if len(rolling) > ROLLING_TICKS:
            rolling.pop(0)

        prev_prev_price = prev_price
        prev_price = price
        prev_phase = phase

        if str(state.get("position_status")) != "open":
            break

    done_ev: dict[str, Any] = {
        "type": "done",
        "generations": generations,
        "message_pairs": generations,
        "session_prompt_tokens_sum": session_prompt_sum,
        "session_output_tokens_sum": session_out_sum,
        "session_total_tokens_sum": session_total_sum,
        "session_last_prompt_tokens": last_prompt,
        "provider": cfg["provider"],
    }
    if cfg["provider"] == "openai":
        inp_r, out_r, pc_note = _openai_lab_price_rates(cfg["model"])
        done_ev["session_cost_usd_estimate"] = round(session_cost_usd, 8)
        done_ev["session_cost_input_usd_per_mtok"] = inp_r
        done_ev["session_cost_output_usd_per_mtok"] = out_r
        done_ev["session_cost_pricing_note"] = session_cost_note or pc_note
    yield done_ev


def iter_sse_lines(payload: dict[str, Any]) -> Iterable[str]:
    """Serialize ``run_lab_events`` as SSE ``data:`` lines."""
    for ev in run_lab_events(payload):
        yield f"data: {json.dumps(ev)}\n\n"


def main() -> None:
    """Run default synthetic path + stream to stdout (no SSE framing)."""
    payload = {
        "duration_sec": 90,
        "poll_seconds": 2,
        "entry_price": 100.0,
        "target_price": 108.0,
        "stop_loss": 94.0,
        "risk_level": 4,
        "seed": 42,
        "max_ticks": 35,
    }
    print("--- reasoning_stream_lab (stdout JSON lines) ---", flush=True)
    for ev in run_lab_events(payload):
        print(json.dumps(ev, ensure_ascii=False)[:500], flush=True)


if __name__ == "__main__":
    main()
