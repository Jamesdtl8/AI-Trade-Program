System Overview
The system has two distinct components that operate independently.
Component 1 — Scanner Grader
Listens to a Discord channel for scanner alerts. Processes each alert through Python hard rules first, then sends qualifying tickers to AI for grading. Returns a TRADE, MONITOR, or PASS decision. Once a TRADE decision is made the grader stops watching that ticker for the day.
Component 2 — Position Manager
Takes over the moment a TRADE decision is returned. Monitors the open position via live P&L from the broker. Manages a tiered trailing stop that widens as the move extends. Executes exit when the trail is breached. Operates entirely in Python — no AI involvement in position management.
These two components share the database but are otherwise independent. The grader does not know or care about position management. The position manager does not interact with the scanner feed.

State Machine — Scanner Grader
Every ticker moves through the following states. Only one state is active at a time.
NEW → WATCHING → PENDING_AI → TRADE (hands off to Position Manager)
                            → WATCH (loops back to PENDING_AI on next alert)
                            → PASS (terminal)
             → DISQUALIFIED (terminal, set by Python at any stage)
StateMeaningNEWFirst alert received, state object createdWATCHINGAlerts 1 and 2 stored, Python rules passing, waiting for alert 3PENDING_AIAlert 3 (or later) has passed Python gates, sent to AI for gradingTRADEAI returned TRADE — scanner ignores this ticker for rest of day, position manager takes overWATCHAI returned MONITOR — holding, next alert sent to AI with full historyPASSAI returned PASS, or Python disqualified — loop endsDISQUALIFIEDPython hard rules eliminated this ticker permanently

State Machine — Position Manager
Runs independently per open position once TRADE is set.
OPEN → TRAIL_ARMED (at +7.5%) → EXITED
StateMeaningOPENPosition entered, monitoring P&L, trail not yet activeTRAIL_ARMEDPrice has reached +7.5% from entry, trailing stop is now active and updatingEXITEDTrail breached or hard stop hit — position closed

Trailing Stop Ladder
The trail percentage widens as the move extends to give the position room to breathe at higher gain levels. Volatile momentum stocks regularly pull back 15–25% intraday even during strong uptrends. A fixed tight trail would exit prematurely on normal consolidation.
The trail is calculated against highest_price_seen — the peak price observed since entry. It NEVER moves down. When a new high is reached, the stop updates upward. When price falls, the stop holds at its current level until breached. Additionally, the stop level itself can never decrease when crossing into a new tier (the running highest_stop is tracked and enforced).

ACTUAL VALUES (trail_stop.py — last updated 2026-05-29):
Gain from entry (peak) | Trail % below peak | Effective stop example ($4.00 entry)
Below +7.5%            | Hard stop only      | $3.60 (entry × 0.90)
+7.5% to +10%          | 5% trail            | arm at $4.30, stop = $4.30 × 0.95 = $4.09
+10% to +20%           | 7% trail            | peak $4.40, stop = $4.40 × 0.93 = $4.09
+20% to +40%           | 10% trail           | peak $4.80, stop = $4.80 × 0.90 = $4.32
+40% to +60%           | 15% trail           | peak $5.60, stop = $5.60 × 0.85 = $4.76
+60% to +100%          | 20% trail           | peak $6.40, stop = $6.40 × 0.80 = $5.12
+100% to +150%         | 25% trail           | peak $8.00, stop = $8.00 × 0.75 = $6.00
+150% to +200%         | 30% trail           | peak $10.00, stop = $10.00 × 0.70 = $7.00
+200% to +300%         | 35% trail           | peak $12.00, stop = $12.00 × 0.65 = $7.80
+300% and above        | 40% trail           | peak $16.00, stop = $16.00 × 0.60 = $9.60

Hard stop: Always active regardless of trail state. Entry × 0.90 (−10%). The hard stop provides the absolute floor — if price falls 10% below entry before trail activates, exit immediately.
Trail activation: Trail becomes active permanently once peak gain reaches +7.5% (entry × 1.075). Before that, only the hard stop applies. Once armed, the trail stays armed even if price drops back below +7.5%.
Stop continuity: The stop level is tracked as a running maximum (highest_stop). When a tier boundary is crossed (e.g. from +9.9% to +10%), the wider trail percentage could mathematically lower the stop level — this is prevented by always using max(new_calc, highest_stop_seen). The stop can only go up, never down.

Example — entry $4.00, stock runs to +333% peak ($17.32):
Price   | Gain  | Trail active | Trail % | Stop level
$3.60   | -10%  | No           | hard    | $3.60 → EXIT
$4.30   | +7.5% | YES — armed  | 5%      | $4.09
$4.40   | +10%  | Yes          | 7%      | $4.09 (highest_stop held)
$4.80   | +20%  | Yes          | 10%     | $4.32
$5.60   | +40%  | Yes          | 15%     | $4.76
$6.40   | +60%  | Yes          | 20%     | $5.12
$8.00   | +100% | Yes          | 25%     | $6.00
$10.00  | +150% | Yes          | 30%     | $7.00
$17.32  | +333% | Yes          | 40%     | $10.39

At the $17.32 high, stop sits at $10.39. When price reverses through $10.39 the position exits, capturing the majority of the 333% move.

