# Trading Bot — Technical Specification
## For Developer Handoff

---

## Overview

A Python-based automated trading bot that:
1. Listens to a Discord channel via websocket in real time
2. Passes each message to Gemini Flash Lite for trade signal parsing
3. Executes orders on Trading 212 via their REST API
4. Manages the full trade lifecycle — entry, stop loss, and exit

---

## Architecture

```
Discord Gateway (websocket)
        ↓
Message Queue (single threaded)
        ↓
Gemini Flash Lite (signal parser)
        ↓
Order Manager
        ↓
Trading 212 REST API
```

---

## 1. Discord Listener

### Library
Use `discord.py` or raw websocket connection to Discord Gateway.

### What to listen to
- Connect to the specific Discord server and channel where the trader posts
- Capture every message in real time with its full timestamp
- Only process messages from a specific user ID (the trader) — ignore all others

### Message buffer
Maintain a rolling buffer of the last 20 messages from today only. This is passed as `context` to Gemini with every call. Clear the buffer at midnight or market open.

### Implementation
```python
import discord

client = discord.Client()

@client.event
async def on_message(message):
    if message.author.id != TRADER_USER_ID:
        return
    if message.channel.id != TARGET_CHANNEL_ID:
        return
    
    await message_queue.put({
        'content': message.content,
        'timestamp': message.created_at.isoformat()
    })
```

---

## 2. Message Queue — Single Threaded Processing

### Critical rule
**Process one message at a time. Do not process the next message until the current one is fully resolved — Gemini response received AND Trading 212 order confirmed.**

### Why
Prevents race conditions where two entry signals fire simultaneously, or an exit fires before the stop loss is cancelled.

### Implementation
```python
import asyncio

message_queue = asyncio.Queue()

async def process_queue():
    while True:
        message = await message_queue.get()
        await handle_message(message)  # blocks until complete
        message_queue.task_done()
```

### SL hold window
When an entry signal is detected, start a 30-second timer before firing the order. If a stop loss message arrives within 30 seconds, use the stated SL. If no SL arrives within 30 seconds, proceed with the 15% default SL.

```python
async def wait_for_sl(ticker, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        # Check if a new message arrived with SL for this ticker
        if sl_received_for(ticker):
            return get_sl_for(ticker)
        await asyncio.sleep(0.5)
    return None  # use default 15%
```

---

## 3. State Management

### Active trade state
Store in memory (and persist to a local JSON file for crash recovery):

```python
state = {
    "pot_available": True,
    "balance": 10000.00,
    "active_trade": {
        "ticker": "FCHL",
        "entry_type": "normal",          # "normal" or "break"
        "raw_price": 0.62,
        "limit_price": 0.629,            # our entry price
        "stop_loss": 0.54,               # trader stated SL — used by candle monitor only
        "default_sl": False,
        "hard_stop_price": 0.476,        # 25% below entry — emergency backstop T212 order
        "requested_quantity": 15898,     # what we asked for
        "filled_quantity": 15898,        # what we actually got — use for ALL sell orders
        "filled": False,
        "entry_order_id": None,          # T212 entry order ID
        "hard_stop_order_id": None,      # T212 hard stop at 25% order ID
        "limit_sell_order_id": None,     # T212 limit sell order ID
        "candle_monitor_task": None,     # asyncio task reference
        "opened_at": "2026-04-20T13:50:46Z"
    }
}
```

### Key state rules
- `stop_loss` — updated when trader posts new SL. Only used by candle monitor, not a T212 order.
- `hard_stop_price` — set once at entry (25% below entry), never changes, always a live T212 stop order.
- `filled_quantity` — always use this for sell orders, never `requested_quantity`.
- `limit_sell_order_id` — automatic upside exit. Fires without any message processing needed.
```

### Persist to disk
Write state to `state.json` after every change. On startup, load from file to recover from crashes.

### Message context buffer
```python
context_buffer = []  # list of {time, msg} dicts, today only

def add_to_context(message):
    context_buffer.append({
        "time": message['timestamp'],
        "msg": message['content']
    })
    # Keep last 20 messages only
    if len(context_buffer) > 20:
        context_buffer.pop(0)
```

---

## 4. Gemini Integration

### Model
`gemini-2.0-flash-lite` via Google Generative AI API

### When to call
Every message that arrives from the trader — no pre-filtering. Let Gemini decide if it's actionable. The model is fast and cheap enough to call on every message.

### System prompt (send on every call)
```
You are a trading signal parser. You receive live messages from a trader and must decide what action to take. Respond ONLY with valid JSON. No explanation. No extra text. Your response will be parsed and executed immediately by the order system.

