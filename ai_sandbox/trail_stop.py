"""Trailing stop ladder for position manager (New System; hard stop stays -10%)."""

from __future__ import annotations

from typing import Optional


def get_trail_pct(gain_pct: float) -> Optional[float]:
    if gain_pct < 7.5:
        return None
    if gain_pct < 10:
        return 7.5
    if gain_pct < 40:
        return 10.0
    if gain_pct < 60:
        return 15.0
    if gain_pct < 100:
        return 20.0
    if gain_pct < 150:
        return 25.0
    if gain_pct < 200:
        return 30.0
    if gain_pct < 300:
        return 35.0
    return 40.0


def calculate_stop(
    entry_price: float,
    highest_price: float,
    gain_pct: float,
    *,
    hard_stop_pct: float = 10.0,
) -> tuple[float, bool, Optional[float]]:
    """Return (stop_level, trail_active, trail_pct)."""
    hard_stop = round(entry_price * (1.0 - hard_stop_pct / 100.0), 6)
    trail_pct = get_trail_pct(gain_pct)
    if trail_pct is None:
        return hard_stop, False, None
    trail_stop = round(highest_price * (1.0 - trail_pct / 100.0), 6)
    stop = max(hard_stop, trail_stop)
    return stop, True, trail_pct
