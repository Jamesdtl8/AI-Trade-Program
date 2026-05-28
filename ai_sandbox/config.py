"""AI sandbox configuration: env + tunables. Separate from Trading_AI.config."""

from __future__ import annotations

import json
import os
from pathlib import Path

# Platform monorepo root (parent of ``AI_Trade_Program/``).
REPO_ROOT = Path(__file__).resolve().parents[2]


def market_phase() -> str:
    """Return the current US market phase: ``regular``, ``extended``, or ``closed``.

    Regular hours: 09:30–16:00 ET (Mon–Fri).
    Extended:     04:00–09:30 ET (pre-market) and 16:00–20:00 ET (after-hours), Mon–Fri.
    Closed:       weekends and overnight.

    Used to pick the order type — T212 only accepts limit orders during regular
    hours, so extended-hours entries must go in as market orders.
    """
    from datetime import datetime
    try:
        import zoneinfo
        et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return "regular"
    if et.weekday() >= 5:
        return "closed"
    minutes = et.hour * 60 + et.minute
    if 570 <= minutes < 960:
        return "regular"
    if (240 <= minutes < 570) or (960 <= minutes < 1200):
        return "extended"
    return "closed"
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "sandbox.db"
SCANNER_FEED_PATH = DATA_DIR / "scanner_feed.jsonl"
SCANNER_FEED_POS_PATH = DATA_DIR / "scanner_feed.pos"
NEWS_SCANNER_FEED_PATH = DATA_DIR / "news_scanner_feed.jsonl"
NEWS_SCANNER_FEED_POS_PATH = DATA_DIR / "news_scanner_feed.pos"

# ── Trading 212 (AI account) ─────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def t212_env() -> str:
    raw = _env("T212_ENV_AI", "live").lower()
    return "live" if raw == "live" else "demo"


def t212_base_url() -> str:
    return (
        "https://live.trading212.com/api/v0"
        if t212_env() == "live"
        else "https://demo.trading212.com/api/v0"
    )


def t212_credentials() -> tuple[str, str]:
    return _env("TRADING_212_KEY_AI"), _env("TRADING_212_SECRET_AI")


def t212_credentials_ok() -> bool:
    k, s = t212_credentials()
    return bool(k and s)


# ── Gemini (AI sandbox scorer / monitor / news classifier) ────────────────────
# Uses the same ``GEMINI_API_KEY`` as the main Discord Gemini parser by default.


def gemini_api_key() -> str:
    return _env("GEMINI_API_KEY") or _env("GOOGLE_GENAI_API_KEY")


def openai_api_key() -> str:
    """OpenAI key for Reasoning Test lab (``responses.create``)."""
    return _env("OPENAI_API_KEY")


def gemini_model_scorer() -> str:
    return _env("AI_GEMINI_MODEL_SCORER") or _env("GEMINI_MODEL") or "gemini-3.1-flash-lite"


def gemini_model_news() -> str:
    return _env("AI_GEMINI_MODEL_NEWS") or _env("GEMINI_MODEL") or "gemini-3.1-flash-lite"


def gemini_model_news_scanner() -> str:
    """Cheap high-volume filter for Discord #news-scanner posts."""
    return _env("AI_GEMINI_MODEL_NEWS_SCANNER") or "gemini-2.5-flash-lite"


def gemini_model_news_scanner_web() -> str:
    """Model for optional Google Search grounded re-grade of #news-scanner items.

    Defaults to the same Flash-Lite tier as :func:`gemini_model_news_scanner`.
    """
    return _env("AI_GEMINI_MODEL_NEWS_SCANNER_WEB").strip() or gemini_model_news_scanner()


def news_scanner_web_search_enabled() -> bool:
    """Second-pass Google Search grounded re-eval when Phase-1 asks for lookup.

    Disable with ``AI_NEWS_SCANNER_WEB_SEARCH=0``.
    """
    raw = (_env("AI_NEWS_SCANNER_WEB_SEARCH", "1")).lower()
    return raw not in ("0", "false", "no", "off")


def news_scanner_web_search_max_output_tokens() -> int:
    """Hard cap on *model-generated* tokens for grounded re-grade JSON (cheap + safe).

    Clamp 96–896. Default ``448``.
    """
    try:
        n = int(_env("AI_NEWS_SCANNER_WEB_MAX_OUTPUT_TOKENS", "448"))
    except ValueError:
        n = 448
    return max(96, min(896, n))