Data Model
Table: ticker_states
sqlCREATE TABLE ticker_states (
    id                  SERIAL PRIMARY KEY,
    ticker              VARCHAR(20) NOT NULL,
    date                DATE NOT NULL DEFAULT CURRENT_DATE,
    state               VARCHAR(20) NOT NULL DEFAULT 'NEW',
    alert_count         INTEGER NOT NULL DEFAULT 0,
    alerts              JSONB NOT NULL DEFAULT '[]',
    ai_context          JSONB,
    ai_grade            VARCHAR(10),
    ai_decision         VARCHAR(10),
    entry_price         NUMERIC(10, 2),
    target_price        NUMERIC(10, 2),
    disqualify_reason   VARCHAR(100),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, date)
);
Table: positions
Tracks every open and closed position managed by the position manager.
sqlCREATE TABLE positions (
    id                  SERIAL PRIMARY KEY,
    ticker              VARCHAR(20) NOT NULL,
    date                DATE NOT NULL DEFAULT CURRENT_DATE,
    state               VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    entry_price         NUMERIC(10, 2) NOT NULL,
    entry_time          TIMESTAMP NOT NULL,
    hard_stop           NUMERIC(10, 2) NOT NULL,
    trail_active        BOOLEAN NOT NULL DEFAULT FALSE,
    trail_pct           NUMERIC(5, 2),
    current_stop        NUMERIC(10, 2) NOT NULL,
    highest_price_seen  NUMERIC(10, 2) NOT NULL,
    current_price       NUMERIC(10, 2),
    current_gain_pct    NUMERIC(8, 2),
    exit_price          NUMERIC(10, 2),
    exit_time           TIMESTAMP,
    exit_reason         VARCHAR(50),
    final_gain_pct      NUMERIC(8, 2),
    broker_position_id  VARCHAR(100),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, date)
);
Table: position_updates
Immutable log of every P&L update received from broker for open positions.
sqlCREATE TABLE position_updates (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    price           NUMERIC(10, 2) NOT NULL,
    gain_pct        NUMERIC(8, 2),
    stop_level      NUMERIC(10, 2),
    trail_pct       NUMERIC(5, 2),
    action_taken    VARCHAR(20),
    received_at     TIMESTAMP NOT NULL DEFAULT NOW()
);
Table: alert_log
sqlCREATE TABLE alert_log (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    alert_number    INTEGER NOT NULL,
    raw_content     TEXT NOT NULL,
    parsed          JSONB NOT NULL,
    received_at     TIMESTAMP NOT NULL DEFAULT NOW()
);
Table: ai_decisions
sqlCREATE TABLE ai_decisions (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    alert_number    INTEGER NOT NULL,
    ai_input        JSONB NOT NULL,
    ai_output       JSONB NOT NULL,
    grade           VARCHAR(10),
    action          VARCHAR(10),
    entry_price     NUMERIC(10, 2),
    target_price    NUMERIC(10, 2),
    latency_ms      INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

Discord Integration
pythonimport discord
import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SCANNER_CHANNEL_ID = int(os.environ["SCANNER_CHANNEL_ID"])
SCANNER_BOT_USER_ID = int(os.environ["SCANNER_BOT_USER_ID"])

class ScannerBot(discord.Client):

    async def on_ready(self):
        print(f"Connected as {self.user}")

    async def on_message(self, message):
        if message.channel.id != SCANNER_CHANNEL_ID:
            return
        if message.author.id != SCANNER_BOT_USER_ID:
            return
        await process_raw_message(message.content, message.created_at)

intents = discord.Intents.default()
intents.message_content = True
client = ScannerBot(intents=intents)
client.run(DISCORD_TOKEN)

Alert Parser
Parses raw Discord message content into a structured dataclass.
pythonimport re
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class ParsedAlert:
    ticker: str
    alert_number: int
    label: Optional[str]
    change_pct: Optional[float]
    price: float
    float_shares: Optional[float]
    market_cap: Optional[float]
    rv: Optional[float]
    volume_1m: Optional[int]
    indicators: list
    news: Optional[str]
    has_offering_flag: bool
    reverse_split: Optional[str]
    borrow_confirmed: bool
    ctb: Optional[float]
    si: Optional[float]
    is_whale_print: bool
    whale_direction: Optional[str]
    whale_value: Optional[float]
    is_halt: bool
    halt_direction: Optional[str]
    timestamp: datetime
    raw: str


def parse_alert(content: str, timestamp: datetime) -> Optional[ParsedAlert]:

    # Whale print
    whale_match = re.search(
        r'LARGE WHALE.*?Direction:\s*([↑↓→]).*?Price:\s*([\d.]+).*?Value:\s*\$([\d,.]+)',
        content
    )
    if whale_match:
        ticker_match = re.search(r'\*\*(\w+)\*\*', content)
        if not ticker_match:
            return None
        direction_map = {"↑": "BUY", "↓": "SELL", "→": "NEUTRAL"}
        return ParsedAlert(
            ticker=ticker_match.group(1),
            alert_number=-1, label=None, change_pct=None,
            price=float(whale_match.group(2)),
            float_shares=None, market_cap=None, rv=None, volume_1m=None,
            indicators=[], news=None, has_offering_flag=False,
            reverse_split=None, borrow_confirmed=False, ctb=None, si=None,
            is_whale_print=True,
            whale_direction=direction_map.get(whale_match.group(1), "NEUTRAL"),
            whale_value=float(whale_match.group(3).replace(",", "")),
            is_halt=False, halt_direction=None,
            timestamp=timestamp, raw=content
        )

    # Halt
    halt_match = re.search(r'HALTED\s+(UP|DOWN)', content)
    if halt_match:
        ticker_match = re.search(r'\*\*(\w+)\*\*', content)
        price_match = re.search(r'Price:\s*\$([\d.]+)', content)
        if not ticker_match:
            return None
        return ParsedAlert(
            ticker=ticker_match.group(1),
            alert_number=-1, label=None, change_pct=None,
            price=float(price_match.group(1)) if price_match else 0.0,
            float_shares=None, market_cap=None, rv=None, volume_1m=None,
            indicators=[], news=None, has_offering_flag=False,
            reverse_split=None, borrow_confirmed=False, ctb=None, si=None,
            is_whale_print=False, whale_direction=None, whale_value=None,
            is_halt=True, halt_direction=halt_match.group(1),
            timestamp=timestamp, raw=content
        )

    # Borrow confirmation
    borrow_match = re.search(
        r'0 BORROW.*?CTB:\s*([\d.]+)%.*?SI:\s*([\d.]+)%', content
    )
    if borrow_match:
        ticker_match = re.search(r'\*\*(\w+)\s', content)
        if not ticker_match:
            return None
        return ParsedAlert(
            ticker=ticker_match.group(1),
            alert_number=-1, label=None, change_pct=None, price=0.0,
            float_shares=None, market_cap=None, rv=None, volume_1m=None,
            indicators=[], news=None, has_offering_flag=False,
            reverse_split=None, borrow_confirmed=True,
            ctb=float(borrow_match.group(1)), si=float(borrow_match.group(2)),
            is_whale_print=False, whale_direction=None, whale_value=None,
            is_halt=False, halt_direction=None,
            timestamp=timestamp, raw=content
        )

    has_offering = bool(re.search(r'OFFERING', content))

    ticker_match = re.search(r'\*\*(\w+)\*\*', content)
    alert_num_match = re.search(r'`#(\d+)`', content)
    label_match = re.search(
        r'`(MOMENTUM[^`]*|BREAKOUT|NBREAK|REV V|BTT V|HUGE S)`', content
    )
    change_match = re.search(r'[↑↓]([\d.]+)%', content)
    price_match = re.search(r'\`\$([\d.]+)\`|\$\s*([\d.]+)', content)
    ft_match = re.search(r'FT\s+([\d.]+)([KMB]?)', content)
    mc_match = re.search(r'MC\s+([\d.]+)([KMB]?)', content)
    rv_match = re.search(r'RV\s+([\d.]+)x', content)
    vol_match = re.search(r'1V\s+([\d.]+)([KMB]?)', content)
    ind_match = re.search(r'IND.*?[·•](.*?)(?:\||$)', content)
    rs_match = re.search(r'R/S\s*[·•]\s*(1:\d+)', content)

    if not ticker_match or not alert_num_match:
        return None

    def parse_suffixed(value_str, suffix_str):
        v = float(value_str)
        s = suffix_str.upper()
        if s == 'K': return v * 1_000
        if s == 'M': return v * 1_000_000
        if s == 'B': return v * 1_000_000_000
        return v

    float_shares = parse_suffixed(ft_match.group(1), ft_match.group(2)) if ft_match else None
    market_cap = parse_suffixed(mc_match.group(1), mc_match.group(2)) if mc_match else None

    indicators = []
    if ind_match:
        raw_ind = ind_match.group(1)
        for ind in ["0 Borrow", "Reg SHO", "Potential Squeeze", "Known Runner"]:
            if ind.lower() in raw_ind.lower():
                indicators.append(ind)

    news = None
    news_match = re.search(r'NEWS.*?[•·]\s*(.+?)\s*-\s*\[LINK\]', content)
    if news_match:
        news = news_match.group(1).strip()

    price_val = None
    if price_match:
        price_val = float(price_match.group(1) or price_match.group(2))
    if price_val is None:
        return None

    return ParsedAlert(
        ticker=ticker_match.group(1),
        alert_number=int(alert_num_match.group(1)),
        label=label_match.group(1) if label_match else None,
        change_pct=float(change_match.group(1)) if change_match else None,
        price=price_val,
        float_shares=float_shares,
        market_cap=market_cap,
        rv=parse_suffixed(rv_match.group(1), '') if rv_match else None,
        volume_1m=int(parse_suffixed(vol_match.group(1), vol_match.group(2))) if vol_match else None,
        indicators=indicators,
        news=news,
        has_offering_flag=has_offering,
        reverse_split=rs_match.group(1) if rs_match else None,
        borrow_confirmed=False, ctb=None, si=None,
        is_whale_print=False, whale_direction=None, whale_value=None,
        is_halt=False, halt_direction=None,
        timestamp=timestamp, raw=content
    )

Step 1 — Python Hard Rules
Plain English Rules
Permanent disqualification — ticker never recovers:

Float over 50 million shares — too large to squeeze
Market cap over 100 million dollars — too large to move meaningfully
Price below 10 cents — spread too wide, execution unreliable
Relative volume below 1x — no buying interest above average
Offering flag present — dilutive, kills upside
NBREAK label at alert 3 or later — price failed to hold a breakout level

Deferral — alert stored, ticker stays active, no AI call:

Alert number is 1 or 2 — accumulating history
Alert 3 label is absent — scanner did not classify the move

Alert 2 early promotion — send to AI before alert 3 if ALL true:

Float under 2 million shares
At least one of: 0 Borrow, Reg SHO, Potential Squeeze
Label at alert 2 is MOMENTUM or BREAKOUT
Price at alert 2 is higher than price at alert 1
RV at alert 2 is above 50x

Rationale: on extreme micro-float stocks with confirmed squeeze and a MOMENTUM label at alert 2, waiting for alert 3 costs significant entry price. ATPC and AMSS from the test feed both qualified under these criteria. This is the only scenario where a sub-alert-3 AI call is made.
Alert 3 Python pre-check before AI:

NBREAK label → DISQUALIFIED
No label → DISQUALIFIED
Price at alert 3 below price at alert 2 → DISQUALIFIED
If none of the above → send to AI

Code
pythonfrom typing import Tuple, Optional

FLOAT_LIMIT = 50_000_000
MC_LIMIT = 100_000_000
PRICE_MIN = 0.10
RV_MIN = 1.0
EARLY_PROMOTION_FLOAT_LIMIT = 2_000_000
EARLY_PROMOTION_RV_MIN = 50.0
SQUEEZE_INDICATORS = {"0 Borrow", "Reg SHO", "Potential Squeeze"}
MOMENTUM_LABELS = {"MOMENTUM", "BREAKOUT"}


def hard_disqualify(alert: ParsedAlert) -> Tuple[bool, Optional[str]]:
    if alert.float_shares and alert.float_shares > FLOAT_LIMIT:
        return True, "float_too_large"
    if alert.market_cap and alert.market_cap > MC_LIMIT:
        return True, "mc_too_large"
    if alert.price < PRICE_MIN:
        return True, "price_too_low"
    if alert.rv is not None and alert.rv < RV_MIN:
        return True, "rv_too_low"
    if alert.has_offering_flag:
        return True, "offering_present"
    if alert.alert_number >= 3 and alert.label == "NBREAK":
        return True, "nbreak_at_3"
    return False, None


def should_send_to_ai(state: dict, alert: ParsedAlert) -> Tuple[bool, str]:
    alerts = state["alerts"]
    alert_count = len(alerts)

    if alert_count == 3:
        if alert.label not in MOMENTUM_LABELS:
            return False, "no_momentum_label_at_3"
        alert_2 = alerts[1]
        if alert.price <= alert_2["price"]:
            return False, "price_not_higher_than_alert_2"
        return True, "alert_3_standard"

    if alert_count == 2:
        alert_1 = alerts[0]
        float_ok = state.get("float_shares") and state["float_shares"] < EARLY_PROMOTION_FLOAT_LIMIT
        squeeze_ok = bool(SQUEEZE_INDICATORS.intersection(set(alert.indicators)))
        label_ok = alert.label in MOMENTUM_LABELS
        price_ok = alert.price > alert_1["price"]
        rv_ok = alert.rv is not None and alert.rv >= EARLY_PROMOTION_RV_MIN
        if float_ok and squeeze_ok and label_ok and price_ok and rv_ok:
            return True, "alert_2_early_promotion"
        return False, "alert_2_accumulating"

    if alert_count == 1:
        return False, "alert_1_accumulating"

    if alert_count >= 4 and state["state"] == "WATCH":
        return True, "continuation_watch"

    return False, "not_ready"

Step 2 — State Management
pythonfrom flask import Flask
from sqlalchemy import create_engine, text
from datetime import date
import json, os

app = Flask(__name__)
engine = create_engine(os.environ["DATABASE_URL"])


def get_or_create_state(ticker: str) -> dict:
    today = date.today().isoformat()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM ticker_states WHERE ticker=:t AND date=:d"),
            {"t": ticker, "d": today}
        ).fetchone()
        if row:
            return dict(row._mapping)
        conn.execute(
            text("INSERT INTO ticker_states (ticker, date, state, alert_count, alerts) VALUES (:t, :d, 'NEW', 0, '[]')"),
            {"t": ticker, "d": today}
        )
        conn.commit()
        return get_or_create_state(ticker)


