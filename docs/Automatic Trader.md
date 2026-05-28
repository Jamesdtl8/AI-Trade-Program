
AI TRADING SANDBOX
Technical Architecture & Integration Specification
Optical Media UK  •  May 2026  •  v1.0

5 Trade Slots
Concurrent positions each £5,000	5–10% Target
Fixed TP range per trade	Sandboxed
Separate T212 account + shared price data

1. PURPOSE & SCOPE

This document defines the technical architecture for an AI-powered trading sandbox that runs independently of the existing production trading system. The sandbox uses a dedicated Trading 212 account, shares the existing Yahoo Finance price data infrastructure, ingests the TrendVision Discord scanner feed, and uses the Claude API to make autonomous trade decisions. All activity is benchmarked against real market conditions with zero interference to production.

GOAL	Capture 5–10% gains per trade across 5 simultaneous slots. Build a benchmark of AI trade quality over 2–4 weeks before any production integration decisions.

2. SYSTEM OVERVIEW

The system consists of six layers that operate in sequence from alert ingestion through to trade execution and position monitoring. Each layer is independently deployable and the sandbox account is completely isolated from the production system.

LAYER	Component	Responsibility
1	Discord Ingest	Connects to TrendVision channel, parses every alert into structured JSON in real time
2	Alert Filter	Hard-kills noise (halts, offerings, large cap, low RV). Classifies news headlines as positive/neutral/negative
3	AI Scorer	Claude API call with chart data + ticker history + pre-signals. Outputs score 0–100 and TRADE/WATCH/SKIP decision
4	Slot Manager	Tracks 5 independent trade slots. Queues alerts when full. Enforces concentration and drawdown rules
5	Position Monitor	Polls yfinance 1m candles for all active slots. Feeds live updates to Claude. Acts on AI exit decisions
6	Trade Logger	Records every decision, entry, exit, P&L to SQLite. Generates daily benchmark report

3. SANDBOX ISOLATION

3.1  Trading 212 Account
A dedicated Trading 212 account is created exclusively for the AI sandbox. It has no connection to any existing production account. The sandbox account:
•	Holds a fixed starting capital ring-fenced from all other funds
•	Has its own API credentials stored separately from production credentials
•	Never receives manual trades — all activity is AI-generated only
•	Is the sole source of truth for P&L benchmarking

3.2  Shared Yahoo Finance Price Data
The existing system already polls Yahoo Finance via yfinance for live price data. Rather than making duplicate API calls, the sandbox reads from a shared in-process price cache:

Component	Implementation
Price cache	Shared dict in memory, keyed by ticker. Both production and sandbox read from it
Cache TTL	30 seconds. Any caller that reads stale data triggers a fresh yfinance pull
1m candles	yfinance.history(period='1d', interval='1m', prepost=True) with shared result
Previous close	hist['Close'].iloc[-1] from 5-day daily history — fixes Monday morning lag bug
API calls saved	Estimated 60–80% reduction vs independent polling per system

NOTE	The price cache is read-only from the sandbox perspective. The sandbox never writes to shared state. If the production system is offline the sandbox falls back to direct yfinance calls automatically.

4. LAYER 1 — DISCORD ALERT INGEST

A Discord bot connects to the TrendVision scanner channel using discord.py. Every message is parsed into a structured alert object within 1–2 seconds of arrival.

4.1  Alert Types Parsed

Type	Trigger Pattern	Output Fields
SCANNER	Ticker + RV + % move	ticker, pct, price, float, rv, rank, indicators, timestamp
FIRE	🔥 + 0 BORROW	ticker, ctb, si, timestamp
WHALE	🐋 + direction	ticker, direction, price, shares, value, timestamp
HALT	🛑 + HALTED	ticker, direction, price, change, volume, timestamp
OFFERING	⚠️ + OFFERING	ticker, headline — immediate discard signal
NEWS	NEWS bullet in alert	ticker, headline, url, age_mins

4.2  Hard Discard Rules (pre-AI)
These checks happen in code before any API call is made:
•	HALT alerts → log only, never trade
•	OFFERING alerts → log only, flag ticker as avoid for 24 hours
•	RV < 10x → discard (no real volume event)
•	Float > 30M → discard (too large for 5–10% momentum capture)
•	Market cap > £200M → discard
•	First alert % > 40% → discard (entry too late for 5–10% TP)
•	Ticker already in 2 active slots → queue only, do not score

5. LAYER 2 — NEWS CLASSIFIER

When a scanner alert carries a news headline, the headline is passed to a lightweight Claude API call (no thinking, Haiku-level logic) to classify it. This is a cheap call — approximately £0.001 per classification.

5.1  Classification Categories

Class	Examples	Effect on Scoring
POSITIVE	Earnings beat, FDA approval, contract win, buyback, partnership	+15 points to AI score. Lowers RV threshold to 5x
NEUTRAL	Quarterly update, investor webinar, analyst note	No score adjustment
NEGATIVE	Going concern, secondary offering, revenue miss, CEO departure	Immediate discard regardless of RV or float
SQUEEZE	Short interest disclosure, borrow rate spike, Reg SHO entry	+20 points. Activates pre-signal watch mode