def news_web_search_daily_cap() -> int:
    """Max grounded lookups per UTC calendar day. ``0`` = no daily limit."""

    raw = (_env("AI_NEWS_SCANNER_WEB_SEARCH_DAILY_MAX", "28")).strip()
    try:
        n = int(raw)
    except ValueError:
        n = 28
    return max(0, min(50_000, n))


def news_web_search_monthly_cap() -> int:
    """Max grounded lookups per UTC calendar month. ``0`` = no monthly limit.

    Keep **below** your Gemini search allowance (often ~1000/month on free tiers).
    """
    raw = (_env("AI_NEWS_SCANNER_WEB_SEARCH_MONTHLY_MAX", "850")).strip()
    try:
        n = int(raw)
    except ValueError:
        n = 850
    return max(0, min(500_000, n))


def news_web_search_ticker_gap_seconds() -> float:
    """Minimum seconds between *successful* grounded lookups for the same ticker."""

    raw = (_env("AI_NEWS_SCANNER_WEB_TICKER_GAP_S", "9000")).strip()
    try:
        ss = float(raw)
    except ValueError:
        ss = 9000.0
    return max(0.0, min(864_000.0, ss))


def gemini_token_price_usd_per_million(model: str) -> tuple[float, float]:
    """Return (input USD per 1M tokens, output USD per 1M) for billing estimates.

    Override globally with ``AI_GEMINI_PRICE_TABLE_JSON``:
    ``{"gemini-2.5-flash-lite":{"in":0.075,"out":0.3},"gemini-3.1-flash-lite":{"in":0.1,"out":0.4},"default":{"in":0.1,"out":0.4}}``
    Keys are matched on the full model id (lowercased) or the tail after ``/``.
    """
    m = (model or "").strip().lower()
    raw = _env("AI_GEMINI_PRICE_TABLE_JSON")
    if raw:
        try:
            table = json.loads(raw)
            for key in (m, m.rsplit("/", 1)[-1] if m else ""):
                if key and key in table and isinstance(table[key], dict):
                    e = table[key]
                    return float(e["in"]), float(e["out"])
            if isinstance(table.get("default"), dict):
                e = table["default"]
                return float(e["in"]), float(e["out"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    if "2.5" in m and "flash-lite" in m:
        return 0.075, 0.30
    if "3.1" in m and "flash-lite" in m:
        return 0.10, 0.40
    if "flash-lite" in m:
        return 0.10, 0.40
    return 0.15, 0.60


def gemini_thinking_level() -> str:
    """Thinking depth for AI Trade sandbox Gemini only (main Discord bot stays MINIMAL).

    Valid values match the Gemini API, e.g. ``MINIMAL``, ``LOW``, ``MEDIUM``, ``HIGH``.
    Default **MEDIUM**. Override with ``AI_GEMINI_THINKING_LEVEL``.
    """
    lvl = (_env("AI_GEMINI_THINKING_LEVEL", "MEDIUM")).strip().upper()
    if lvl in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
        return lvl
    return "MEDIUM"


def gemini_scorer_try_thinking() -> bool:
    """When True the client enables Gemini thinking at :func:`gemini_thinking_level`.

    Disabled by ``AI_GEMINI_SCORER_THINKING=0`` — useful if combined JSON output
    with thinking leaks non-JSON into ``response.text`` on your model. Default on.
    """
    raw = (_env("AI_GEMINI_SCORER_THINKING", "1")).lower()
    return raw not in ("0", "false", "no", "off")


def gemini_scorer_logs_thinking_used() -> bool:
    """Persisted into ``scores.thinking_used`` for benchmark reporting."""
    return gemini_scorer_try_thinking()


# ── Engine ───────────────────────────────────────────────────────────────────

_AI_TRADING_STATE = DATA_DIR / "ai_trading_enabled_state"


def trading_enabled() -> bool:
    """AI Trade master switch (``AI_TRADING_ENABLED``).

    If :data:`_AI_TRADING_STATE` exists (written by the dashboard toggle), it
    overrides ``AI_TRADING_ENABLED`` in the process environment and in ``.env``
    across **restarts**. Delete that file to follow environment only again.

    When false, the AI sandbox engine skips scanner/news feeds, Gemini, watch
    reviews, slot monitors, position reconciler, and ticker-map refresh. T212
    order helpers still short-circuit to stubs. Independent of the Discord bot.
    """
    if _AI_TRADING_STATE.is_file():
        try:
            v = _AI_TRADING_STATE.read_text(encoding="utf-8").strip().lower()
            if v:
                return v not in ("0", "false", "no", "off")
        except OSError:
            pass
    raw = _env("AI_TRADING_ENABLED", "1").lower()
    return raw not in ("0", "false", "no", "off")


def news_scanner_enabled() -> bool:
    """Separate Discord #news-scanner / #news-tester pipeline (``news_scanner_feed.jsonl``).

    Production ``#news-scanner`` is off by default. ``#news-tester`` (James server) is on
    by default for isolated news grading tests. Override with env flags.
    """
    raw = (_env("AI_NEWS_SCANNER_ENABLED", "0")).lower()
    raw_tester = (_env("AI_NEWS_TESTER_ENABLED", "1")).lower()
    on = raw in ("1", "true", "yes", "on")
    on_tester = raw_tester in ("1", "true", "yes", "on")
    return on or on_tester


def persist_ai_trading_enabled(enabled: bool) -> None:
    """Save kill-switch position so ``systemctl restart`` keeps the same mode."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _AI_TRADING_STATE.write_text("1" if enabled else "0", encoding="ascii")
    os.environ["AI_TRADING_ENABLED"] = "1" if enabled else "0"


def ai_t212_instrument_map_ttl_seconds() -> float:
    """Seconds between T212 ``/equity/metadata/instruments`` map refreshes (default 1 hour).

    Env ``AI_T212_MAP_TTL_SECONDS``. Clamped to 300–86400. The engine's
    ticker-map task sleeps for this interval as well.
    """
    raw = (_env("AI_T212_MAP_TTL_SECONDS", "3600")).strip()
    try:
        v = float(raw)
        return max(300.0, min(86400.0, v))
    except ValueError:
        return 3600.0


# Tunables — exposed as module constants so we never typo them in business code.
SLOT_COUNT = 5
SLOT_CAPITAL_GBP = 5000.0
# Rough FX: £ → USD for slot sizing; inverse used to store/show deployed £ (qty × $ entry).
GBP_USD_RATE = 1.27


def usd_notionals_to_gbp(usd: float) -> float:
    """Approximate GBP for a USD amount (notional or signed P&L)."""
    if usd == 0:
        return 0.0
    return round(float(usd) / float(GBP_USD_RATE), 4)


COOLING_SECONDS = 120


def monitor_poll_seconds() -> float:
    """Per-slot AI monitor loop interval (Gemini watch); separate from protective T212 poll."""
    raw = (_env("AI_MONITOR_POLL_SECONDS", "1")).strip()
    try:
        v = float(raw)
        return max(0.25, min(120.0, v))
    except ValueError:
        return 1.0


MONITOR_POLL_SECONDS = monitor_poll_seconds()
# Entry fill polling (matches production trading_ai/order_flow semantics).
def fill_wait_timeout_seconds() -> float:
    raw = _env("AI_FILL_WAIT_SECONDS", "").strip()
    if raw:
        try:
            v = float(raw)
            return v if v >= 15 else 120.0
        except ValueError:
            pass
    return 120.0


def fill_partial_threshold() -> float:
    raw = _env("AI_FILL_PARTIAL_THRESHOLD", "").strip()
    if raw:
        try:
            v = float(raw)
            if 0.1 <= v <= 1.0:
                return v
        except ValueError:
            pass
    return 0.5


FILL_WAIT_TIMEOUT_SECONDS = fill_wait_timeout_seconds()
FILL_PARTIAL_THRESHOLD = fill_partial_threshold()
POSITION_RECONCILE_FAST_S = 5
POSITION_RECONCILE_SLOW_S = 30
# Do not stop-loss broker-adopted orphans immediately (they may already be underwater).
RECONCILE_STOP_GRACE_SECONDS = 1800
EXIT_FLAT_POLL_TIMEOUT_S = 45.0
MAX_STOP_LOSS_PCT = 10.0   # hard cap — engine clamps scorer stop so it can never sit deeper than entry × (1 - MAX_STOP_LOSS_PCT/100)


def take_profit_pct() -> float:
    """Maximum take-profit % vs fill (cap). Scorer may request a lower ``tp_pct``.

    Env ``AI_TAKE_PROFIT_PCT`` (default 7.5) is the **ceiling**; the model can
    pick any ``tp_pct`` down to ``AI_TAKE_PROFIT_PCT_MIN`` for quick news pops.
    """
    raw = (_env("AI_TAKE_PROFIT_PCT", "7.5")).strip()
    try:
        v = float(raw)
        return v if 0 < v <= 500 else 7.5
    except ValueError:
        return 7.5


def take_profit_pct_min() -> float:
    """Floor for scorer-chosen take-profit % (default 3). Cannot exceed the cap."""
    raw = (_env("AI_TAKE_PROFIT_PCT_MIN", "3")).strip()
    try:
        v = float(raw)
        return v if 0 < v <= 100 else 3.0
    except ValueError:
        return 3.0


AI_TAKE_PROFIT_PCT = take_profit_pct()
AI_TAKE_PROFIT_PCT_MIN = take_profit_pct_min()


def resolve_take_profit_pct(decision: dict | None = None) -> float:
    """Fixed take-profit % vs confirmed T212 entry (default 7.5%)."""
    _ = decision
    return float(AI_TAKE_PROFIT_PCT)


def profit_target_price(entry: float, decision: dict | None = None) -> float:
    """Limit price target at ``entry × (1 + AI_TAKE_PROFIT_PCT)``."""
    if entry <= 0:
        return 0.0
    pct = resolve_take_profit_pct(decision)
    return round(float(entry) * (1.0 + pct / 100.0), 6)


def entry_limit_cap_price(entry: float) -> float:
    """Maximum limit-buy price during regular hours (AI entry + cap %)."""
    if entry <= 0:
        return 0.0
    return round(float(entry) * (1.0 + ENTRY_LIMIT_CAP_PCT / 100.0), 6)


def reconcile_orphan_positions() -> bool:
    """When false, broker positions without a matching OPEN trade are ignored."""
    return _env("AI_RECONCILE_ORPHAN_POSITIONS", "1").strip().lower() in ("1", "true", "yes")


# CLOSED rows with these exit_reason values were never verified at the broker.
UNCONFIRMED_CLOSE_REASON_PREFIXES: tuple[str, ...] = (
    "startup_flat_or_unfilled:",
    "state_drift:",
    "duplicate_open_cleanup",
    "invalid_slot_cleanup",
    "missing_t212_ticker_restart",
)


def trade_close_broker_confirmed(
    *,
    status: str,
    exit_reason: str | None,
    t212_close_order_id: str | None,
    pnl_gbp: float | None,
) -> bool:
    """True when a CLOSED trade row is backed by a broker fill (order id + P&L)."""
    st = (status or "").strip().upper()
    if st == "OPEN":
        return True
    if st != "CLOSED":
        return False
    oid = str(t212_close_order_id or "").strip()
    if not oid:
        return False
    reason = str(exit_reason or "")
    for prefix in UNCONFIRMED_CLOSE_REASON_PREFIXES:
        if reason.startswith(prefix) or reason == prefix:
            return False
    if pnl_gbp is None:
        return False
    return True


ENTRY_LIMIT_CAP_PCT = 5.0   # regular-hours limit buy capped at entry × (1 + this)
ENTRY_LIMIT_FALLBACK_MARKUP_PCT = ENTRY_LIMIT_CAP_PCT  # legacy alias
SCORER_THRESHOLD_TRADE = 60
SCORER_THRESHOLD_WATCH = 40
MAX_SLOTS_PER_TICKER = 2
QUEUE_TTL_SECONDS = 4 * 60 * 60   # 4h — re-eval loop is now the primary cull
WATCH_REVIEW_INTERVAL_SECONDS = 60   # re-score every watched ticker every 60s — fast enough to catch entries as the move develops
WATCH_DROP_SCORE = 35   # below this on review → remove from watch
WATCH_MAX_REVIEWS = 90  # safety cap (≈90min of reviews at 60s cadence) — drop after this many
HARD_FILTER_RV_MIN = 3.0
HARD_FILTER_RV_MIN_WITH_POSITIVE_NEWS = 3.0
HARD_FILTER_FLOAT_MAX = 30_000_000
HARD_FILTER_FIRST_PCT_MAX = 50.0
HARD_FILTER_FIRST_PCT_MAX_ELEVATED = 70.0
TICKER_HISTORY_HOURS = 48
OFFERING_BLOCK_HOURS = 24
