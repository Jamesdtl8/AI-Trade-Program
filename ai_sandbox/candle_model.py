"""Candle exit state machine (scanner alert model).

Live: enter at scanner alert price (market buy).
Initial stop: 1-minute candle **close** at/below E×0.90 (unprotected only).
Emergency: 1s tick market sell if loss reaches 18%.
Ramping trail: 15s tick-bar avg from T212 polls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config, ramping_trail as rt

STATE_WAITING = "waiting_for_entry"
STATE_UNPROTECTED = "unprotected"
STATE_RAMPING = "ramping"
# Legacy aliases (resume old DB rows)
STATE_PROTECTED = "protected"
STATE_RUNNER = "runner"
STATE_CLOSED = "closed"
STATE_NOT_FILLED = "not_filled"

OUTCOME_NOT_FILLED = "not_filled"
OUTCOME_STOP_BEFORE_TRIGGER = "stopped_before_trigger"
OUTCOME_RAMPING_TRAIL = "ramping_trail_exit"
OUTCOME_INTRABAR_STOP = "intrabar_stop_exit"
OUTCOME_INTRABAR_RAMP = "intrabar_ramp_exit"
OUTCOME_EMERGENCY_STOP = "emergency_stop_18pct"
OUTCOME_STALE_QUOTE = "stale_quote_halt_exit"
OUTCOME_PROTECTED_CLOSE = "protected_profit_exit"
OUTCOME_RUNNER_TRAIL = "runner_trail_exit"
OUTCOME_UNTRIGGERED_EOD = "unprotected_end_day"
OUTCOME_RAMPING_EOD = "ramping_end_day"
OUTCOME_PROTECTED_EOD = "protected_end_day"
OUTCOME_RUNNER_EOD = "runner_end_day"


@dataclass
class CandleLevels:
    alert_price: float
    entry_trigger: float
    entry_price: float | None = None
    initial_stop: float | None = None
    peak_gain_pct: float | None = None
    ratcheted_peak_pct: float | None = None
    trail_width_pp: float | None = None
    trail_floor_gain_pct: float | None = None
    trail_floor_price: float | None = None
    # Legacy fields (hydration only)
    first_trigger: float | None = None
    protected_floor: float | None = None
    runner_trigger: float | None = None
    highest_runner_close: float | None = None
    runner_trail_exit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_price": self.alert_price,
            "entry_trigger": self.entry_trigger,
            "entry_price": self.entry_price,
            "initial_stop": self.initial_stop,
            "peak_gain_pct": self.peak_gain_pct,
            "ratcheted_peak_pct": self.ratcheted_peak_pct,
            "trail_width_pp": self.trail_width_pp,
            "trail_floor_gain_pct": self.trail_floor_gain_pct,
            "trail_floor_price": self.trail_floor_price,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> CandleLevels:
        d = d or {}
        return cls(
            alert_price=float(d.get("alert_price") or 0),
            entry_trigger=float(d.get("entry_trigger") or 0),
            entry_price=_opt_float(d.get("entry_price")),
            initial_stop=_opt_float(d.get("initial_stop")),
            peak_gain_pct=_opt_float(d.get("peak_gain_pct")),
            ratcheted_peak_pct=_opt_float(d.get("ratcheted_peak_pct")),
            trail_width_pp=_opt_float(d.get("trail_width_pp")),
            trail_floor_gain_pct=_opt_float(d.get("trail_floor_gain_pct")),
            trail_floor_price=_opt_float(d.get("trail_floor_price")),
            first_trigger=_opt_float(d.get("first_trigger")),
            protected_floor=_opt_float(d.get("protected_floor")),
            runner_trigger=_opt_float(d.get("runner_trigger")),
            highest_runner_close=_opt_float(d.get("highest_runner_close")),
            runner_trail_exit=_opt_float(d.get("runner_trail_exit")),
        )


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def entry_trigger_price(alert_price: float, *, gap_pct: float) -> float:
    return round(float(alert_price) * (1.0 + float(gap_pct) / 100.0), 6)


def levels_after_entry(entry_price: float, alert_price: float, *, gap_pct: float) -> CandleLevels:
    e = float(entry_price)
    a = float(alert_price)
    return CandleLevels(
        alert_price=a,
        entry_trigger=entry_trigger_price(a, gap_pct=gap_pct),
        entry_price=e,
        initial_stop=config.candle_initial_stop_price(e),
    )


def _sync_ramp_from_peak(lv: CandleLevels, *, close_gain_pct: float) -> None:
    peak = round(max(float(lv.peak_gain_pct or 0.0), float(close_gain_pct)), 2)
    lv.peak_gain_pct = peak
    ratchet = rt.ratchet_level_for_peak(peak)
    if ratchet <= 0 or not lv.entry_price:
        return
    lv.ratcheted_peak_pct = ratchet
    w = rt.trail_width_pp(ratchet)
    floor_g = rt.trail_floor_gain_pct(ratchet)
    lv.trail_width_pp = w
    lv.trail_floor_gain_pct = floor_g
    lv.trail_floor_price = rt.trail_floor_price(float(lv.entry_price), floor_g)


def _migrate_legacy_ramp(lv: CandleLevels) -> None:
    """Map old protected/runner price levels into ramp fields on resume."""
    if lv.ratcheted_peak_pct and lv.trail_floor_gain_pct is not None:
        return
    entry = float(lv.entry_price or 0)
    if entry <= 0:
        return
    if lv.highest_runner_close and lv.highest_runner_close > entry:
        peak = unrealized_pct(entry, float(lv.highest_runner_close))
    elif lv.peak_gain_pct:
        peak = float(lv.peak_gain_pct)
    else:
        return
    _sync_ramp_from_peak(lv, close_gain_pct=peak)


def _effective_state(state: str) -> str:
    if state in (STATE_PROTECTED, STATE_RUNNER):
        return STATE_RAMPING
    return state


@dataclass
class CandleStepResult:
    state: str
    levels: CandleLevels
    action: str | None = None
    outcome: str | None = None
    exit_close: float | None = None
    entry_close: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "levels": self.levels.to_dict(),
            "action": self.action,
            "outcome": self.outcome,
            "exit_close": self.exit_close,
            "entry_close": self.entry_close,
        }


@dataclass
class CandleTradeState:
    state: str = STATE_WAITING
    levels: CandleLevels = field(default_factory=lambda: CandleLevels(0.0, 0.0))
    outcome: str | None = None

    @classmethod
    def waiting(cls, alert_price: float, *, gap_pct: float) -> CandleTradeState:
        a = float(alert_price)
        return cls(
            state=STATE_WAITING,
            levels=CandleLevels(
                alert_price=a,
                entry_trigger=entry_trigger_price(a, gap_pct=gap_pct),
            ),
        )


def process_emergency_stop_tick(
    trade: CandleTradeState,
    *,
    price: float,
) -> CandleStepResult:
    """1s tick market sell if unrealised loss hits emergency threshold (e.g. −18%)."""
    px = float(price)
    lv = trade.levels
    st = _effective_state(trade.state)
    if st not in (STATE_UNPROTECTED, STATE_RAMPING):
        return CandleStepResult(state=trade.state, levels=lv)
    entry = float(lv.entry_price or 0)
    if entry <= 0 or px <= 0:
        return CandleStepResult(state=trade.state, levels=lv)
    emerg = config.candle_emergency_stop_pct()
    if unrealized_pct(entry, px) <= -emerg:
        return CandleStepResult(
            state=STATE_CLOSED,
            levels=lv,
            action="exit",
            outcome=OUTCOME_EMERGENCY_STOP,
            exit_close=px,
        )
    return CandleStepResult(state=trade.state, levels=lv)


def process_stop_candle_1m(
    trade: CandleTradeState,
    *,
    close: float,
) -> CandleStepResult:
    """1m candle close at/below initial stop (unprotected only). No tick stop at −10%."""
    c = float(close)
    lv = trade.levels
    st = _effective_state(trade.state)
    if st != STATE_UNPROTECTED:
        return CandleStepResult(state=trade.state, levels=lv)
    assert lv.initial_stop is not None
    if c <= float(lv.initial_stop):
        return CandleStepResult(
            state=STATE_CLOSED,
            levels=lv,
            action="exit",
            outcome=OUTCOME_STOP_BEFORE_TRIGGER,
            exit_close=c,
        )
    return CandleStepResult(state=trade.state, levels=lv)


def process_stop_bar_30s(
    trade: CandleTradeState,
    *,
    close: float,
    avg: float,
) -> CandleStepResult:
    """Deprecated alias — stop is 1m close only (avg ignored)."""
    del avg
    return process_stop_candle_1m(trade, close=close)


def process_profit_tick(
    trade: CandleTradeState,
    *,
    price: float,
    gap_pct: float,
) -> CandleStepResult:
    """Arm ratchet and exit on a single broker tick (1s poll). Stops stay on 1m close."""
    px = float(price)
    return process_profit_bar(trade, close=px, avg=px, gap_pct=gap_pct)


def process_profit_bar(
    trade: CandleTradeState,
    *,
    close: float,
    avg: float,
    gap_pct: float,
) -> CandleStepResult:
    """Profit sample: arm ramp at +7.5%; ratchet on close; exit when avg <= floor (1s tick or bar avg)."""
    c = float(close)
    a = float(avg)
    lv = trade.levels
    st = _effective_state(trade.state)
    entry = float(lv.entry_price or 0)

    if st == STATE_WAITING:
        if c >= lv.entry_trigger:
            new_lv = levels_after_entry(c, lv.alert_price, gap_pct=gap_pct)
            return CandleStepResult(
                state=STATE_UNPROTECTED,
                levels=new_lv,
                action="enter",
                entry_close=c,
            )
        return CandleStepResult(state=trade.state, levels=lv)

    if entry <= 0:
        return CandleStepResult(state=trade.state, levels=lv)

    avg_gain = unrealized_pct(entry, a)
    close_gain = unrealized_pct(entry, c)

    if st == STATE_UNPROTECTED:
        if avg_gain >= rt.ARM_GAIN_PCT:
            _sync_ramp_from_peak(lv, close_gain_pct=max(close_gain, avg_gain))
            return CandleStepResult(state=STATE_RAMPING, levels=lv)
        return CandleStepResult(state=trade.state, levels=lv)

    if st == STATE_RAMPING:
        _migrate_legacy_ramp(lv)
        _sync_ramp_from_peak(lv, close_gain_pct=close_gain)
        floor_g = lv.trail_floor_gain_pct
        if floor_g is not None and avg_gain <= float(floor_g):
            return CandleStepResult(
                state=STATE_CLOSED,
                levels=lv,
                action="exit",
                outcome=OUTCOME_RAMPING_TRAIL,
                exit_close=c,
            )
        return CandleStepResult(state=STATE_RAMPING, levels=lv)

    return CandleStepResult(state=trade.state, levels=lv)


def process_candle_close(
    trade: CandleTradeState,
    close: float,
    *,
    gap_pct: float,
) -> CandleStepResult:
    stop_step = process_stop_candle_1m(trade, close=close)
    if stop_step.action == "exit":
        return stop_step
    return process_profit_bar(trade, close=close, avg=close, gap_pct=gap_pct)


# Back-compat alias
process_profit_bar_15s = process_profit_bar


def end_of_day_close(trade: CandleTradeState, final_close: float) -> CandleStepResult:
    c = float(final_close)
    st = _effective_state(trade.state)
    lv = trade.levels

    if st == STATE_WAITING:
        return CandleStepResult(
            state=STATE_NOT_FILLED,
            levels=lv,
            outcome=OUTCOME_NOT_FILLED,
        )

    outcome_map = {
        STATE_UNPROTECTED: OUTCOME_UNTRIGGERED_EOD,
        STATE_RAMPING: OUTCOME_RAMPING_EOD,
    }
    outcome = outcome_map.get(st, OUTCOME_UNTRIGGERED_EOD)
    return CandleStepResult(
        state=STATE_CLOSED,
        levels=lv,
        action="exit",
        outcome=outcome,
        exit_close=c,
    )


def unrealized_pct(entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    return (float(price) - float(entry)) / float(entry) * 100.0


def check_intrabar_exit(
    trade: CandleTradeState,
    *,
    price: float,
) -> CandleStepResult | None:
    """Optional tick exit for ramp floor only (stop always uses 30s bar avg+close)."""
    from . import config as _cfg

    if not _cfg.candle_intrabar_ramp_exit_enabled():
        return None
    px = float(price)
    lv = trade.levels
    entry = float(lv.entry_price or 0)
    if entry <= 0 or px <= 0:
        return None
    st = _effective_state(trade.state)

    if st == STATE_RAMPING:
        _migrate_legacy_ramp(lv)
        floor_g = lv.trail_floor_gain_pct
        if floor_g is not None and unrealized_pct(entry, px) <= float(floor_g):
            return CandleStepResult(
                state=STATE_CLOSED,
                levels=lv,
                action="exit",
                outcome=OUTCOME_INTRABAR_RAMP,
                exit_close=px,
            )
    return None