RULES:
1. EXCLUDE if message contains: gamble, risky, riskier, pure gamble, chinese
2. ENTRY: @everyone + TICKER + PRICE = new trade signal
3. BREAK entries: stop limit order. Stop trigger at exact break price. Limit ceiling at break price +1%. No 1.5% added.
4. NORMAL entries: limit buy at 1.5% above stated price. This is the maximum fill price.
5. STOP LOSS: use trader stated SL if given. If none, default to 15% below entry price. Flag default_sl as true.
6. EXIT: trigger on first message containing Trim, Scaled, NHOD, All out + a price — market sell at that price. Taps/TAPS does not trigger a sell.
7. ONE trade at a time. If pot is occupied, ignore new entry signals
8. Update SL in real time if trader posts new SL for active trade
9. Quantity = floor(10000 / limit_price) for normal entries, floor(10000 / stop_price) for break entries

ACTIONS: PLACE_LIMIT_BUY, PLACE_STOP_LIMIT, PLACE_MARKET_SELL, CANCEL_AND_SELL, UPDATE_SL, IGNORE
```

### User message structure (build this on every call)
```python
def build_gemini_payload(new_message, state, context_buffer):
    return {
        "pot": {
            "available": state['pot_available'],
            "balance": state['balance'],
            "active_trade": state['active_trade']
        },
        "context": context_buffer[-15:],  # last 15 messages
        "new_message": {
            "time": new_message['timestamp'],
            "msg": new_message['content']
        }
    }
```

### API call
```python
import google.generativeai as genai
import json

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-lite')

async def call_gemini(payload):
    response = model.generate_content(
        json.dumps(payload),
        generation_config=genai.GenerationConfig(
            temperature=0.1,
            max_output_tokens=300
        )
    )
    return json.loads(response.text)
```

---

## 5. Order Execution — Trading 212

### Base URLs
- **Paper trading (test):** `https://demo.trading212.com/api/v0`
- **Live trading:** `https://live.trading212.com/api/v0`

### Authentication
Basic Auth — Base64 encode `API_KEY:API_SECRET`

```python
import base64
import httpx

credentials = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
headers = {"Authorization": f"Basic {credentials}"}
```

### Important
Trading 212 API is **Invest and ISA accounts only**. CFD not supported.
Sell orders use **negative quantity** (e.g. `-387`).

---

## 6. Order Flow

### A — Normal Entry (PLACE_LIMIT_BUY)

**Step 1: Place limit buy with max quantity fallback**
```python
async def place_entry_order(ticker, quantity, limit_price):
    response = await t212.post('/equity/orders/limit', {
        "ticker": ticker,
        "quantity": quantity,
        "limitPrice": limit_price,
        "timeValidity": "DAY"
    })

    if response.status_code == 400:
        error = response.json()
        if error['type'] == '/api-errors/max-position-quantity-exceeded':
            # Extract max allowed quantity from error detail
            max_qty = float(error['detail'].split('is ')[-1])
            log(f"Max quantity exceeded — retrying with max: {max_qty}")

            # Retry once with max allowed quantity
            response = await t212.post('/equity/orders/limit', {
                "ticker": ticker,
                "quantity": max_qty,
                "limitPrice": limit_price,
                "timeValidity": "DAY"
            })

            if response.status_code != 200:
                log("Retry also failed — abandoning trade")
                mark_pot_free()
                return None

        else:
            log(f"Order rejected: {error['detail']}")
            mark_pot_free()
            return None

    order = response.json()
    state['active_trade']['entry_order_id'] = order['id']
    state['active_trade']['requested_quantity'] = quantity
    save_state()
    return order
```

**Step 2: Wait for fill — poll until FILLED, PARTIALLY_FILLED or REJECTED**
```python
async def wait_for_fill(order_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        order = await t212.get(f'/equity/orders/{order_id}')
        status = order['status']

        if status == 'FILLED':
            return order['filledQuantity']

        elif status == 'PARTIALLY_FILLED':
            fill_pct = order['filledQuantity'] / order['quantity']
            if fill_pct >= 0.5:
                # Accept partial fill above 50% — proceed with actual quantity
                log(f"Partial fill: {order['filledQuantity']} of {order['quantity']} shares")
                return order['filledQuantity']

        elif status == 'REJECTED':
            log(f"Order rejected at fill stage: {order_id}")
            mark_pot_free()
            return None

        await asyncio.sleep(2)

    # Timeout — cancel and abandon
    await t212.delete(f'/equity/orders/{order_id}')
    log("Fill timeout — order cancelled")
    mark_pot_free()
    return None
```

**Step 3: Once filled — place THREE orders using actual filledQuantity**

Always use `filledQuantity` not the originally requested quantity for all subsequent orders.