5.2  News Classification API Call
System prompt sent to Claude (no thinking, max_tokens: 50):

Classify this stock news headline as one of: POSITIVE, NEGATIVE, NEUTRAL, SQUEEZE.
Respond with only the single word. No explanation.
Headline: {headline}

6. LAYER 3 — AI SCORER

When an alert passes the hard filter and has been news-classified, the scorer makes a single Claude API call with medium thinking enabled. This call determines whether to open a trade, and if so, at what levels.

6.1  Scorer Input Context

Context block	Content
Alert data	Ticker, type, %, price, float, RV, indicators, timestamp
Pre-signals	Any FIRE alerts for this ticker in last 4 hours (CTB, SI)
Ticker history	All alerts for this ticker in last 72 hours from SQLite
Live 1m candles	Last 20 candles pulled from shared yfinance cache
HOD / LOD	Calculated from candles — key for break level identification
News class	POSITIVE / NEUTRAL / NEGATIVE / SQUEEZE from Layer 2
Current slots	How many of 5 slots are open, which tickers are active
Slot context	Current P&L on all active positions

6.2  Scorer Output (JSON)

{
  "decision": "TRADE" | "WATCH" | "SKIP",
  "score": 0-100,
  "entry": 6.95,           // break level from chart
  "tp": 7.30,              // 5-10% above entry
  "stop": 6.46,            // recent consolidation low
  "tp_pct": 5.0,           // exactly what % we are targeting
  "stop_pct": 7.1,         // actual stop distance
  "reason": "HOD break, RV 567x, pre-signal 19 mins prior",
  "risk_flags": ["already up 29% on first alert"]
}

COST	Scorer call with medium thinking: ~£0.10 per call. Only triggered on alerts that pass hard filter AND news classification. Estimated 15–25 scorer calls per active trading day.

7. LAYER 4 — SLOT MANAGER

The slot manager maintains the state of all 5 trade slots and enforces capital and risk rules before any trade is opened.

7.1  Slot States

State	Description
OPEN	Available for a new trade. Slot picks next item from queue if one exists
ACTIVE	Trade in progress. Slot is feeding 1m candle updates to the position monitor
COOLING	Just exited (TP, stop, or AI decision). 2-minute hold before returning to OPEN

7.2  Entry Rules
•	Score >= 60 required to consume a slot (WATCH = score 40–59, queued for upgrade)
•	Max 2 slots in the same ticker simultaneously
•	If 3 or more slots are simultaneously in negative P&L → pause all new entries until one closes green
•	Alerts scored while all 5 slots are ACTIVE go into a time-limited queue (10 minute TTL)
•	When a slot opens, take the highest-scored item from queue that still passes freshness check

7.3  Exit Rules
•	TP hit → close immediately, log result, slot to COOLING
•	Stop hit → close immediately, log result, slot to COOLING
•	AI monitor says SELL NOW → close immediately
•	AI monitor says TIGHTEN STOP → update stop level in slot state
•	AI monitor says SCALE OUT 50% → close half position, keep slot ACTIVE with reduced size
•	15:55 ET hard close → force-close all ACTIVE slots regardless of P&L, no exceptions

8. LAYER 5 — POSITION MONITOR

Once a trade is open the position monitor maintains a persistent Claude conversation for that slot, feeding it 1m candle updates every 30 seconds. This is the core of the AI decision loop.

8.1  Monitor Conversation Structure

Message	Content
System prompt (cached)	Role definition + allowed responses: HOLD, SELL NOW, TIGHTEN STOP, SCALE OUT 50%
Message 1 (cached)	Trade setup: ticker, entry, TP, stop, capital, reason, pre-signals, scanner alert history
Response 1	AI acknowledgement: monitoring confirmed
Messages 2–N (fresh)	Price update: current price, vs entry %, vs TP %, unrealised P&L, last 5 candles, volume trend
Response 2–N	Single-line AI decision: HOLD / SELL NOW / TIGHTEN STOP to $X.XX / SCALE OUT 50%

8.2  Thinking Strategy
Thinking is selectively enabled to control API cost:

Call type	Thinking	Cost (approx)
News classification	Disabled	£0.001
Alert scorer (entry decision)	Medium (8k tokens)	£0.10
Position monitor (routine update)	Disabled	£0.001
Exit decision (SELL NOW candidate)	Medium (8k tokens)	£0.10
EOD summary	Disabled	£0.002

COST	Estimated total API cost per trading day: £2–5 at normal activity levels (15–25 scored alerts, 3–5 trades open for average 45 minutes each). This is the corrected figure using prompt caching on the trade setup block.

8.3  Adaptive Polling
Polling frequency adjusts based on price activity to reduce unnecessary API calls:
•	Price change < 1% over last 5 candles → poll every 60 seconds
•	Price change 1–3% → poll every 30 seconds
•	Price change > 3% or approaching TP/stop within 1% → poll every 10 seconds
•	Within 5 minutes of EOD (15:50–15:55 ET) → poll every 10 seconds regardless

