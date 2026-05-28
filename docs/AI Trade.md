# AI Trade — Build & Runbook

Optical Media UK · v2.0 · May 2026

This document supersedes the original *Automatic Trader.md* spec. Use this one as
the source of truth for how the AI sandbox actually runs in production.

---

## 1. What it does

A self-contained AI trader that lives inside the existing Pulse dashboard
process. It runs **alongside** the Gemini-driven main trader but on a
**separate Trading 212 account** (`$50,000` paper-money sandbox), and never
touches the existing bot's state, orders or watchlist.

- 5 concurrent trade slots, **£5,000 each** (≈$25k of the $50k account deployed).
- Targets **5–10 % moves** on TrendVision scanner alerts.
- Always on — pre-market, regular session **and** after-hours.
- No daily loss cap, no EOD close. If the account blows up it blows up (paper).
- Claude end-to-end (Haiku for cheap, Sonnet 4.5 for the work).

## 2. File map

```
ai_sandbox/
├── __init__.py
├── config.py             # env keys, Anthropic models, slot/risk constants
├── db.py                 # SQLite schema (alerts, scores, trades, monitor_log)
├── alert_parser.py       # TrendVision regex → typed Alert objects
├── alert_filter.py       # hard discard rules (halts/offerings/RV/float/mcap/%)
├── scanner_feed.py       # tails ai_scanner_feed.jsonl written by the relay
├── price_data.py         # 1m candles + prev close, calls dashboard's shared yfinance cache
├── claude.py             # Anthropic client (news classify, score, monitor) + prompt caching
├── slot_manager.py       # 5 slots, queue, ticker concentration
├── position_monitor.py   # 10 s candle loop per ACTIVE slot, AI decisions
├── t212_ai.py            # T212 REST calls scoped to the AI account credentials
├── engine.py             # async orchestrator wiring all of the above
├── service.py            # entrypoint called from Main_Website/app.py at boot
└── data/
    ├── sandbox.db        # SQLite — own schema, own file
    └── scanner_feed.jsonl # written by Discord/Discord_Relay, read by scanner_feed.py
```

Shared with the main system (read-only):

- `Main_Website/app.py::_cached_yahoo_history` and `_yahoo_fast_quote_batch`
  — the AI's `price_data.py` imports these directly. No second yfinance poller.
- `Discord/Discord_Relay/discord_socket_relay.py` — extended (not duplicated) so the
  scanner channel(s) in `AI_SCANNER_CHANNEL_IDS` get appended to
  `ai_sandbox/data/scanner_feed.jsonl` instead of going over Socket.IO. The
  existing Socket.IO traffic for `DISCORD_CHANNEL_ID` is untouched.

## 3. Environment

Repo-root `.env`:

| Key | Purpose |
|---|---|
| `TRADING_212_KEY_AI` / `TRADING_212_SECRET_AI` | AI account API key + secret |
| `T212_ENV_AI` | `demo` (default) or `live` |
| `ANTHROPIC_API_KEY` | Claude API key |
| `ANTHROPIC_MODEL_SCORER` | Default `claude-sonnet-4-5` (medium thinking) |
| `ANTHROPIC_MODEL_MONITOR` | Default `claude-sonnet-4-5` (no thinking, cached prompt) |
| `ANTHROPIC_MODEL_NEWS` | Default `claude-haiku-4-5` (no thinking) |
| `AI_TRADING_ENABLED` | `1` = live trading, `0` = kill switch (engine stays up, no orders) |
| `AI_SCANNER_CHANNEL_IDS` | CSV of Discord channel IDs that feed the AI scanner JSONL |

## 4. Layers

### 4.1 Discord scanner ingest
`Discord/Discord_Relay/discord_socket_relay.py` is extended:
- The set of channels in `AI_SCANNER_CHANNEL_IDS` is added to the Discord
  client's listen set.
- For those channels, messages **do not** broadcast on Socket.IO. Each is
  written as one JSON line to `ai_sandbox/data/scanner_feed.jsonl` (created
  lazily). A `.pos` sidecar file tracks how many bytes the AI engine has
  consumed.
- Existing channels in `DISCORD_CHANNEL_ID` continue to go over Socket.IO
  exactly as today.

### 4.2 Alert parser + hard filter
`alert_parser.py` handles every TrendVision shape: SCANNER, FIRE, WHALE,
HALT, OFFERING, NEWS bullets, price-change rebroadcasts.