def update_state(ticker: str, updates: dict):
    today = date.today().isoformat()
    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    set_clauses += ", updated_at = NOW()"
    with engine.connect() as conn:
        conn.execute(
            text(f"UPDATE ticker_states SET {set_clauses} WHERE ticker=:ticker AND date=:date"),
            {**updates, "ticker": ticker, "date": today}
        )
        conn.commit()


async def process_raw_message(content: str, timestamp):
    alert = parse_alert(content, timestamp)
    if alert is None:
        return

    ticker = alert.ticker

    if alert.borrow_confirmed:
        state = get_or_create_state(ticker)
        if state["state"] in ("WATCHING", "WATCH"):
            ctx = state.get("ai_context") or {}
            ctx.update({"borrow_confirmed": True, "ctb": alert.ctb, "si": alert.si})
            update_state(ticker, {"ai_context": json.dumps(ctx)})
        return

    if alert.is_halt or alert.is_whale_print:
        return

    state = get_or_create_state(ticker)

    if state["state"] in ("TRADE", "PASS", "DISQUALIFIED"):
        return

    eliminated, reason = hard_disqualify(alert)
    if eliminated:
        update_state(ticker, {"state": "DISQUALIFIED", "disqualify_reason": reason})
        return

    alerts = json.loads(state["alerts"]) if isinstance(state["alerts"], str) else state["alerts"]
    alerts.append({
        "number": alert.alert_number,
        "price": alert.price,
        "rv": alert.rv,
        "label": alert.label,
        "change_pct": alert.change_pct,
        "indicators": alert.indicators,
        "news": alert.news,
        "timestamp": alert.timestamp.isoformat(),
        "float_shares": alert.float_shares,
        "market_cap": alert.market_cap
    })

    float_val = alert.float_shares or state.get("float_shares")
    update_state(ticker, {
        "state": "WATCHING" if state["state"] == "NEW" else state["state"],
        "alert_count": len(alerts),
        "alerts": json.dumps(alerts),
        "float_shares": float_val
    })

    state = get_or_create_state(ticker)
    ready, reason = should_send_to_ai(state, alert)
    if ready:
        await call_ai_grader(state, alert, reason)

