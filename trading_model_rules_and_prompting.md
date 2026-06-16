# Scanner Alert Trading System - Final Rules and AI Prompting

This document is the clean operating version of the model. It is split into three sections:

1. Hard entry rules and exact matching rules to allow a trade.
2. Exit, take-profit, stop, and trail rules using 1-minute candles.
3. AI prompting format that must return the final trade decision.

Important update: entry is now treated as a market buy after an approved alert. The 1-minute candle logic is used for exits, take profit, protection, and trailing management.

The model is not financial advice. It is a rule set built from scanner alerts and recent 1-minute candle modelling. Live fills, spread, liquidity, halts, and broker execution can change results.

## 1. Hard Entry Rules

A trade is only allowed when the scanner alert passes all hard filters and then matches one of the approved trade setups.

The alert itself creates the possible entry. If the alert is approved, the action is:

```text
Approved alert = market buy after the alert.
Actual entry price E = real market fill price.
Stake = fixed base stake, normally £10,000 unless forward-testing smaller.
```

There is no candle-close confirmation needed for entry in this version. The candle system starts after the market buy has filled.

### 1.1 Required Alert Context

For every alert, record these fields before making a decision:

```text
Ticker:
Alert date:
Alert time:
Alert number:
Alert label:
Alert price:
Previous alert price for same ticker/day:
Change %:
Float FT:
Market cap MC:
Relative volume RV:
1-minute volume:
NEWS flag:
Reverse split flag:
IPO flag:
Zero borrow flag:
Reg SHO flag:
Known runner flag:
Potential squeeze flag:
```

A decision cannot be made if the prior same-day alert is missing and the alert number is above 1. The model compares the current alert to the prior alert for the same ticker on the same day.

### 1.2 Hard Reject Rules

Reject immediately if any of these are true:

```text
Alert number = 1
No prior same-day alert exists
Current alert price <= prior same-day alert price
Float FT > 50,000,000
Market cap MC > 100,000,000
Alert price < 0.10
RV is missing
RV < 5
Reverse split flag is present
Alert label is REV V
Alert label is BTT V
Alert label is MOMENTUM and change % > 80
```

These are hard rejects. The AI should not override them.

### 1.3 Approved Label / Momentum Match

The alert must show continuation, strong tape, or squeeze conditions.

The alert passes the label/momentum check if any of these are true:

```text
Alert label is MOMENTUM
Alert label starts with MOMENTUM
Alert label is BREAKOUT
Alert label is NBREAK
Alert label is HUGE
Alert label is HUGE S
Change % >= 40
RV >= 100
Potential squeeze flag is present and RV >= 50
```

If none of these are true, the alert is normally not strong enough for entry unless it qualifies under the extreme tape rule below.

### 1.4 Size / Liquidity Grade

Classify the stock size using float and market cap.

```text
PASS_STRONG:
    FT < 5,000,000 OR MC < 10,000,000

PASS:
    FT <= 20,000,000 OR MC <= 30,000,000

PARTIAL:
    FT <= 50,000,000 OR MC <= 100,000,000

FAIL:
    Anything larger than those limits
```

For the main model, approved trade setups normally require `PASS_STRONG` or `PASS`. `PARTIAL` can be watched but should not be treated as a normal full-size entry unless the extreme tape rule triggers.

### 1.5 Structure Grade

Classify structure using the squeeze/borrow flags.

```text
PASS_STRONG:
    At least 2 of these are true:
    - Zero borrow
    - Reg SHO
    - Potential squeeze

PASS:
    Exactly 1 of these is true:
    - Zero borrow
    - Reg SHO
    - Potential squeeze

PARTIAL:
    Known runner flag is present

FAIL:
    None of the above
```

### 1.6 Tape Confirmation

Tape is confirmed when:

```text
Current alert price > prior same-day alert price
AND
(
    alert is a continuation label
    OR RV >= 50
)
```

Continuation labels are:

```text
MOMENTUM
MOMENTUM...
BREAKOUT
NBREAK
HUGE
HUGE S
```

### 1.7 Exact Approved Entry Setups

A market-buy entry is allowed only if one of the following setups is true.

