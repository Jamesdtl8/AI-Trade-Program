"""Candle-close exit state machine (scanner alert model).

Live: enter at scanner alert price (market buy). Exits use yfinance 1m candle
*closes* only — not intraminute highs. Backtest mode can wait for A×1.03 close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


STATE_WAITING = "waiting_for_entry"
STATE_UNPROTECTED = "unprotected"
STATE_PROTECTED = "protected"
STATE_RUNNER = "runner"
STATE_CLOSED = "closed"
STATE_NOT_FILLED = "not_filled"

OUTCOME_NOT_FILLED = "not_filled"
OUTCOME_STOP_BEFORE_TRIGGER = "staged_stop_before_trigger"
OUTCOME_PROTECTED_CLOSE = "staged_protected_close"
OUTCOME_RUNNER_TRAIL = "staged_runner_trail_close"
OUTCOME_UNTRIGGERED_EOD = "staged_untriggered_end_day"
OUTCOME_PROTECTED_EOD = "staged_protected_end_day"
OUTCOME_RUNNER_EOD = "staged_runner_end_day"


@dataclass
class CandleLevels:
    alert_price: float
    entry_trigger: float
    entry_price: float | None = None
    initial_stop: float | None = None
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
            "first_trigger": self.first_trigger,
            "protected_floor": self.protected_floor,
            "runner_trigger": self.runner_trigger,
            "highest_runner_close": self.highest_runner_close,
            "runner_trail_exit": self.runner_trail_exit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> CandleLevels:
        d = d or {}
        return cls(
            alert_price=float(d.get("alert_price") or 0),
            entry_trigger=float(d.get("entry_trigger") or 0),
            entry_price=_opt_float(d.get("entry_price")),
            initial_stop=_opt_float(d.get("initial_stop")),
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
        initial_stop=round(e * 0.90, 6),
        first_trigger=round(e * 1.10, 6),
        protected_floor=round(e * 1.075, 6),
        runner_trigger=round(e * 1.25, 6),
    )


@dataclass
class CandleStepResult:
    state: str
    levels: CandleLevels
    action: str | None = None  # enter | exit | None
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


def process_candle_close(
    trade: CandleTradeState,
    close: float,
    *,
    gap_pct: float,
) -> CandleStepResult:
    """Advance state machine on a confirmed 1m candle close."""
    c = float(close)
    lv = trade.levels
    st = trade.state

    if st == STATE_WAITING:
        if c >= lv.entry_trigger:
            new_lv = levels_after_entry(c, lv.alert_price, gap_pct=gap_pct)
            return CandleStepResult(
                state=STATE_UNPROTECTED,
                levels=new_lv,
                action="enter",
                entry_close=c,
            )
        return CandleStepResult(state=st, levels=lv)

    if st == STATE_UNPROTECTED:
        assert lv.initial_stop is not None and lv.first_trigger is not None
        if c <= lv.initial_stop:
            return CandleStepResult(
                state=STATE_CLOSED,
                levels=lv,
                action="exit",
                outcome=OUTCOME_STOP_BEFORE_TRIGGER,
                exit_close=c,
            )
        if c >= lv.first_trigger:
            return CandleStepResult(state=STATE_PROTECTED, levels=lv)
        return CandleStepResult(state=st, levels=lv)

    if st == STATE_PROTECTED:
        assert lv.protected_floor is not None and lv.runner_trigger is not None
        if c <= lv.protected_floor:
            return CandleStepResult(
                state=STATE_CLOSED,
                levels=lv,
                action="exit",
                outcome=OUTCOME_PROTECTED_CLOSE,
                exit_close=c,
            )
        if c >= lv.runner_trigger:
            lv.highest_runner_close = c
            lv.runner_trail_exit = round(c * 0.85, 6)
            return CandleStepResult(state=STATE_RUNNER, levels=lv)
        return CandleStepResult(state=st, levels=lv)

    if st == STATE_RUNNER:
        peak = float(lv.highest_runner_close or c)
        if c > peak:
            peak = c
        lv.highest_runner_close = peak
        lv.runner_trail_exit = round(peak * 0.85, 6)
        if c <= lv.runner_trail_exit:
            return CandleStepResult(
                state=STATE_CLOSED,
                levels=lv,
                action="exit",
                outcome=OUTCOME_RUNNER_TRAIL,
                exit_close=c,
            )
        return CandleStepResult(state=st, levels=lv)

    return CandleStepResult(state=st, levels=lv)


def end_of_day_close(trade: CandleTradeState, final_close: float) -> CandleStepResult:
    """Force exit at final same-day 1m candle close (no overnight hold)."""
    c = float(final_close)
    st = trade.state
    lv = trade.levels

    if st == STATE_WAITING:
        return CandleStepResult(
            state=STATE_NOT_FILLED,
            levels=lv,
            outcome=OUTCOME_NOT_FILLED,
        )

    outcome_map = {
        STATE_UNPROTECTED: OUTCOME_UNTRIGGERED_EOD,
        STATE_PROTECTED: OUTCOME_PROTECTED_EOD,
        STATE_RUNNER: OUTCOME_RUNNER_EOD,
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