Step 3 — AI Grading
System Prompt
You are a momentum stock scanner grading system. Evaluate tickers at alert 3 and return a structured JSON decision. No explanations unless requested.

FIELD DEFINITIONS
FT = float shares | MC = market cap | RV = relative volume multiple | IND = indicator flags
Labels: MOMENTUM/BREAKOUT = continuation signals | NBREAK = failed breakout, skip | REV V/BTT V = reversal, weak | no label = skip

IND flags:
0 Borrow = no shares to short, squeeze fuel
Reg SHO = SEC threshold list, amplifies squeeze
Potential Squeeze = elevated short interest vs float
Known Runner = prior history of same pattern

HARD SKIP RULES — if any are true, grade is SKIP, no further evaluation
- Float over 50M OR market cap over 100M
- Label at alert 3 is NBREAK or absent
- Price at alert 3 is lower than price at alert 2
- RV declining across all three alerts consecutively
- Active dilutive OFFERING flag present

GATE SCORING
Gate 1 — Float/MC
PASS_STRONG: float <5M or MC <$10M
PASS: float 5M–20M or MC $10–30M
PARTIAL: float 20–50M or MC $30–100M
FAIL: over those limits → auto SKIP

Gate 2 — Catalyst
PASS: specific named news, 8-K, 6-K, or corporate event in any of the three alerts
PARTIAL: Form 4/3 only, or vague update, or no named counterparty
FAIL: no news across all three alerts
Override: Gate 2 FAIL becomes PARTIAL if Gate 1 is PASS_STRONG AND Gate 3 is PASS or stronger