`alert_filter.py` (context-aware, post day-1 back-test rewrite). For every
incoming alert we first compute a **per-ticker context** from the DB:

- `prev_pct`, `prev_rv`, `prev_alert_age_s` — last SCANNER reading for this ticker
- `rv_growth`, `pct_jump` — deltas vs `prev_*`
- `scanner_alerts_5m` — count of SCANNER pings in last 5 minutes
- `halt_count_60m`, `recently_halted` (last halt ≤ 30 min)
- `fast_mover` = (prev alert ≤ 10 min ago) AND (rv ≥ 5× growth) AND (pct jump ≥ 15 pp)
- `news_in_history`, `whale_count_60m`

Filter decisions:

| Rule | Outcome |
|---|---|
| HALT | log, never trade (but `recently_halted` flag survives for the next SCANNER alert) |
| OFFERING | log, block ticker for 24 h |
| Ticker already in 2 active slots | reject |
| FIRE / WHALE | pass (low-risk; scorer decides) |
| Float > 30 M | reject |
| Market cap > $250 M | reject |
| **pct ceiling — DEFAULT** | 30 % (tightened from 40 %) |
| **pct ceiling — ELEVATED** | 60 % when `fast_mover` OR `recently_halted` OR (`news` AND RV ≥ 100×) |
| **RV floor — DEFAULT** | 10× |
| **RV floor — with POSITIVE news** | 5× |
| **RV floor — fast_mover** | bypassed (the RV growth IS the liquidity signal) |

### 4.3 News classifier (Claude Haiku)
When a scanner alert carries a news bullet:
- One Claude Haiku call, `max_tokens: 5`, no thinking.
- Returns `POSITIVE | NEUTRAL | NEGATIVE | SQUEEZE`.
- `NEGATIVE` → instant discard regardless of RV/float.
- `POSITIVE` → +15 to score, RV threshold drops to 5×.
- `SQUEEZE` → +20 to score.

### 4.4 Scorer (Claude Sonnet 4.5, medium thinking)
One call per surviving alert. Inputs: alert payload, ticker's last-72 h alert
history, last 20 × 1 m candles + HOD/LOD from the shared cache, news class,
current slot state. Output JSON:

```json
{ "decision": "TRADE|WATCH|SKIP", "score": 0-100,
  "entry": 6.95, "tp": 7.30, "stop": 6.46,
  "tp_pct": 5.0, "stop_pct": 7.1,
  "reason": "...", "risk_flags": [] }
```

`score ≥ 60` consumes a slot (`WATCH` = 40–59, queued). The full per-ticker
context (above) is passed to Claude so it can reason about cadence, halt
history and fast-mover momentum.

### 4.4a Confirmation gate (5 minute)
TRADE calls with score `60–79` do NOT execute immediately. They wait up to
5 minutes for one follow-up SCANNER alert on the same ticker at ≥ (entry pct
− 2 pp). When confirmed → execute. On timeout → drop. This skips automatically
when the alert was already strongly confirmed (fast_mover, recently_halted,
or scanner_alerts_5m ≥ 2). Score ≥ 80 always executes immediately (HIGH
CONVICTION reserved for textbook setups).

### 4.5 Slot manager
5 slots, each fixed at £5,000. States: `OPEN → ACTIVE → COOLING (2 min) → OPEN`.

Rules:
- Max 2 slots in the same ticker simultaneously.
- If 3+ slots are simultaneously negative P&L → pause new entries until one closes green.
- Queue TTL 10 min; on free slot pick the highest score still fresh.

### 4.6 Position monitor (Claude Sonnet 4.5, prompt-cached)
Once a slot is ACTIVE, a persistent Claude conversation is opened for that
slot:
- System prompt + initial trade setup are marked `cache_control: ephemeral`
  so every follow-up reuses Anthropic's prompt cache (≈10× cheaper).
- Poll every 10 s. Snapshot includes mode + age + running high + vs-stop /
  vs-tp / vs-running-high. Claude replies with one of:
  `HOLD | SELL NOW | TIGHTEN STOP <price> | SCALE OUT 50%`.

#### Two operating modes:

