"""Trailing stop ladder for position manager (New System; hard stop from config).

Trail arms permanently on first +10% peak gain — it does not turn off if price
pulls back below +10%. Tier width is keyed off peak gain, not current P&L.

Tiers (peak gain from entry → trail % below peak):
  +10% to +25%    → 10% trail (arms at break-even; wider early tier for runners)
  +25% to +50%    → 13% trail
  +50% to +60%    → 15% trail
  +60% to +100%   → 20% trail
  +100% to +150%  → 25% trail
  +150% to +200%  → 30% trail
  +200% to +300%  → 35% trail
  +300% and above → 40% trail

TIER-BOUNDARY CONTINUITY:
Tier boundaries can cause the raw trail stop to step DOWN when crossing into a
wider tier. This is prevented by the running highest_stop tracked in
position_monitor — the returned stop is never less than the prior highest stop,
so the stop level only ever ratchets upward.

Hard stop: entry × (1 - hard_stop_pct/100). Always active, provides an absolute floor.
"""

from __future__ import annotations

from typing import Optional

TRAIL_ARM_PCT = 10.0


def peak_gain_pct(entry_price: float, highest_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return (float(highest_price) - float(entry_price)) / float(entry_price) * 100.0


def get_trail_pct(peak_gain_pct: float) -> Optional[float]:
    """Trail width (%) from peak gain achieved (not current P&L).

    Tiers are widened vs prior version to give genuine runners more room to
    breathe before locking them out. Trail only arms at +10% so we don't trail
    too early on moves that haven't confirmed direction.
    """
    if peak_gain_pct < TRAIL_ARM_PCT:
        return None
    if peak_gain_pct < 25:
        return 10.0  # was 5% (7.5-10%) + 7% (10-20%); wider so runners don't stop on first dip
    if peak_gain_pct < 50:
        return 13.0  # was 10% (20-40%); wider to hold through mid-move consolidation
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

    Trail arms once peak gain reaches +10% and stays armed for the trade.
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