#### Setup A - Alert 2 Extreme Tape Confirmed

Only applies to alert number `2`.

Entry is allowed if:

```text
Alert number = 2
RV >= 500
Current alert price > prior same-day alert price
At least one is true: NEWS, zero borrow, Reg SHO
Change % is missing OR change % <= 80
No hard reject rule is triggered
```

Decision reason:

```text
alert2_extreme_tape_confirmed
```

#### Setup B - Clean Catalyst Trade

Entry is allowed if:

```text
Alert number >= 3
Size grade is PASS_STRONG or PASS
NEWS flag is present
Tape is confirmed
RV >= 50
No hard reject rule is triggered
```

Decision reason:

```text
clean_catalyst_trade
```

#### Setup C - Squeeze Momentum Trade

Entry is allowed if:

```text
Alert number >= 3
Size grade is PASS_STRONG or PASS
Structure grade is PASS_STRONG or PASS
Tape is confirmed
RV >= 50
No hard reject rule is triggered
```

Decision reason:

```text
squeeze_momentum_trade
```

#### Setup D - Pure Tape Trade

Entry is allowed if:

```text
Alert number >= 3
Size grade is PASS_STRONG
Tape is confirmed
RV >= 200
No hard reject rule is triggered
```

Decision reason:

```text
pure_tape_trade
```

#### Setup E - Extreme Tape Trade

Entry is allowed if:

```text
Alert number >= 3
RV >= 500
Current alert price > prior same-day alert price
No hard reject rule is triggered
```

Decision reason:

```text
extreme_tape_trade
```

### 1.8 Entry Decision Output

The entry decision must be one of:

```text
TRADE_NOW
WATCH_ONLY
SKIP
```

Use `TRADE_NOW` only when one of the exact approved setups is matched.

If `TRADE_NOW`:

```text
Action: market buy after alert.
Entry price E: actual broker fill price.
Stake: £10,000 unless testing smaller.
Immediately calculate the exit levels from E.
```

If more than one setup matches, use the strongest decision reason in this order:

```text
alert2_extreme_tape_confirmed
clean_catalyst_trade
squeeze_momentum_trade
pure_tape_trade
extreme_tape_trade
```

## 2. Exit, Take-Profit, Stop, and Trail Rules Using 1-Minute Candles

Once the market buy has filled, all trade management is based on 1-minute candle closes.

The entry price is the actual market fill price, called `E`.

After entry, calculate:

```text
Initial stop = E x 0.90
First profit trigger = E x 1.10
Protected profit floor = E x 1.075
Runner trigger = E x 1.25
Runner trail = 15% below the highest runner candle close
```

The trade can be in one of these states:

```text
UNPROTECTED
PROTECTED
RUNNER
CLOSED
```

### 2.1 State 1 - Unprotected

This is the state immediately after the market buy fills.

Rules:

```text
If a 1-minute candle closes at or below E x 0.90:
    Exit at that candle close.
    Outcome = stopped_before_trigger.

If a 1-minute candle closes at or above E x 1.10:
    Move to PROTECTED state.
    Do not exit yet.

If neither happens:
    Keep holding until the next 1-minute candle close.
```

Important:

```text
The 10% stop is not a guaranteed 10% maximum loss.
If the candle closes below the stop, the model exits at the actual candle close.
Example: if stop is -10% but the candle closes -17%, the realised model loss is -17%.
```

### 2.2 State 2 - Protected

This state starts only after a 1-minute candle closes at or above `E x 1.10`.

Rules:

```text
If a 1-minute candle closes at or below E x 1.075:
    Exit at that candle close.
    Outcome = protected_profit_exit.

If a 1-minute candle closes at or above E x 1.25:
    Move to RUNNER state.
    Set highest_runner_close = that candle close.
    Set runner_trail_exit = highest_runner_close x 0.85.

If neither happens:
    Keep holding until the next 1-minute candle close.
```

Important:

```text
The protected floor is 7.5% above entry, but the exit is still the actual candle close.
If price closes through the floor, take the real close, not a perfect 7.5%.
```

### 2.3 State 3 - Runner

This state starts only after a 1-minute candle closes at or above `E x 1.25`.