Gate 3 — Structural constraint
PASS_STRONG: two or more of {0 Borrow, Reg SHO, Potential Squeeze}
PASS: one of {0 Borrow, Reg SHO, Potential Squeeze}
PARTIAL: Known Runner only — does not count toward PASS_STRONG threshold
FAIL: none present

Gate 4 — Velocity
PASS: label is MOMENTUM or BREAKOUT AND RV alert3 > RV alert2 AND price alert3 > price alert2
PARTIAL: label is MOMENTUM or BREAKOUT AND only one of RV or price condition met
FAIL: anything else → auto SKIP

RISK MODIFIERS
R/S flag present → cap grade at WATCH maximum
Whale SELL in sequence → add to risk_flags, do not auto-downgrade
Borrow confirmation alert fired → strengthens Gate 3, note CTB value

FINAL GRADE
STRONG: Gate1 PASS or stronger + Gate2 PASS or override + Gate3 PASS or stronger + Gate4 PASS + no R/S + no offering
WATCH: Gate1 PASS or PARTIAL + Gate4 PASS or PARTIAL + at least one of Gate2/Gate3 at PASS + or STRONG criteria met but R/S present
SKIP: any hard skip rule triggered, or Gate1 FAIL, or Gate4 FAIL, or fewer than two gates at PASS with Gate4 PARTIAL

PRICE RANGE CALCULATION
entry_price = alert 3 price × 1.03, rounded to 2 decimal places
target_price = alert 3 price + ((alert 3 price - alert 2 price) × 0.5), rounded to 2 decimal places
Note: target_price is a conservative minimum continuation estimate only. The position manager uses a trailing stop and will stay in the trade well beyond this level if momentum holds.

CONTEXT BLOCK RULES
ticker must be populated from the alert data, never empty
initial_grade and current_grade must match the top-level grade field exactly, never PENDING
grade_timestamp must be populated from the alert 3 timestamp, never empty
catalyst must contain the full catalyst text from the most recent alert in the sequence that contains news — never the word "same" and never empty
highest_price_seen must be set to alert_3_price at initialisation, never 0
rv_sequence must be populated from all three alerts as a flat array: [alert1_rv, alert2_rv, alert3_rv]
price_sequence must be populated from all three alerts as a flat array: [alert1_price, alert2_price, alert3_price]
all prices rounded to 2 decimal places, never more

OUTPUT FORMAT
Return only this JSON. No prose. No explanation. No markdown outside the JSON block.

