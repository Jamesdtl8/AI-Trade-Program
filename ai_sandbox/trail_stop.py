"""Trailing stop ladder for position manager (New System; hard stop from config).

Trail arms permanently on first +7.5% peak gain — it does not turn off if price
pulls back below +7.5%. Tier width is keyed off peak gain, not current P&L.

Tiers (peak gain from entry → trail % below peak):
  +7.5% to +10%   → 5% trail  (min lock-in: ~2.1% from entry)
  +10%  to +20%   → 7% trail  (min lock-in: ~2.3% at 10% peak)
  +20%  to +40%   → 10% trail
  +40%  to +60%   → 15% trail
  +60%  to +100%  → 20% trail
  +100% to +150%  → 25% trail
  +150% to +200%  → 30% trail
  +200% to +300%  → 35% trail
  +300% and above → 40% trail

TIER-BOUNDARY CONTINUITY:
Each tier boundary is chosen so the trail stop NEVER drops when peak crosses
into a new tier. The minimum stop at the bottom of each tier always equals or
exceeds the stop at the top of the previous tier.

At exactly +10% peak:
  Previous tier (5% trail): stop = 10% peak × 0.95 = entry × 1.0 × 1.10 × 0.95 = entry × 1.045
  New tier     (7% trail):  stop = 10% peak × 0.93 = entry × 1.10 × 0.93 = entry × 1.023
  → PROBLEM: stop dropped. We fix this by never letting the computed stop fall
    below the running highest_stop seen so far (tracked in position_monitor).

Hard stop: entry × (1 - hard_stop_pct/100). Always active, provides an absolute floor.
"""

from __future__ import annotations

from typing import Optional

TRAIL_ARM_PCT = 7.5


def peak_gain_pct(entry_price: float, highest_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return (float(highest_price) - float(entry_price)) / float(entry_price) * 100.0


def get_trail_pct(peak_gain_pct: float) -> Optional[float]:
    """Trail width (%) from peak gain achieved (not current P&L).

    Tiers are tuned so a 7.5%+ winner rides as long as possible while
    protecting locked-in gains. Wider early tier (5% vs old 3%) keeps us
    in 8-15% moves without exiting too early.
    """
    if peak_gain_pct < TRAIL_ARM_PCT:
        return None
    if peak_gain_pct < 10:
        return 5.0   # was 3% — widened so we catch 8-15% moves without haircut
    if peak_gain_pct < 20:
        return 7.0   # was 5%
    if peak_gain_pct < 40:
        return 10.0
    if peak_gain_pct < 60:
        return 15.0
    if peak_gain_pct < 100:
        return 20.0
    if peak_gain_pct < 150:
        return 25.0
    if peak_gain_pct < 200:
        return 30.0
    if peak_gain_pct < 300:
        return 35.0
    return 40.0


def calculate_stop(
    entry_price: float,
    highest_price: float,
    current_gain_pct: float,
    *,
    hard_stop_pct: float = 15.0,
    highest_stop: float = 0.0,
) -> tuple[float, bool, Optional[float]]:
    """Return (stop_level, trail_active, trail_pct).

    ``current_gain_pct`` is unused for arming/tier (kept for API compat).
    ``highest_stop`` is the highest stop level seen so far — the returned stop
    is never less than this, preventing tier-boundary backward steps where a
    wider trail on a new tier would otherwise push the stop down.

    Trail arms once peak gain reaches +7.5% and stays armed for the trade.
    """
    del current_gain_pct  # peak-driven; monitor still passes live P&L for display
    hard_stop = round(entry_price * (1.0 - hard_stop_pct / 100.0), 6)
    peak_gain = peak_gain_pct(entry_price, highest_price)
    trail_pct = get_trail_pct(peak_gain)
    if trail_pct is None:
        return hard_stop, False, None
    trail_stop_raw = round(highest_price * (1.0 - trail_pct / 100.0), 6)
    # Stop can never decrease: take the max of new calc, prior highest stop, and hard floor.
    stop = max(hard_stop, trail_stop_raw, float(highest_stop))
    return stop, True, trail_pct