Hard stop backstop (emergency 25% protection — fires on any tick):
```
POST /api/v0/equity/orders/stop
{
  "ticker": "FCHL_US_EQ",
  "quantity": -<filledQuantity>,
  "stopPrice": limit_price * 0.75,   # 25% below entry
  "timeValidity": "DAY"
}
→ store response.id as hard_stop_order_id
```

Limit sell (upside profit target — fires automatically, no latency):
```
POST /api/v0/equity/orders/limit
{
  "ticker": "FCHL_US_EQ",
  "quantity": -<filledQuantity>,
  "limitPrice": limit_price * 1.10,  # 10% target — configurable
  "timeValidity": "DAY"
}
→ store response.id as limit_sell_order_id
```

Candle SL monitor (primary stop — fires on 1-min candle close below stated SL):
```python
# Run as background async task — not a T212 order
asyncio.create_task(monitor_candle_sl(ticker, stated_sl))
```

**Important:** The candle SL monitor and hard stop serve different purposes:
- **Candle SL monitor** → respects the trader's intention (1-min candle must close below SL)
- **Hard stop at 25%** → emergency backstop only, catches catastrophic drops, halts, gaps

---

### B — Break Entry (PLACE_STOP_LIMIT)

**Step 1: Place stop limit buy with max quantity fallback**
```python
async def place_break_entry(ticker, stop_price, limit_price, quantity):
    response = await t212.post('/equity/orders/stop_limit', {
        "ticker": ticker,
        "quantity": quantity,
        "stopPrice": stop_price,           # trigger at exact break price
        "limitPrice": stop_price * 1.01,   # ceiling = break price + 1%
        "timeValidity": "DAY"
    })

    if response.status_code == 400:
        error = response.json()
        if error['type'] == '/api-errors/max-position-quantity-exceeded':
            max_qty = float(error['detail'].split('is ')[-1])
            log(f"Max quantity exceeded — retrying with max: {max_qty}")
            response = await t212.post('/equity/orders/stop_limit', {
                "ticker": ticker,
                "quantity": max_qty,
                "stopPrice": stop_price,
                "limitPrice": stop_price * 1.01,
                "timeValidity": "DAY"
            })
            if response.status_code != 200:
                mark_pot_free()
                return None
        else:
            mark_pot_free()
            return None

    order = response.json()
    state['active_trade']['entry_order_id'] = order['id']
    save_state()
    return order
```

**Step 2 onwards: Same as normal entry — poll for fill, then place hard stop + limit sell + start candle monitor**

---

### C — Candle SL Monitor (Primary Stop Loss)

Runs as a background async task from the moment the entry fills.
Checks the last **closed** 1-minute candle every 10 seconds via Yahoo Finance.
**Always use `data.iloc[-2]`** — `iloc[-1]` is the current forming candle, not yet closed.

```python
async def monitor_candle_sl(ticker, stated_sl):
    while state['active_trade'] is not None:
        data = yf.download(ticker, period="1d", interval="1m", progress=False)

        if len(data) < 2:
            await asyncio.sleep(10)
            continue

        last_closed_candle = data.iloc[-2]  # last fully closed 1-min candle

        if last_closed_candle['Close'] < stated_sl:
            log(f"Candle closed below SL {stated_sl} — firing market sell")

            # Cancel limit sell and hard stop first
            await cancel_order(state['active_trade']['limit_sell_order_id'])
            await cancel_order(state['active_trade']['hard_stop_order_id'])

            # Market sell
            await place_market_sell(ticker, state['active_trade']['filled_quantity'])
            break

        await asyncio.sleep(10)
```

---

### D — Exit Triggered by Gemini (PLACE_MARKET_SELL)

Fires when trader posts explicit red exit ("All out [TICKER] red") with no price.

```python
async def gemini_exit(ticker):
    # Cancel both standing orders first
    await cancel_order(state['active_trade']['limit_sell_order_id'])
    await cancel_order(state['active_trade']['hard_stop_order_id'])

    # Stop candle monitor
    candle_monitor_task.cancel()

    # Market sell
    await t212.post('/equity/orders/market', {
        "ticker": ticker,
        "quantity": -state['active_trade']['filled_quantity']
    })
```

---

### E — One of the Sell Orders Fills Naturally

Poll `GET /api/v0/equity/positions` every 2 seconds while in a trade.

When ticker disappears from positions:
```python
async def monitor_position(ticker):
    while True:
        positions = await get_positions()
        if ticker not in [p['ticker'] for p in positions]:
            # Position closed naturally (limit sell or hard stop triggered)
            # Cancel whichever order didn't fill
            await cancel_order(state['active_trade']['limit_sell_order_id'])
            await cancel_order(state['active_trade']['hard_stop_order_id'])

            # Stop candle monitor
            candle_monitor_task.cancel()

            # Calculate and log P&L
            log_trade_result()
            clear_active_trade()
            break
        await asyncio.sleep(2)
```