8.4  Context Compression
To prevent context window growth the conversation is compressed every 20 updates:
•	A summary call asks Claude to distill the trade history into 3 bullet points
•	Conversation is reset to: system prompt + original trade setup + compressed summary
•	Compression cost: one extra call at £0.002

9. LAYER 6 — TRADE LOGGER & BENCHMARK

Every decision the AI makes is logged regardless of outcome. The goal is a clean benchmark dataset after 2–4 weeks of live running.

9.1  Database Schema (SQLite)

Table	Key fields
alerts	ticker, type, pct, rv, float, indicators, news_class, timestamp, raw_message
scores	ticker, score, decision, entry, tp, stop, reason, risk_flags, thinking_used, timestamp
trades	slot, ticker, entry, tp, stop, capital, entry_time, exit_price, exit_time, exit_reason, pnl_pct, pnl_gbp
monitor_log	trade_id, candle_time, price, unrealised_pct, ai_decision, raw_response
daily_summary	date, trades, wins, losses, win_rate, total_pnl, avg_win_pct, avg_loss_pct, api_cost

9.2  Benchmark Metrics Tracked
•	Win rate overall and by setup type (FIRE pre-signal vs cold breakout vs news catalyst)
•	Average actual gain on winners vs target 5–10%
•	Average loss on losers vs expected ~8%
•	Slot utilisation rate (how often all 5 slots are active simultaneously)
•	Queue overflow rate (alerts discarded because slots were full)
•	AI scorer accuracy: % of TRADE decisions that resulted in at least 5% move
•	Best and worst performing scanner alert types (RV bands, indicator combos)
•	API cost per trade and per profitable pound earned

10. INTEGRATION WITH EXISTING SYSTEM

The sandbox is designed to run alongside the existing production system with no shared state except the price cache. The integration points are minimal and one-way.

Component	Direction	Notes
yfinance price cache	Production → Sandbox (read-only)	Sandbox reads, never writes. Falls back to direct yfinance if cache unavailable
Discord bot	Shared channel, separate bot instance	Both can read TrendVision channel independently or sandbox reuses existing bot
Trading 212 API	Sandbox only	Dedicated credentials, dedicated account. Zero overlap with production T212
SQLite database	Sandbox only	Separate DB file. No schema overlap with any production database
Claude API key	Shared Anthropic account	Usage tracked separately via metadata tags on each call (tag: sandbox_trading)

11. TECHNOLOGY STACK

Layer	Technology	Notes
Discord ingest	discord.py	Existing pattern, new bot token for sandbox
Alert parser	Python regex + dataclasses	Parses TrendVision message format into typed objects
News classifier	Claude Haiku (no thinking)	~£0.001/call, fast classification only
AI scorer	Claude Sonnet 4.6 (medium thinking)	~£0.10/call, full context reasoning
Position monitor	Claude Sonnet 4.6 (no thinking)	~£0.001/call with caching, 30s polling
Price data	yfinance + shared cache	period='1d', interval='1m', prepost=True
Previous close fix	history(period='5d')['Close'].iloc[-1]	Avoids Monday morning lag bug
Trade execution	Trading 212 REST API	Sandbox account only
Storage	SQLite	Single file, no server required
Scheduler	APScheduler	EOD close at 15:55 ET, daily report at 16:30 ET
Runtime	Python 3.11+	Runs on existing infrastructure

12. ROLLOUT PLAN

Phase	Duration	Scope
Phase 1	Week 1	Ingest + filter + scorer running. All decisions logged but NO trades executed. Pure observation to validate scoring logic.
Phase 2	Weeks 2–3	Paper trading mode. Trades executed against sandbox T212 account with real prices but review all AI decisions manually each day.
Phase 3	Week 4+	Fully autonomous. AI opens and closes all positions. Daily P&L review only. Benchmark report at end of month.
Review gate	End of month	Analyse benchmark data. Decide whether to increase slot capital, adjust TP targets, or refine filter rules.

13. RISK CONTROLS

HARD LIMIT	Maximum daily loss across all 5 slots: £1,000. If total drawdown hits this level the system auto-halts all activity until next trading day. This is enforced in code, not reliant on the AI.

13.1  Position-Level Controls
•	Stop loss: AI-determined per trade (typically 7–10% based on chart structure)
•	Maximum single position: £5,000 — hard coded, not adjustable by AI
•	No overnight holds: EOD close at 15:55 ET is a hard system rule
•	No pre-market entries until 09:25 ET (5 minutes before open)

13.2  System-Level Controls
•	Daily loss limit: £1,000 across all slots → halt
•	3 consecutive losing trades → pause new entries for 30 minutes
•	3+ slots simultaneously in red → pause new entries
•	Trading 212 API error or connectivity loss → halt all new entries, hold existing
•	Claude API failure → hold all positions, no new entries, alert via log

DOCUMENT END

This document is the technical specification for internal development use only.
Optical Media UK  •  AI Trading Sandbox v1.0  •  May 2026

