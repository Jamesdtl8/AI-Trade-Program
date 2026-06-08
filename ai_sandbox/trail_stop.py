"""Trailing stop ladder for position manager (New System; hard stop from config).

Early profit-lock tiers (peak gain from entry → minimum locked gain before exit):
  +8.5% reached  → exit if price falls back to +5%
  +10% reached  → exit if price falls back to +6%
  +15% reached  → exit if price falls back to +8%

Above +15% peak, the %-below-peak trail ladder arms (same tiers as before).

Trail tiers (peak gain from entry → trail % below peak):
  +15% to +25%    → 10% trail
  +25% to +50%    → 13% trail
  +50% to +60%    → 15% trail
  +60% to +100%   → 20% trail
  +100% to +150%  → 25% trail
  +150% to +200%  → 30% trail
  +200% to +300%  → 35% trail
  +300% and above → 40% trail

The effective stop is the highest of hard stop, profit-lock floor, and trail stop.
TIER-BOUNDARY CONTINUITY: ``highest_stop`` in position_monitor ensures the stop
level only ever ratchets upward.

Hard stop: entry × (1 - hard_stop_pct/100). Always active, provides an absolute floor.

Runner giveback cap (position_monitor, once peak gain ≥ config.PEAK_GIVEBACK_ARM_PCT):
  Exit if unrealized P&L falls more than PEAK_GIVEBACK_CAP_PCT percentage points below
  peak gain (e.g. peak +80% with 25pp cap → floor +55%). Replaces the trail ladder on
  mega-moves so violent reversals lock in more of the run.
"""

from __future__ import annotations

from typing import Optional

# Peak gain % required before each profit-lock floor applies (highest matching tier wins).
PROFIT_LOCK_TIERS: tuple[tuple[float, float], ...] = (
    (8.5, 5.0),
    (10.0, 6.0),
    (15.0, 8.0),
)

TRAIL_ARM_PCT = 15.0


def peak_gain_pct(entry_price: float, highest_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return (float(highest_price) - float(entry_price)) / float(entry_price) * 100.0


def get_profit_lock_floor_pct(peak_gain_pct: float) -> Optional[float]:
    """Minimum unrealized gain % to hold once peak has reached each lock tier."""
    floor: float | None = None
    for arm_peak, lock_floor in PROFIT_LOCK_TIERS:
        if peak_gain_pct >= arm_peak:
            floor = lock_floor
    return floor


def profit_lock_stop_price(entry_price: float, peak_gain_pct: float) -> Optional[float]:
    """Absolute stop price from profit-lock tiers, or None if not armed."""
    floor = get_profit_lock_floor_pct(peak_gain_pct)
    if floor is None or entry_price <= 0:
        return None
    return round(float(entry_price) * (1.0 + float(floor) / 100.0), 6)


def get_trail_pct(peak_gain_pct: float) -> Optional[float]:
    """Trail width (%) from peak gain achieved (not current P&L).

    Arms at +15% peak; below that only profit-lock tiers apply.
    """
    if peak_gain_pct < TRAIL_ARM_PCT:
        return None
    if peak_gain_pct < 25:
        return 10.0
    if peak_gain_pct < 50:
        return 13.0
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


def giveback_floor_gain_pct(
    peak_gain_pct: float,
    *,
    cap_pp: float | None = None,
    arm_pct: float | None = None,
) -> float | None:
    """Minimum unrealized gain % while runner giveback cap is armed, or None if not armed."""
    from . import config

    arm = float(arm_pct if arm_pct is not None else config.PEAK_GIVEBACK_ARM_PCT)
    cap = float(cap_pp if cap_pp is not None else config.PEAK_GIVEBACK_CAP_PCT)
    if peak_gain_pct < arm:
        return None
    return float(peak_gain_pct) - cap


def giveback_cap_breached(
    unreal_gain_pct: float,
    peak_gain_pct: float,
    *,
    cap_pp: float | None = None,
    arm_pct: float | None = None,
) -> bool:
    """True when a runner has given back more than ``cap_pp`` from its peak gain."""
    from . import config

    cap = float(cap_pp if cap_pp is not None else config.PEAK_GIVEBACK_CAP_PCT)
    arm = float(arm_pct if arm_pct is not None else config.PEAK_GIVEBACK_ARM_PCT)
    floor_gain = giveback_floor_gain_pct(peak_gain_pct, cap_pp=cap)
    if floor_gain is None:
        return False
    return float(unreal_gain_pct) < floor_gain


def calculate_stop(
    entry_price: float,
    highest_price: float,
    current_gain_pct: float,
    *,
    hard_stop_pct: float = 10.0,
    highest_stop: float = 0.0,
) -> tuple[float, bool, Optional[float]]:
    """Return (stop_level, protective_active, trail_pct).

    ``current_gain_pct`` is unused for arming/tier (kept for API compat).
    ``highest_stop`` is the highest stop level seen so far — the returned stop
    is never less than this, preventing tier-boundary backward steps.

    Protective stop arms on first +8.5% peak (profit lock). Trail ladder arms at +15%.
    """
    del current_gain_pct  # peak-driven; monitor still passes live P&L for display
    hard_stop = round(entry_price * (1.0 - hard_stop_pct / 100.0), 6)
    peak_gain = peak_gain_pct(entry_price, highest_price)
    profit_lock = profit_lock_stop_price(entry_price, peak_gain)
    trail_pct = get_trail_pct(peak_gain)
    trail_stop_raw = 0.0
    if trail_pct is not None:
        trail_stop_raw = round(highest_price * (1.0 - trail_pct / 100.0), 6)

    if profit_lock is None and trail_pct is None:
        return hard_stop, False, None

    candidates = [hard_stop, float(highest_stop)]
    if profit_lock is not None:
        candidates.append(profit_lock)
    if trail_stop_raw > 0:
        candidates.append(trail_stop_raw)
    stop = max(candidates)
    return stop, True, trail_pct
