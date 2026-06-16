"""Ramping gain-% trail (1s tick or bar avg validation in candle_model)."""

from __future__ import annotations

ARM_GAIN_PCT = 7.5
RATCHET_STEP_BELOW_20 = 2.0
RATCHET_STEP_FROM_20 = 5.0
RATCHET_SWITCH_PCT = 20.0

TRAIL_WIDTH_BELOW_20 = 5.0
TRAIL_WIDTH_AT_20 = 7.5
TRAIL_WIDTH_AT_30 = 10.0
TRAIL_WIDTH_AT_40 = 15.0


def trail_width_pp(ratcheted_peak_pct: float) -> float:
    """Trail width in percentage points below the ratcheted peak gain."""
    p = float(ratcheted_peak_pct)
    if p >= 40:
        return TRAIL_WIDTH_AT_40
    if p >= 30:
        return TRAIL_WIDTH_AT_30
    if p >= 20:
        return TRAIL_WIDTH_AT_20
    return TRAIL_WIDTH_BELOW_20


def ratchet_level_for_peak(peak_gain_pct: float) -> float:
    """Snap peak gain to the ratchet grid (0 = not armed)."""
    peak = float(peak_gain_pct)
    if peak < ARM_GAIN_PCT:
        return 0.0
    if peak < RATCHET_SWITCH_PCT:
        n = int((peak - ARM_GAIN_PCT) // RATCHET_STEP_BELOW_20)
        return round(ARM_GAIN_PCT + RATCHET_STEP_BELOW_20 * n, 4)
    n = int((peak - RATCHET_SWITCH_PCT) // RATCHET_STEP_FROM_20)
    return round(RATCHET_SWITCH_PCT + RATCHET_STEP_FROM_20 * n, 4)


def trail_floor_gain_pct(ratcheted_peak_pct: float) -> float:
    w = trail_width_pp(ratcheted_peak_pct)
    return round(float(ratcheted_peak_pct) - w, 4)


def trail_floor_price(entry_price: float, floor_gain_pct: float) -> float:
    e = float(entry_price)
    return round(e * (1.0 + float(floor_gain_pct) / 100.0), 6)