{
  "grade": "STRONG | WATCH | SKIP",
  "action": "TRADE | MONITOR | PASS",
  "entry_price": 0.00,
  "target_price": 0.00,
  "risk_flags": [],
  "context": {
    "ticker": "",
    "initial_grade": "",
    "grade_timestamp": "",
    "alert_3_price": 0.00,
    "alert_3_rv": 0.00,
    "realistic_entry": 0.00,
    "gates": {
      "gate_1": "",
      "gate_2": "",
      "gate_3": "",
      "gate_4": ""
    },
    "risk_flags": [],
    "catalyst": "",
    "highest_price_seen": 0.00,
    "highest_rv_seen": 0.00,
    "alert_count": 3,
    "halts_fired": 0,
    "whale_prints": [],
    "borrow_confirmed": false,
    "ctb": null,
    "si": null,
    "rv_sequence": [],
    "price_sequence": [],
    "current_grade": "",
    "grade_change_history": []
  }
}
Building the User Message
pythondef build_user_message(state: dict, current_alert: ParsedAlert) -> str:
    alerts = json.loads(state["alerts"]) if isinstance(state["alerts"], str) else state["alerts"]

    latest_news = None
    for a in reversed(alerts):
        if a.get("news"):
            latest_news = a["news"]
            break

    lines = ["ALERT HISTORY:\n"]
    for i, a in enumerate(alerts, 1):
        news_text = a.get("news") or latest_news or "none"
        label_text = a.get("label") or "none"
        line = (
            f"#{i} | {a['timestamp']} | ${a['price']} | "
            f"FT {fmt_number(a.get('float_shares'))} | "
            f"MC {fmt_number(a.get('market_cap'))} | "
            f"RV {a.get('rv', 0)}x | "
            f"IND: {', '.join(a.get('indicators', [])) or 'none'} | "
            f"Label: {label_text} | "
            f"Change: {a.get('change_pct', 0)}% | "
            f"News: {news_text}"
        )
        lines.append(line)

    if state["state"] == "WATCH" and state.get("ai_context"):
        lines.append("\nPRIOR AI DECISION:")
        lines.append(json.dumps(state["ai_context"], indent=2))

    return "\n".join(lines)


def fmt_number(val):
    if val is None: return "N/A"
    if val >= 1_000_000_000: return f"{val/1_000_000_000:.1f}B"
    if val >= 1_000_000: return f"{val/1_000_000:.1f}M"
    if val >= 1_000: return f"{val/1_000:.0f}K"
    return str(val)
AI Call and Decision Routing
pythonimport anthropic
import time

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def call_ai_grader(state: dict, current_alert: ParsedAlert, reason: str):
    user_message = build_user_message(state, current_alert)
    start = time.time()

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        latency_ms = int((time.time() - start) * 1000)
        result = json.loads(response.content[0].text.strip())
    except Exception as e:
        print(f"AI grading failed for {current_alert.ticker}: {e}")
        return

    action = result.get("action")
    grade = result.get("grade")

    alerts = json.loads(state["alerts"]) if isinstance(state["alerts"], str) else state["alerts"]
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO ai_decisions
                (ticker, alert_number, ai_input, ai_output, grade, action, entry_price, target_price, latency_ms)
                VALUES (:ticker, :alert_number, :ai_input, :ai_output, :grade, :action, :entry_price, :target_price, :latency_ms)
            """),
            {
                "ticker": current_alert.ticker,
                "alert_number": len(alerts),
                "ai_input": user_message,
                "ai_output": json.dumps(result),
                "grade": grade,
                "action": action,
                "entry_price": result.get("entry_price"),
                "target_price": result.get("target_price"),
                "latency_ms": latency_ms
            }
        )
        conn.commit()

    if action == "TRADE":
        entry = result.get("entry_price")
        update_state(current_alert.ticker, {
            "state": "TRADE",
            "ai_grade": grade,
            "ai_decision": action,
            "entry_price": entry,
            "target_price": result.get("target_price"),
            "ai_context": json.dumps(result.get("context", {}))
        })
        open_position(current_alert.ticker, entry, result)
        await notify_trade(current_alert.ticker, result)

    elif action == "MONITOR":
        update_state(current_alert.ticker, {
            "state": "WATCH",
            "ai_grade": grade,
            "ai_decision": action,
            "ai_context": json.dumps(result.get("context", {}))
        })

    elif action == "PASS":
        update_state(current_alert.ticker, {
            "state": "PASS",
            "ai_grade": grade,
            "ai_decision": action
        })

Step 4 — Position Manager
The position manager is triggered when open_position is called after a TRADE decision. It monitors live P&L from the broker and applies the trailing stop ladder. It runs as a background loop polling the broker API.
Trail Calculation
pythondef get_trail_pct(gain_pct: float) -> Optional[float]:
    """
    Returns the trail percentage for a given gain level.
    Returns None if trail has not yet activated (gain below 7.5%).
    """
    if gain_pct < 7.5:
        return None  # Hard stop only, trail not active
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


def calculate_stop(entry_price: float, highest_price: float, gain_pct: float) -> Tuple[float, bool, Optional[float]]:
    """
    Returns (stop_level, trail_active, trail_pct).
    Hard stop is always entry × 0.85.
    Trail stop is highest × (1 - trail_pct/100) when active.
    The higher of the two is used — trail can never be below hard stop.
    """
    hard_stop = round(entry_price * 0.85, 2)
    trail_pct = get_trail_pct(gain_pct)

    if trail_pct is None:
        return hard_stop, False, None

    trail_stop = round(highest_price * (1 - trail_pct / 100), 2)
    effective_stop = max(hard_stop, trail_stop)
    return effective_stop, True, trail_pct
Opening a Position
pythonfrom datetime import datetime

def open_position(ticker: str, entry_price: float, ai_result: dict):
    """
    Called immediately when TRADE decision is returned.
    Creates the position record with initial hard stop.
    """
    hard_stop = round(entry_price * 0.85, 2)
    today = date.today().isoformat()

    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO positions
                (ticker, date, state, entry_price, entry_time, hard_stop,
                 trail_active, current_stop, highest_price_seen)
                VALUES (:ticker, :date, 'OPEN', :entry_price, NOW(), :hard_stop,
                        FALSE, :hard_stop, :entry_price)
            """),
            {
                "ticker": ticker,
                "date": today,
                "entry_price": entry_price,
                "hard_stop": hard_stop
            }
        )
        conn.commit()
