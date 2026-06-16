"""Compute trade-path analytics from 1-second tick samples."""

from __future__ import annotations

import math
from typing import Any


def _downsample_series(
    series: list[dict[str, float]],
    *,
    max_points: int = 480,
) -> list[dict[str, float]]:
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    out: list[dict[str, float]] = []
    i = 0.0
    while int(i) < len(series):
        out.append(series[int(i)])
        i += step
    if out[-1] != series[-1]:
        out.append(series[-1])
    return out


def compute_trade_analytics(
    ticks: list[dict[str, Any]],
    *,
    exit_pct: float | None = None,
    capital_gbp: float | None = None,
    max_series_points: int = 480,
) -> dict[str, Any]:
    """Summarise unrealised % path for the info panel."""
    if not ticks:
        return {"tick_count": 0}

    cap = float(capital_gbp) if capital_gbp not in (None, "") else None

    def _tick_gbp(t: dict[str, Any], pct: float | None) -> float | None:
        raw = t.get("unreal_gbp")
        if raw is not None:
            return float(raw)
        if cap is not None and pct is not None:
            return round(cap * float(pct) / 100.0, 2)
        return None

    usable: list[tuple[float, float, float, float | None]] = []
    for t in ticks:
        pct = t.get("unreal_pct")
        if pct is None:
            continue
        elapsed = float(t.get("elapsed_sec") or 0.0)
        ts = float(t.get("ts") or 0.0)
        pct_f = float(pct)
        usable.append((elapsed, pct_f, ts, _tick_gbp(t, pct_f)))

    if not usable:
        return {"tick_count": len(ticks)}

    elapsed = [u[0] for u in usable]
    pcts = [u[1] for u in usable]
    timestamps = [u[2] for u in usable]
    gbps = [u[3] for u in usable]

    max_gain_idx = max(range(len(pcts)), key=pcts.__getitem__)
    max_loss_idx = min(range(len(pcts)), key=pcts.__getitem__)

    avg = sum(pcts) / len(pcts)
    duration = elapsed[-1] - elapsed[0] if len(elapsed) > 1 else 0.0

    profit_ticks = sum(1 for p in pcts if p > 0)
    underwater_ticks = sum(1 for p in pcts if p < 0)
    flat_ticks = len(pcts) - profit_ticks - underwater_ticks

    peak = pcts[0]
    max_dd = 0.0
    for p in pcts:
        if p > peak:
            peak = p
        dd = peak - p
        if dd > max_dd:
            max_dd = dd

    final_pct = float(exit_pct) if exit_pct is not None else pcts[-1]
    max_gain = pcts[max_gain_idx]
    exit_efficiency: float | None = None
    if max_gain > 0:
        exit_efficiency = final_pct / max_gain * 100.0

    mean = avg
    volatility = math.sqrt(sum((p - mean) ** 2 for p in pcts) / len(pcts))

    first_green = next((elapsed[i] for i, p in enumerate(pcts) if p > 0), None)
    first_red = next((elapsed[i] for i, p in enumerate(pcts) if p < 0), None)

    series = [
        {
            "t": round(elapsed[i], 1),
            "p": round(pcts[i], 4),
            "ts": timestamps[i] if timestamps[i] > 0 else None,
        }
        for i in range(len(pcts))
    ]

    return {
        "tick_count": len(ticks),
        "sample_count": len(pcts),
        "duration_sec": round(duration, 1),
        "max_gain_pct": round(max_gain, 4),
        "max_gain_sec": round(elapsed[max_gain_idx], 1),
        "max_gain_gbp": round(gbps[max_gain_idx], 2) if gbps[max_gain_idx] is not None else None,
        "max_loss_pct": round(pcts[max_loss_idx], 4),
        "max_loss_sec": round(elapsed[max_loss_idx], 1),
        "max_loss_gbp": round(gbps[max_loss_idx], 2) if gbps[max_loss_idx] is not None else None,
        "average_pct": round(avg, 4),
        "time_in_profit_pct": round(profit_ticks / len(pcts) * 100.0, 1),
        "time_underwater_pct": round(underwater_ticks / len(pcts) * 100.0, 1),
        "time_flat_pct": round(flat_ticks / len(pcts) * 100.0, 1),
        "max_drawdown_from_peak_pct": round(max_dd, 4),
        "exit_efficiency_pct": round(exit_efficiency, 1) if exit_efficiency is not None else None,
        "volatility_pct": round(volatility, 4),
        "time_to_first_green_sec": round(first_green, 1) if first_green is not None else None,
        "time_to_first_red_sec": round(first_red, 1) if first_red is not None else None,
        "range_pct": round(max_gain - pcts[max_loss_idx], 4),
        "final_pct": round(final_pct, 4),
        "series": _downsample_series(series, max_points=max_series_points),
    }