| Mode | When | Exit rule |
|---|---|---|
| **fixed-tp** (default at entry) | Always start here | Hit static TP → exit. Hit stop → exit. Claude may TIGHTEN/SCALE/SELL. |
| **trail** | Auto-switched when unreal P&L ≥ +5 % within first 30 min of trade | Engine moves stop to break-even, then trails 3 % below the running high. Claude is told **not** to call TIGHTEN STOP — it focuses on calling SELL NOW only on confirmed structure breaks. Lets winners run past the original TP. |

This is the key answer to "we left 90 % of the EZGO move on the table":
trail mode replaces the static 10 % TP with an AI-managed exit on the few
trades that actually run hard.

### 4.7 Trade execution
Reuses `Trading_AI/t212.py` for the HTTP layer but with **separate
credentials and base URL** resolved in `ai_sandbox/config.py`. All AI orders
go through `Trading_AI.t212` calls but with `extendedHours: true` so they
can fill pre- and after-market.

If `AI_TRADING_ENABLED=0`, the engine still parses alerts, scores them and
keeps slot state, but `t212_ai.place_*` short-circuits to a no-op + journal
entry. Use this when you want to observe scoring quality without filling.

### 4.8 Trade logger / DB
SQLite at `ai_sandbox/data/sandbox.db`:

| Table | Columns |
|---|---|
| `alerts` | id, ts, ticker, type, raw, parsed_json, news_class |
| `scores` | id, alert_id, ts, score, decision, entry, tp, stop, reason, risk_flags, thinking_used |
| `trades` | id, slot, ticker, entry_price, tp, stop, capital_gbp, open_ts, exit_price, exit_ts, exit_reason, pnl_pct, pnl_gbp |
| `monitor_log` | id, trade_id, ts, price, unreal_pct, ai_decision, raw_response |
| `daily_summary` | date, trades, wins, losses, win_rate, total_pnl, api_cost |

## 5. Dashboard page (`AI Trade` tab)

Lives at `#page-ai-trade` in `Main_Website/tradingserver.html`. Identical
visual language to the rest of the dashboard (uses `--bg2`, `--green`,
`--green-dim`, IBM Plex Mono numerics, etc.).

Layout (top→bottom):
1. Engine status strip — engine ON/OFF, T212 env, account cash, today's P&L,
   API spend, kill-switch toggle.
2. 5 slot cards — each shows ticker, entry/TP/stop, live price, unrealised
   P&L, age, last AI decision.
3. Queue panel — pending alerts waiting for an open slot.
4. Scanner feed — last 50 raw TrendVision lines with parsed tags + news class.
5. Trade history table — every closed AI trade with reason, P&L, AI decision
   trail (clickable).

All powered by these endpoints (added to `Main_Website/app.py`):

```
GET  /api/ai/status            — engine status, cash, today P&L
GET  /api/ai/slots             — current 5 slots + queue
GET  /api/ai/feed?limit=50     — recent scanner messages (raw + parsed)
GET  /api/ai/trades?limit=100  — closed trade history
GET  /api/ai/monitor/<trade_id> — full decision trail for one trade
POST /api/ai/toggle            — flip AI_TRADING_ENABLED at runtime
```

## 6. Deploy

This box already serves the site via systemd → nginx. Workflow:

```bash
# 1. .env has been updated (no commit needed — repo isn't git-tracked here).
# 2. Restart the trading site + relay so new code + env are picked up.
sudo systemctl restart discord-socket-relay.service
sudo systemctl restart trading-website.service   # or whichever unit runs app.py
journalctl -u trading-website.service -f | grep -i ai_sandbox
```

The AI engine is started as a background asyncio task inside `app.py`'s
process; there is **no separate systemd unit**. This keeps the in-process
yfinance cache shared with the dashboard for free.

## 7. Costs (rough)

Per active trading day, with prompt caching enabled:

| Call | Volume / day | Unit | Total |
|---|---|---|---|
| News classify (Haiku) | ~50 | £0.001 | £0.05 |
| Scorer (Sonnet, thinking) | ~25 | £0.10 | £2.50 |
| Monitor poll (Sonnet, cached) | ~1,500 (5 slots × 10 s × ~50 min) | £0.0005 | £0.75 |
| Exit / scale candidate decision | ~10 | £0.10 | £1.00 |
| Daily summary | 1 | £0.002 | £0.002 |
| **Total** | | | **≈ £4–5 / day** |

## 8. Open items (post-launch)

- Detect cash < £25k and refuse to open new slots.
- Per-trade journal export (CSV) on the AI Trade page.
- Compare AI win rate vs main bot's Gemini win rate over the same window.