Rules:

```text
For each new 1-minute candle close:
    If close > highest_runner_close:
        highest_runner_close = close
        runner_trail_exit = highest_runner_close x 0.85

    If close <= runner_trail_exit:
        Exit at that candle close.
        Outcome = runner_trail_exit.

    Else:
        Keep holding.
```

The trail is always based on the highest runner candle close, not the entry price.

Example:

```text
Entry E = 1.00
Runner starts when close >= 1.25
Highest runner close later becomes 2.00
Runner trail exit = 2.00 x 0.85 = 1.70
If a later candle closes at or below 1.70, exit at that actual close.
```

### 2.4 End-of-Day Rule

No overnight hold is part of this model.

At the final usable 1-minute candle of the day:

```text
If trade is still open:
    Exit at final same-day 1-minute candle close.
```

Outcome labels:

```text
unprotected_end_day
protected_end_day
runner_end_day
```

### 2.5 Position Sizing

Base model:

```text
Stake = £10,000 fixed per trade.
```

Forward-test sizing:

```text
Paper test: full rules, no capital.
Small live test: £1,000 to £2,500.
Scaled live test: £5,000.
Full model: £10,000 only after fills are proven.
```

Do not reinvest profits in the current recommended model. The 10% reinvest test made the sample worse because it increased stake before later losing trades.

## 3. AI Prompting for Final Trade Decision

The AI prompt must force a final trade decision, not just commentary.

The AI must return:

```text
TRADE_NOW
WATCH_ONLY
SKIP
```

It must also return the exact matched setup or exact reject reason.

### 3.1 AI Decision Prompt

Use this prompt for each scanner alert.

```text
You are reviewing a live scanner alert for a momentum trading model.

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
Initial stop after fill: fill x 0.90
First trigger after fill: fill x 1.10
Protected floor after fill: fill x 1.075
Runner trigger after fill: fill x 1.25
Runner trail after runner trigger: highest runner 1m close x 0.85
Notes: one short sentence only
```

### 3.2 Required AI Output Example

```text
Decision: TRADE_NOW
Matched setup: clean_catalyst_trade
Reject reason: none
Size grade: PASS
Structure grade: PASS
Tape confirmed: yes
Entry action if TRADE_NOW: market buy after alert
Entry price: actual broker market fill
Initial stop after fill: fill x 0.90
First trigger after fill: fill x 1.10
Protected floor after fill: fill x 1.075
Runner trigger after fill: fill x 1.25
Runner trail after runner trigger: highest runner 1m close x 0.85
Notes: Approved because catalyst, size, tape, and RV all match the model.
```

### 3.3 Live Trade Management Prompt After Entry

After the market buy is filled, use this prompt for trade management:

```text
Trade state review:
Ticker:
Actual fill price E:
Current state: UNPROTECTED / PROTECTED / RUNNER
Latest 1-minute candle close:
Initial stop E x 0.90:
First trigger E x 1.10:
Protected floor E x 1.075:
Runner trigger E x 1.25:
Highest runner close, if runner:
Runner trail exit, if runner:

Return the next action only:
- HOLD_UNPROTECTED
- MOVE_TO_PROTECTED
- EXIT_STOP
- HOLD_PROTECTED
- MOVE_TO_RUNNER
- EXIT_PROTECTED
- HOLD_RUNNER
- EXIT_RUNNER_TRAIL
- EXIT_END_OF_DAY

Use only the latest 1-minute candle close.
Do not use candle high, candle low, or guessed tick data.
```

## Final Working System

The final system is:

```text
1. Scanner alert appears.
2. AI applies exact hard reject and setup rules.
3. If AI returns TRADE_NOW, buy at market after the alert.
4. Actual broker fill becomes entry E.
5. Manage all exits from E using 1-minute candle closes.
6. Stop = E x 0.90.
7. Protected mode starts at E x 1.10.
8. Protected floor = E x 1.075.
9. Runner mode starts at E x 1.25.
10. Runner trail = 15% below highest runner candle close.
11. Exit all open trades by end of day.
12. Use fixed stake unless separately testing smaller live size.
```