P&L Monitor Loop
This runs as a background thread or async task. Poll interval is configurable — 5 seconds recommended for active momentum positions.
pythonimport asyncio

POLL_INTERVAL_SECONDS = 5


async def position_monitor_loop():
    """
    Continuously polls open positions and applies trail logic.
    Runs as a background task alongside the Discord bot.
    """
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            await check_open_positions()
        except Exception as e:
            print(f"Position monitor error: {e}")


async def check_open_positions():
    today = date.today().isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM positions WHERE date=:d AND state IN ('OPEN', 'TRAIL_ARMED')"),
            {"d": today}
        ).fetchall()

    for row in rows:
        position = dict(row._mapping)
        await process_position_update(position)


async def process_position_update(position: dict):
    ticker = position["ticker"]
    entry_price = float(position["entry_price"])
    highest_price_seen = float(position["highest_price_seen"])

    # Fetch current price from broker
    current_price = await get_broker_price(ticker)
    if current_price is None:
        return

    gain_pct = round((current_price - entry_price) / entry_price * 100, 2)

    # Update highest price seen
    new_highest = max(highest_price_seen, current_price)

    # Calculate current stop
    stop_level, trail_active, trail_pct = calculate_stop(
        entry_price, new_highest, gain_pct
    )

    new_state = "TRAIL_ARMED" if trail_active else "OPEN"

    # Check if stop is breached
    if current_price <= stop_level:
        await exit_position(position, current_price, stop_level, trail_active, gain_pct)
        return

    # Update position record
    with engine.connect() as conn:
        conn.execute(
            text("""
                UPDATE positions SET
                    state = :state,
                    trail_active = :trail_active,
                    trail_pct = :trail_pct,
                    current_stop = :stop_level,
                    highest_price_seen = :highest,
                    current_price = :price,
                    current_gain_pct = :gain_pct,
                    updated_at = NOW()
                WHERE ticker = :ticker AND date = :date
            """),
            {
                "state": new_state,
                "trail_active": trail_active,
                "trail_pct": trail_pct,
                "stop_level": stop_level,
                "highest": new_highest,
                "price": current_price,
                "gain_pct": gain_pct,
                "ticker": ticker,
                "date": date.today().isoformat()
            }
        )
        conn.commit()

    # Log update
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO position_updates
                (ticker, price, gain_pct, stop_level, trail_pct, action_taken)
                VALUES (:ticker, :price, :gain_pct, :stop_level, :trail_pct, 'HOLD')
            """),
            {
                "ticker": ticker,
                "price": current_price,
                "gain_pct": gain_pct,
                "stop_level": stop_level,
                "trail_pct": trail_pct
            }
        )
        conn.commit()
Exiting a Position
pythonasync def exit_position(position: dict, current_price: float, stop_level: float, trail_active: bool, gain_pct: float):
    ticker = position["ticker"]
    exit_reason = "TRAIL_STOP" if trail_active else "HARD_STOP"

    with engine.connect() as conn:
        conn.execute(
            text("""
                UPDATE positions SET
                    state = 'EXITED',
                    exit_price = :exit_price,
                    exit_time = NOW(),
                    exit_reason = :exit_reason,
                    final_gain_pct = :gain_pct,
                    updated_at = NOW()
                WHERE ticker = :ticker AND date = :date
            """),
            {
                "exit_price": current_price,
                "exit_reason": exit_reason,
                "gain_pct": gain_pct,
                "ticker": ticker,
                "date": date.today().isoformat()
            }
        )
        conn.execute(
            text("""
                INSERT INTO position_updates
                (ticker, price, gain_pct, stop_level, action_taken)
                VALUES (:ticker, :price, :gain_pct, :stop_level, 'EXIT')
            """),
            {
                "ticker": ticker,
                "price": current_price,
                "gain_pct": gain_pct,
                "stop_level": stop_level
            }
        )
        conn.commit()

    await notify_exit(ticker, current_price, gain_pct, exit_reason, position)


async def notify_exit(ticker: str, price: float, gain_pct: float, reason: str, position: dict):
    channel = client.get_channel(int(os.environ["TRADE_ALERTS_CHANNEL_ID"]))
    if not channel:
        return

    emoji = "✅" if gain_pct > 0 else "❌"
    msg = (
        f"{emoji} **EXIT — {ticker}**\n"
        f"Reason: {reason}\n"
        f"Exit price: ${price}\n"
        f"Entry price: ${position['entry_price']}\n"
        f"P&L: {'+' if gain_pct > 0 else ''}{gain_pct}%\n"
        f"High during trade: ${position['highest_price_seen']}"
    )
    await channel.send(msg)
Broker Price Feed
Implement this function using the Interactive Brokers Client Portal API. The position manager calls this on every poll cycle. The IBKR CPAPI market data endpoint returns last price for a given conid (contract ID). The ticker-to-conid mapping must be maintained separately or resolved at position open time.
pythonasync def get_broker_price(ticker: str) -> Optional[float]:
    """
    Fetch current market price for ticker from IBKR CPAPI.
    Returns None if price cannot be retrieved.

    Implementation notes:
    - Base URL: https://localhost:5000/v1/api (IBKR gateway runs locally)
    - Endpoint: GET /iserver/marketdata/snapshot?conids={conid}&fields=31
    - Field 31 = last price
    - Requires active IBKR session (gateway must be running and authenticated)
    - SSL verification disabled for local gateway (verify=False)
    - Resolve conid from ticker using /iserver/secdef/search?symbol={ticker}
    """
    raise NotImplementedError(
        "Implement using IBKR CPAPI. "
        "See https://www.interactivebrokers.com/api/doc.html"
    )

Step 5 — Notifications
pythonasync def notify_trade(ticker: str, result: dict):
    channel = client.get_channel(int(os.environ["TRADE_ALERTS_CHANNEL_ID"]))
    if not channel:
        return
    entry = result["entry_price"]
    hard_stop = round(entry * 0.85, 2)
    msg = (
        f"🚀 **TRADE — {ticker}**\n"
        f"Grade: {result['grade']}\n"
        f"Entry: ${entry}\n"
        f"Initial target: ${result['target_price']}\n"
        f"Hard stop: ${hard_stop} (-15%)\n"
        f"Trail activates at: ${round(entry * 1.075, 2)} (+7.5%)\n"
        f"Catalyst: {result['context'].get('catalyst', 'N/A')}"
    )
    await channel.send(msg)

Step 6 — Daily Reset
Ticker states and positions are scoped to the current date. No manual reset needed.
pythondef cleanup_old_records(days_to_keep: int = 30):
    with engine.connect() as conn:
        for table in ("ticker_states", "positions", "alert_log", "position_updates", "ai_decisions"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE date < CURRENT_DATE - :days"),
                {"days": days_to_keep}
            )
        conn.commit()

Environment Variables
DISCORD_TOKEN=
SCANNER_CHANNEL_ID=
SCANNER_BOT_USER_ID=
TRADE_ALERTS_CHANNEL_ID=
ANTHROPIC_API_KEY=
DATABASE_URL=
IBKR_GATEWAY_URL=https://localhost:5000
POLL_INTERVAL_SECONDS=5

Full Flow Summary
Discord message received
        │
        ▼
Parse alert
        │
        ▼
Borrow / whale / halt event?
  YES → update state metadata only, return
  NO  → continue
        │
        ▼
Ticker already TRADE / PASS / DISQUALIFIED?
  YES → ignore, return
  NO  → continue
        │
        ▼
hard_disqualify() — float, MC, price, rv, offering, NBREAK
  FAIL → DISQUALIFIED, return
  PASS → continue
        │
        ▼
Append alert to history
        │
        ▼
should_send_to_ai()
  NO  → WATCHING, return
  YES → continue
        │
        ▼
build_user_message() — alert history + prior AI context if WATCH state
        │
        ▼
Call Claude API
        │
        ▼
Parse JSON response
        │
        ├── TRADE   → state = TRADE
        │             open_position() — creates DB record, sets hard stop
        │             notify Discord — entry, hard stop, trail activation price
        │             scanner ignores this ticker for rest of day
        │             position_monitor_loop() picks it up on next poll cycle
        │
        ├── MONITOR → state = WATCH
        │             store AI context
        │             await next scanner alert
        │
        └── PASS    → state = PASS, loop ends


Position Monitor Loop (runs every 5 seconds, independent of scanner)
        │
        ▼
For each OPEN or TRAIL_ARMED position:
        │
        ▼
get_broker_price() — fetch current price from IBKR CPAPI
        │
        ▼
Calculate gain_pct from entry
        │
        ▼
Update highest_price_seen if new high
        │
        ▼
get_trail_pct(gain_pct) — look up trail % from ladder
calculate_stop() — returns effective stop (higher of hard stop or trail stop)
        │
        ▼
current_price <= stop_level?
  YES → exit_position()
        update DB to EXITED with exit price and final P&L
        notify Discord — exit price, reason, final gain %
  NO  → update DB with new stop level and highest price seen
        continue monitoring

Key Design Decisions
Why the trail widens as the move extends
A stock up 300% has intraday swings of 20–40%. A 10% trail on ASTC at $15 would have stopped out on every normal candle wick. The trail must be proportional to the volatility of the move, which increases with the size of the gain. The ladder is calibrated so the trail is tight enough to protect significant profit but wide enough to survive normal momentum consolidations.
Why hard stop is always active
The trail does not replace the hard stop — it supplements it. Before the trail activates the hard stop at -15% is the only protection. Even after the trail activates the hard stop floor ensures that if the trail calculation somehow results in a level below the hard stop (only possible on extreme gaps), the harder level applies.
Why target_price is retained in the AI output
The AI still calculates a conservative continuation estimate. This is used only for the trade notification to give context — it is not used as an exit level. The position manager ignores it entirely. The actual exit is determined solely by the trail.
Why Python handles the trail, not AI
The trail calculation is pure arithmetic — a lookup table applied to a current price. There is no judgement involved. Putting this in AI would add latency, cost, and potential inconsistency to a decision that should be deterministic and execute in milliseconds.
Why the broker integration is left as NotImplementedError
The IBKR CPAPI implementation is highly environment-specific — it depends on whether you are using paper or live account, how the gateway is configured, and how conids are resolved for the specific stocks. The interface is defined clearly here. The implementation must be completed by the developer against their specific IBKR setup.