---

### F — Update Stop Loss (UPDATE_SL)

When trader posts a new SL during an active trade:

```python
async def update_stop_loss(ticker, new_sl):
    # Update candle monitor target — no T212 order change needed
    # The hard stop at 25% stays unchanged
    state['active_trade']['stop_loss'] = new_sl
    state['active_trade']['default_sl'] = False
    save_state()
    log(f"SL updated to {new_sl} — candle monitor will use new level")
```

Note: The stated SL is only used by the candle monitor, not a T212 order. So updating it is just a state change — no API calls needed. The hard stop at 25% never changes.

---

## 7. Ticker Format

Trading 212 uses a specific ticker format — not just the raw symbol.
`FCHL` becomes `FCHL_US_EQ` for US equities.

**Before going live:** Call `GET /api/v0/equity/metadata/instruments` to build a lookup table mapping raw tickers to T212 format. Store this as a local dict and refresh daily at market open.

```python
async def build_ticker_map():
    instruments = await get_instruments()
    ticker_map = {}
    for inst in instruments:
        # e.g. "FCHL_US_EQ" → extract "FCHL"
        raw = inst['ticker'].split('_')[0]
        ticker_map[raw] = inst['ticker']
    return ticker_map
```

---

## 8. Error Handling

### T212 API Error Types
| HTTP Status | Error Type | Action |
|-------------|------------|---------|
| 404 | `entity-not-found` — ticker not on T212 | Skip trade, log ticker as unavailable, mark pot free |
| 400 | `max-position-quantity-exceeded` | Retry once with max quantity parsed from error detail |
| 400 | `insufficient-funds` | Skip trade, log, mark pot free |
| 400 | `market-closed` | Skip trade, log |
| 429 | Rate limit | Back off 2 seconds, retry |
| 408 | Timeout | Retry once, then abandon |
| 401/403 | Auth failure | Alert immediately, pause bot |

### General Scenarios
| Scenario | Action |
|----------|---------|
| Gemini API timeout | Retry once. If fails again, IGNORE message, log error |
| Max quantity exceeded | Retry with max allowed from error detail — once only |
| Partial fill < 50% | Cancel remaining order, mark pot free, do not proceed |
| Partial fill >= 50% | Accept fill, use filledQuantity for all subsequent orders |
| Fill never confirmed after 30s | Cancel entry order, mark pot as free, log |
| Position disappears without known exit | Cancel all standing orders, log as unknown close, mark pot free |
| Duplicate order non-idempotent API | Check pending orders before placing, cancel dupes |
| Network dropout | On reconnect, poll positions to reconcile state vs state.json |
| Candle monitor loses Yahoo connection | Fall back to hard stop at 25% only, log warning |
| Both limit sell and hard stop try to fire | First cancel wins — always cancel the other immediately after position closes |
---

## 9. Rate Limits to Respect

| Endpoint | Limit |
|----------|-------|
| Stop limit orders | 1 req / 2s |
| Limit orders | 1 req / 2s |
| Market orders | 50 req / 1min |
| Cancel orders | 50 req / 1min |
| Get positions | 1 req / 1s |
| Max pending orders | 50 per ticker |

Add a small delay between sequential API calls to stay safe.

---

## 10. Configuration File

```python
# config.py
DISCORD_TOKEN = ""
TRADER_USER_ID = 0
TARGET_CHANNEL_ID = 0

GEMINI_API_KEY = ""

T212_API_KEY = ""
T212_API_SECRET = ""
T212_ENV = "demo"  # "demo" or "live"

POT_SIZE = 10000.00
LIMIT_SELL_TARGET_PCT = 0.10   # 10% profit target — pre-set limit sell
DEFAULT_SL_PCT = 0.15          # 15% stop loss if none stated by trader
HARD_STOP_PCT = 0.25           # 25% emergency backstop — T212 stop order
BREAK_ENTRY_RANGE_PCT = 0.01   # 1% ceiling above break price
NORMAL_ENTRY_DELAY_PCT = 0.015 # 1.5% above stated price
SL_WAIT_WINDOW_SECONDS = 30    # wait for stated SL before using default
CANDLE_CHECK_INTERVAL = 10     # seconds between candle SL checks
PARTIAL_FILL_THRESHOLD = 0.50  # accept partial fills above this %
FILL_TIMEOUT_SECONDS = 30      # abandon unfilled orders after this
```

---

## 11. Logging

Log every event to a local file and optionally a Discord DM or Telegram notification:
- Every Gemini response received
- Every order placed (with order ID)
- Every fill confirmed
- Every exit (with P&L calculated)
- Every error

```python
import logging
logging.basicConfig(
    filename='Trading_AI.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
```

---