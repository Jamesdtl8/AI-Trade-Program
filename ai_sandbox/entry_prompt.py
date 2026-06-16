"""AI decision prompt template from trading_model_rules_and_prompting.md §3.1."""

from __future__ import annotations

from typing import Any

PROMPT_TEMPLATE = """You are reviewing a live scanner alert for a momentum trading model.

Your job is to return a final trade decision using only the rules below.
Do not use future price action.
Do not guess.
Do not override hard reject rules.
Do not approve a trade unless one exact approved setup matches.

Return one of:
- TRADE_NOW
- WATCH_ONLY
- SKIP

Alert data:
Ticker: {ticker}
Alert date: {date}
Alert time: {time}
Alert number: {alert_no}
Alert label: {label}
Alert price: {price}
Prior same-day alert price: {prior_price}
Change %: {change_pct}
Float FT: {ft}
Market cap MC: {mc}
Relative volume RV: {rv}
1-minute volume: {one_minute_volume}
NEWS: {has_news}
Reverse split: {has_reverse_split}
IPO: {has_ipo}
Zero borrow: {has_zero_borrow}
Reg SHO: {has_reg_sho}
Known runner: {has_known_runner}
Potential squeeze: {has_potential_squeeze}

Step 1 - Apply hard rejects:
Reject if alert number is 1.
Reject if no prior same-day alert exists.
Reject if current alert price <= prior same-day alert price.
Reject if FT > 50,000,000.
Reject if MC > 100,000,000.
Reject if price < 0.10.
Reject if RV is missing or RV < 5.
Reject if reverse split is present.
Reject if label is REV V or BTT V.
Reject if label is MOMENTUM and change % > 80.

Step 2 - Grade size:
PASS_STRONG if FT < 5,000,000 or MC < 10,000,000.
PASS if FT <= 20,000,000 or MC <= 30,000,000.
PARTIAL if FT <= 50,000,000 or MC <= 100,000,000.
FAIL otherwise.

Step 3 - Grade structure:
PASS_STRONG if at least two of zero borrow, Reg SHO, potential squeeze are true.
PASS if exactly one of zero borrow, Reg SHO, potential squeeze is true.
PARTIAL if known runner is true.
FAIL otherwise.

Step 4 - Confirm tape:
Tape is confirmed if current alert price > prior same-day alert price and either the label is continuation/momentum or RV >= 50.
Continuation labels are MOMENTUM, BREAKOUT, NBREAK, HUGE, HUGE S, or labels starting with MOMENTUM.

Step 5 - Check approved setups:
Setup A alert2_extreme_tape_confirmed:
- alert number = 2
- RV >= 500
- current price > prior price
- NEWS or zero borrow or Reg SHO is true
- change % is missing or <= 80

Setup B clean_catalyst_trade:
- alert number >= 3
- size is PASS_STRONG or PASS
- NEWS is true
- tape is confirmed
- RV >= 50

Setup C squeeze_momentum_trade:
- alert number >= 3
- size is PASS_STRONG or PASS
- structure is PASS_STRONG or PASS
- tape is confirmed
- RV >= 50

Setup D pure_tape_trade:
- alert number >= 3
- size is PASS_STRONG
- tape is confirmed
- RV >= 200

Setup E extreme_tape_trade:
- alert number >= 3
- RV >= 500
- current price > prior price

Step 6 - Final decision:
If any hard reject is true, return SKIP.
If one approved setup matches, return TRADE_NOW.
If no hard reject is true but no approved setup matches, return WATCH_ONLY.

Output format:
Decision: TRADE_NOW / WATCH_ONLY / SKIP
Matched setup: setup name or none
Reject reason: exact hard reject reason or none
Size grade:
Structure grade:
Tape confirmed: yes/no
Entry action if TRADE_NOW: market buy after alert
Entry price: actual broker market fill
Initial stop after fill: fill x 0.90 (10% max loss on 1m candle close; emergency −18% on tick)
First trigger after fill: fill x 1.10
Protected floor after fill: fill x 1.075
Runner trigger after fill: fill x 1.25
Runner trail after runner trigger: highest runner 1m close x 0.85
Notes: one short sentence only
"""


def build_prompt(ctx: dict[str, Any], *, date: str = "", time: str = "") -> str:
    """Format §3.1 prompt from :func:`entry_rules.build_context` output."""
    def _yn(v: Any) -> str:
        return "yes" if v else "no"

    return PROMPT_TEMPLATE.format(
        ticker=ctx.get("ticker") or "",
        date=date,
        time=time,
        alert_no=ctx.get("alert_number") or "",
        label=ctx.get("label") or "",
        price=ctx.get("price") or "",
        prior_price=ctx.get("prior_price") or "none",
        change_pct=ctx.get("change_pct") if ctx.get("change_pct") is not None else "missing",
        ft=ctx.get("float") if ctx.get("float") is not None else "missing",
        mc=ctx.get("market_cap") if ctx.get("market_cap") is not None else "missing",
        rv=ctx.get("rv") if ctx.get("rv") is not None else "missing",
        one_minute_volume=ctx.get("volume_1v") if ctx.get("volume_1v") is not None else "missing",
        has_news=_yn(ctx.get("has_news")),
        has_reverse_split=_yn(ctx.get("has_reverse_split")),
        has_ipo=_yn(ctx.get("has_ipo")),
        has_zero_borrow=_yn(ctx.get("zero_borrow")),
        has_reg_sho=_yn(ctx.get("reg_sho")),
        has_known_runner=_yn(ctx.get("known_runner")),
        has_potential_squeeze=_yn(ctx.get("potential_squeeze")),
    )
