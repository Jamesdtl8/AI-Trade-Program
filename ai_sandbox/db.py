"""SQLite store for the AI sandbox. Single file, WAL mode."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import config

_log = logging.getLogger("ai_sandbox.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT,
  type TEXT NOT NULL,
  raw TEXT NOT NULL,
  parsed_json TEXT,
  news_class TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker_ts ON alerts(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id INTEGER,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  score INTEGER,
  decision TEXT,
  entry REAL,
  tp REAL,
  stop REAL,
  reason TEXT,
  risk_flags TEXT,
  thinking_used INTEGER DEFAULT 0,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  score_id INTEGER,
  entry_price REAL,
  tp REAL,
  stop REAL,
  capital_gbp REAL,
  quantity REAL,
  open_ts REAL NOT NULL,
  exit_price REAL,
  exit_ts REAL,
  exit_reason TEXT,
  pnl_pct REAL,
  pnl_gbp REAL,
  status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | ERROR | REJECTED | BLOCKED_T212 (legacy)
  t212_open_order_id TEXT,
  t212_close_order_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, open_ts DESC);

CREATE TABLE IF NOT EXISTS monitor_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  price REAL,
  unreal_pct REAL,
  ai_decision TEXT,
  raw_response TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_trade ON monitor_log(trade_id, ts);

CREATE TABLE IF NOT EXISTS offering_blocks (
  ticker TEXT PRIMARY KEY,
  ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_queue (
  ticker TEXT PRIMARY KEY,
  score INTEGER NOT NULL,
  alert_id INTEGER,
  decision_json TEXT NOT NULL,
  alert_json TEXT NOT NULL,
  added_ts REAL NOT NULL,
  last_reviewed_ts REAL,
  reviews INTEGER DEFAULT 0,
  last_decision TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_score ON watch_queue(score DESC);

CREATE TABLE IF NOT EXISTS watch_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  added_ts REAL NOT NULL,
  ended_ts REAL NOT NULL,
  reason TEXT NOT NULL,        -- WATCH: DROP_* / UPGRADE_* / EXPIRED …  TRADE: TRADE_OPEN → TRADE_CLOSED
  reviews INTEGER DEFAULT 0,
  initial_score INTEGER,
  peak_score INTEGER,
  final_score INTEGER,
  final_decision TEXT,
  final_reason TEXT,
  trade_id INTEGER,
  alert_id INTEGER,
  episode_type TEXT DEFAULT 'WATCH',
  audit_json TEXT,
  updated_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_watch_hist_ts ON watch_history(ended_ts DESC);
CREATE INDEX IF NOT EXISTS idx_watch_hist_ticker ON watch_history(ticker, ended_ts DESC);

CREATE TABLE IF NOT EXISTS news_scanner_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ticker TEXT NOT NULL,
  headline TEXT,
  price REAL,
  mcap REAL,
  raw TEXT,
  outcome TEXT NOT NULL,
  outcome_detail TEXT,
  flash_json TEXT,
  audit_json TEXT,
  alert_id INTEGER,
  watch_hist_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_news_scanner_log_ts ON news_scanner_log(ts DESC);

CREATE TABLE IF NOT EXISTS gemini_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  source TEXT NOT NULL,
  call_kind TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  cost_gbp REAL NOT NULL,
  extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_gemini_usage_ts ON gemini_usage_log(ts DESC);

CREATE TABLE IF NOT EXISTS openai_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  source TEXT NOT NULL,
  call_kind TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  cost_gbp REAL NOT NULL,
  extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_openai_usage_ts ON openai_usage_log(ts DESC);

CREATE TABLE IF NOT EXISTS ticker_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  date TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'NEW',
  alert_count INTEGER NOT NULL DEFAULT 0,
  alerts_json TEXT NOT NULL DEFAULT '[]',
  ai_context_json TEXT,
  ai_grade TEXT,
  ai_decision TEXT,
  entry_price REAL,
  target_price REAL,
  disqualify_reason TEXT,
  float_shares REAL,
  created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL,
  UNIQUE (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_ticker_states_date ON ticker_states(date, ticker);

CREATE TABLE IF NOT EXISTS ai_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  date TEXT NOT NULL,
  alert_number INTEGER NOT NULL,
  alert_id INTEGER,
  ai_input TEXT NOT NULL,
  ai_output_json TEXT NOT NULL,
  grade TEXT,
  action TEXT,
  entry_price REAL,
  target_price REAL,
  latency_ms INTEGER,
  cost_gbp REAL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_ts ON ai_decisions(ts DESC);

CREATE TABLE IF NOT EXISTS t212_blacklist (
  ticker TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  detail TEXT,
  ts REAL NOT NULL,
  t212_instrument TEXT
);
CREATE INDEX IF NOT EXISTS idx_t212_blacklist_ts ON t212_blacklist(ts DESC);

CREATE TABLE IF NOT EXISTS news_web_search_quota (
  period_key TEXT PRIMARY KEY,
  n INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS news_web_search_ticker_cool (
  ticker TEXT PRIMARY KEY,
  last_ok_ts REAL NOT NULL
);
"""

_TRADE_EXTRA_COLS: tuple[tuple[str, str], ...] = (
    ("alert_id", "INTEGER"),
    ("t212_error", "TEXT"),
    ("anchor_send_price", "REAL"),
    ("hypo_buy_price", "REAL"),
    ("hypo_tp_price", "REAL"),
    ("hypo_stop_price", "REAL"),
    ("sim_exit_price", "REAL"),
    ("sim_outcome_pct", "REAL"),
    ("sim_hit_tp", "INTEGER"),
    ("miss_monitor_end_ts", "REAL"),
)


def _migrate_trades_columns(c: sqlite3.Connection) -> None:
    cur = c.execute("PRAGMA table_info(trades)")
    cols = {str(row[1]) for row in cur.fetchall()}
    for name, typ in _TRADE_EXTRA_COLS:
        if name not in cols:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")



def _migrate_watch_history_ai_columns(c: sqlite3.Connection) -> None:
    cur = c.execute("PRAGMA table_info(watch_history)")
    cols = {str(row[1]) for row in cur.fetchall()}
    if "episode_type" not in cols:
        c.execute("ALTER TABLE watch_history ADD COLUMN episode_type TEXT DEFAULT 'WATCH'")
    if "audit_json" not in cols:
        c.execute("ALTER TABLE watch_history ADD COLUMN audit_json TEXT")
    if "updated_ts" not in cols:
        c.execute("ALTER TABLE watch_history ADD COLUMN updated_ts REAL")
    c.execute(
        "UPDATE watch_history SET episode_type='WATCH' "
        "WHERE episode_type IS NULL OR TRIM(COALESCE(episode_type,''))=''"
    )
    c.execute(
        "UPDATE watch_history SET updated_ts=ended_ts WHERE updated_ts IS NULL"
    )


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.executescript(_SCHEMA)
    _migrate_trades_columns(c)
    _migrate_watch_history_ai_columns(c)
    _conn = c
    return c


def init() -> None:
    with _lock:
        _connect()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        c = _connect()
        return c.execute(sql, params)


def fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    cur = execute(sql, params)
    return list(cur.fetchall())


def fetchone(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    cur = execute(sql, params)
    return cur.fetchone()


def _quota_n(c: sqlite3.Connection, key: str) -> int:
    r = c.execute(
        "SELECT n FROM news_web_search_quota WHERE period_key=?",
        (key,),
    ).fetchone()
    return int(r[0]) if r else 0


def news_web_search_begin_attempt(
    ticker_upper: str,
    *,
    ticker_gap_seconds: float,
    daily_cap: int,
    monthly_cap: int,
) -> tuple[bool, str]:
    """Atomically increment day/month quotas (reservation) if cooldown + caps OK.

    **Caps:** ``0`` = unlimited along that axis. Both unlimited is rejected (would burn search budget).

    On success reserves one grounded-search attempt — call :func:`news_web_search_abort_reservation`
    if the Gemini request raises, otherwise :func:`news_web_search_commit_success`.
    """

    tk = (ticker_upper or "").strip().upper() or "?"
    ticker_gap_seconds = max(0.0, ticker_gap_seconds)
    now = time.time()
    utc = datetime.now(timezone.utc)
    day_k = "d:" + utc.strftime("%Y-%m-%d")
    month_k = "m:" + utc.strftime("%Y-%m")

    if daily_cap <= 0 and monthly_cap <= 0:
        return False, "quota_unconfigured"

    with _lock:
        c = _connect()

        row = c.execute(
            "SELECT last_ok_ts FROM news_web_search_ticker_cool WHERE ticker=?",
            (tk,),
        ).fetchone()
        if row is not None and ticker_gap_seconds > 0:
            last = float(row[0])
            if now - last < ticker_gap_seconds:
                remaining = int(ticker_gap_seconds - (now - last))
                return False, f"ticker_cooldown:{remaining}s"

        dn = _quota_n(c, day_k)
        mn = _quota_n(c, month_k)
        if daily_cap > 0 and dn >= daily_cap:
            return False, "quota_day"
        if monthly_cap > 0 and mn >= monthly_cap:
            return False, "quota_month"

        for key in (day_k, month_k):
            c.execute(
                """INSERT INTO news_web_search_quota(period_key, n) VALUES(?, 1)
                   ON CONFLICT(period_key) DO UPDATE SET n = n + 1""",
                (key,),
            )
    return True, ""


def news_web_search_abort_reservation() -> None:
    """Refund one grounded-search reservation (after failed API call)."""
    utc = datetime.now(timezone.utc)
    day_k = "d:" + utc.strftime("%Y-%m-%d")
    month_k = "m:" + utc.strftime("%Y-%m")

    with _lock:
        c = _connect()
        for key in (day_k, month_k):
            c.execute(
                """UPDATE news_web_search_quota
                   SET n = CASE WHEN COALESCE(n, 0) > 0 THEN n - 1 ELSE 0 END
                   WHERE period_key=?""",
                (key,),
            )


def news_web_search_commit_success(ticker_upper: str) -> None:
    """Record last successful grounded lookup for ticker cooldown."""

    tk = (ticker_upper or "").strip().upper() or "?"
    ts = time.time()
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO news_web_search_ticker_cool(ticker, last_ok_ts) VALUES(?, ?)
               ON CONFLICT(ticker) DO UPDATE SET last_ok_ts=excluded.last_ok_ts""",
            (tk, ts),
        )


def insert(sql: str, params: tuple) -> int:
    with _lock:
        c = _connect()
        cur = c.execute(sql, params)
        return int(cur.lastrowid or 0)


# ── Convenience helpers ─────────────────────────────────────────────────────


def log_alert(ticker: str | None, atype: str, raw: str, parsed: dict[str, Any] | None) -> int:
    return insert(
        "INSERT INTO alerts(ts, ticker, type, raw, parsed_json) VALUES(?,?,?,?,?)",
        (time.time(), ticker, atype, raw, json.dumps(parsed) if parsed else None),
    )


def set_alert_news_class(alert_id: int, news_class: str) -> None:
    execute("UPDATE alerts SET news_class=? WHERE id=?", (news_class, alert_id))


def log_score(alert_id: int | None, ticker: str, payload: dict[str, Any], thinking_used: bool) -> int:
    return insert(
        """INSERT INTO scores(alert_id, ts, ticker, score, decision, entry, tp, stop,
                              reason, risk_flags, thinking_used, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            alert_id,
            time.time(),
            ticker,
            payload.get("score"),
            payload.get("decision"),
            payload.get("entry"),
            payload.get("tp"),
            payload.get("stop"),
            payload.get("reason"),
            json.dumps(payload.get("risk_flags") or []),
            1 if thinking_used else 0,
            json.dumps(payload),
        ),
    )


def news_scanner_log_insert(
    *,
    ticker: str,
    headline: str | None,
    price: float | None,
    mcap: float | None,
    raw: str,
    outcome: str,
    outcome_detail: str | None = None,
    flash_grade: dict[str, Any] | None = None,
    audit: list[dict[str, Any]] | None = None,
    alert_id: int | None = None,
    watch_hist_id: int | None = None,
) -> int:
    """Persist one #news-scanner pipeline outcome (including skips) for the dashboard log."""
    return insert(
        """INSERT INTO news_scanner_log(
              ts, ticker, headline, price, mcap, raw, outcome, outcome_detail,
              flash_json, audit_json, alert_id, watch_hist_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            (ticker or "").strip().upper(),
            headline,
            price,
            mcap,
            (raw or "")[:5000],
            outcome,
            outcome_detail,
            json.dumps(flash_grade, ensure_ascii=False) if flash_grade else None,
            json.dumps(audit, ensure_ascii=False) if audit else None,
            alert_id,
            watch_hist_id,
        ),
    )


def news_scanner_log_list(limit: int = 200) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    rows = fetchall(
        """SELECT id, ts, ticker, headline, price, mcap, outcome, outcome_detail,
                  alert_id, watch_hist_id
             FROM news_scanner_log
            ORDER BY id DESC
            LIMIT ?""",
        (lim,),
    )
    return [dict(r) for r in rows]


def news_scanner_log_get(log_id: int) -> dict[str, Any] | None:
    row = fetchone(
        """SELECT id, ts, ticker, headline, price, mcap, raw, outcome, outcome_detail,
                  flash_json, audit_json, alert_id, watch_hist_id
             FROM news_scanner_log WHERE id=?""",
        (int(log_id),),
    )
    return dict(row) if row else None


def gemini_usage_insert(
    *,
    source: str,
    call_kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_gbp: float,
    extra: dict[str, Any] | None = None,
) -> int:
    return insert(
        """INSERT INTO gemini_usage_log(
              ts, source, call_kind, model, input_tokens, output_tokens,
              total_tokens, cost_gbp, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            str(source)[:32],
            str(call_kind)[:64],
            str(model)[:128],
            int(max(0, input_tokens)),
            int(max(0, output_tokens)),
            int(max(0, total_tokens)),
            round(float(cost_gbp), 6),
            json.dumps(extra, ensure_ascii=False) if extra else None,
        ),
    )


def gemini_usage_stats_since(cutoff_ts: float) -> dict[str, Any]:
    row = fetchone(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(cost_gbp), 0) AS gbp,
                  COALESCE(SUM(input_tokens), 0) AS it,
                  COALESCE(SUM(output_tokens), 0) AS ot
             FROM gemini_usage_log WHERE ts >= ?""",
        (float(cutoff_ts),),
    )
    if not row:
        return {"sum_gbp": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    return {
        "sum_gbp": float(row["gbp"] or 0),
        "calls": int(row["n"] or 0),
        "input_tokens": int(row["it"] or 0),
        "output_tokens": int(row["ot"] or 0),
    }


def openai_usage_insert(
    *,
    source: str,
    call_kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_gbp: float,
    extra: dict[str, Any] | None = None,
) -> int:
    return insert(
        """INSERT INTO openai_usage_log(
              ts, source, call_kind, model, input_tokens, output_tokens,
              total_tokens, cost_gbp, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            str(source)[:32],
            str(call_kind)[:64],
            str(model)[:128],
            int(max(0, input_tokens)),
            int(max(0, output_tokens)),
            int(max(0, total_tokens)),
            round(float(cost_gbp), 6),
            json.dumps(extra, ensure_ascii=False) if extra else None,
        ),
    )


def openai_usage_stats_since(cutoff_ts: float) -> dict[str, Any]:
    row = fetchone(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(cost_gbp), 0) AS gbp,
                  COALESCE(SUM(input_tokens), 0) AS it,
                  COALESCE(SUM(output_tokens), 0) AS ot
             FROM openai_usage_log WHERE ts >= ?""",
        (float(cutoff_ts),),
    )
    if not row:
        return {"sum_gbp": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    return {
        "sum_gbp": float(row["gbp"] or 0),
        "calls": int(row["n"] or 0),
        "input_tokens": int(row["it"] or 0),
        "output_tokens": int(row["ot"] or 0),
    }


def uk_date_iso(ts: float | None = None) -> str:
    import datetime as _dt

    t = float(ts if ts is not None else time.time())
    tz = _dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/London")
    except Exception:
        pass
    return _dt.datetime.fromtimestamp(t, tz=tz).date().isoformat()


def ai_decision_insert(
    *,
    ticker: str,
    alert_number: int,
    alert_id: int | None,
    ai_input: str,
    ai_output: dict[str, Any],
    grade: str,
    action: str,
    entry_price: Any,
    target_price: Any,
    latency_ms: int,
    cost_gbp: float,
) -> int:
    return insert(
        """INSERT INTO ai_decisions(
              ticker, date, alert_number, alert_id, ai_input, ai_output_json,
              grade, action, entry_price, target_price, latency_ms, cost_gbp, ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ticker.upper(),
            uk_date_iso(),
            int(alert_number),
            alert_id,
            ai_input,
            json.dumps(ai_output, default=str),
            grade,
            action,
            entry_price,
            target_price,
            int(latency_ms),
            round(float(cost_gbp), 6),
            time.time(),
        ),
    )


def combined_llm_usage_stats_since(cutoff_ts: float) -> dict[str, Any]:
    g = gemini_usage_stats_since(cutoff_ts)
    o = openai_usage_stats_since(cutoff_ts)
    return {
        "sum_gbp": round(float(g["sum_gbp"]) + float(o["sum_gbp"]), 6),
        "calls": int(g["calls"]) + int(o["calls"]),
        "input_tokens": int(g["input_tokens"]) + int(o["input_tokens"]),
        "output_tokens": int(g["output_tokens"]) + int(o["output_tokens"]),
        "gemini_gbp": float(g["sum_gbp"]),
        "openai_gbp": float(o["sum_gbp"]),
    }


def scores_for_alert(alert_id: int) -> list[dict[str, Any]]:
    """All scorer rows for one alert id (scanner message), oldest first."""
    rows = fetchall(
        """SELECT id, ts, score, decision, entry, tp, stop, reason, thinking_used, raw_json
             FROM scores
            WHERE alert_id=?
            ORDER BY ts ASC, id ASC""",
        (int(alert_id),),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        rj: dict[str, Any] = {}
        if item.get("raw_json"):
            try:
                rj = json.loads(str(item["raw_json"]))
            except Exception:
                rj = {}
        item.pop("raw_json", None)
        item["risk_flags"] = rj.get("risk_flags") or []
        item["entry_pattern"] = rj.get("entry_pattern")
        item["payload"] = rj if rj else None
        out.append(item)
    return out


def ai_decisions_for_alert(alert_id: int) -> list[dict[str, Any]]:
    """GPT grader rows from ai_decisions for one alert id."""
    rows = fetchall(
        """SELECT id, ts, ticker, alert_number, grade, action, entry_price, target_price,
                  latency_ms, cost_gbp, ai_input, ai_output_json
             FROM ai_decisions
            WHERE alert_id=?
            ORDER BY ts ASC, id ASC""",
        (int(alert_id),),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        raw = item.pop("ai_output_json", None)
        item["ai_output"] = None
        if raw:
            try:
                item["ai_output"] = json.loads(str(raw))
            except Exception:
                item["ai_output"] = None
        out.append(item)
    return out


def block_offering(ticker: str) -> None:
    execute(
        "INSERT OR REPLACE INTO offering_blocks(ticker, ts) VALUES(?,?)",
        (ticker.upper(), time.time()),
    )


def offering_block_active(ticker: str, hours: float) -> bool:
    row = fetchone(
        "SELECT ts FROM offering_blocks WHERE ticker=?", (ticker.upper(),)
    )
    if not row:
        return False
    return (time.time() - float(row["ts"])) < hours * 3600.0


def t212_blacklist_add(
    ticker: str,
    *,
    reason: str,
    detail: str | None = None,
    t212_instrument: str | None = None,
) -> None:
    """Remember scanner symbols that cannot be opened (not on T212, close-only, etc.)."""
    t = ticker.strip().upper().lstrip("$")
    if not t or t == "?":
        return
    execute(
        """INSERT INTO t212_blacklist(ticker, reason, detail, ts, t212_instrument)
               VALUES(?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
               reason=excluded.reason,
               detail=excluded.detail,
               ts=excluded.ts,
               t212_instrument=excluded.t212_instrument""",
        (t, reason[:64], (detail or "")[:500] if detail else None, time.time(), t212_instrument),
    )


def t212_blacklist_get(ticker: str) -> sqlite3.Row | None:
    t = ticker.strip().upper().lstrip("$")
    if not t:
        return None
    return fetchone("SELECT * FROM t212_blacklist WHERE ticker=?", (t,))


# ── Watch queue persistence ────────────────────────────────────────────────


def watch_upsert(
    ticker: str,
    *,
    score: int,
    alert_id: int | None,
    decision: dict[str, Any],
    alert: dict[str, Any],
) -> None:
    """Insert or replace a watched ticker. Preserves added_ts/reviews on update."""
    t = ticker.upper()
    existing = fetchone("SELECT added_ts, reviews FROM watch_queue WHERE ticker=?", (t,))
    added_ts = float(existing["added_ts"]) if existing else time.time()
    reviews = int(existing["reviews"]) if existing else 0
    execute(
        """INSERT INTO watch_queue
                (ticker, score, alert_id, decision_json, alert_json,
                 added_ts, last_reviewed_ts, reviews, last_decision)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
                score=excluded.score,
                alert_id=excluded.alert_id,
                decision_json=excluded.decision_json,
                alert_json=excluded.alert_json,
                last_decision=excluded.last_decision""",
        (
            t,
            int(score),
            alert_id,
            json.dumps(decision, default=str),
            json.dumps(alert, default=str),
            added_ts,
            None,
            reviews,
            decision.get("decision"),
        ),
    )


def watch_remove(ticker: str) -> None:
    execute("DELETE FROM watch_queue WHERE ticker=?", (ticker.upper(),))


def watch_all() -> list[dict[str, Any]]:
    rows = fetchall(
        """SELECT ticker, score, alert_id, decision_json, alert_json,
                  added_ts, last_reviewed_ts, reviews, last_decision
             FROM watch_queue ORDER BY score DESC, added_ts ASC"""
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["decision"] = json.loads(d.pop("decision_json") or "{}")
        except Exception:
            d["decision"] = {}
        try:
            d["alert"] = json.loads(d.pop("alert_json") or "{}")
        except Exception:
            d["alert"] = {}
        out.append(d)
    return out


def watch_history_insert(
    *,
    ticker: str,
    added_ts: float,
    ended_ts: float,
    reason: str,
    reviews: int,
    initial_score: int | None,
    peak_score: int | None,
    final_score: int | None,
    final_decision: str | None,
    final_reason: str | None,
    trade_id: int | None,
    alert_id: int | None,
    episode_type: str = "WATCH",
    audit: dict[str, Any] | None = None,
) -> int:
    """Record a watch-queue episode or (via :func:`trade_audit_open`) a trade audit row."""
    aj = json.dumps(audit, default=str) if audit else None
    uts = float(max(added_ts, ended_ts))
    return insert(
        """INSERT INTO watch_history(
                ticker, added_ts, ended_ts, reason, reviews,
                initial_score, peak_score, final_score, final_decision,
                final_reason, trade_id, alert_id, episode_type, audit_json, updated_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ticker.upper(),
            float(added_ts),
            float(ended_ts),
            reason,
            int(reviews or 0),
            initial_score,
            peak_score,
            final_score,
            final_decision,
            final_reason,
            trade_id,
            alert_id,
            episode_type,
            aj,
            uts,
        ),
    )


WATCH_ACTIVE_REASON = "WATCH_ACTIVE"


def _audit_load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {"events": []}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            d.setdefault("events", [])
            return d
    except Exception:
        pass
    return {"events": []}


def watch_episode_ensure_open(
    ticker: str,
    *,
    alert_id: int | None,
    added_ts: float,
    event: dict[str, Any],
) -> int:
    """One open episode per ticker (reason=WATCH_ACTIVE). Appends events on re-enqueue."""
    t = ticker.strip().upper().lstrip("$")
    if not t or t == "?":
        return 0
    row = fetchone(
        "SELECT id, audit_json, peak_score FROM watch_history "
        "WHERE ticker=? AND reason=? ORDER BY id DESC LIMIT 1",
        (t, WATCH_ACTIVE_REASON),
    )
    ev = dict(event)
    ev.setdefault("ts", time.time())
    try:
        scr = int(ev.get("score")) if ev.get("score") is not None else 0
    except (TypeError, ValueError):
        scr = 0
    if row:
        hid = int(row["id"])
        audit = _audit_load(row["audit_json"])
        audit.setdefault("events", []).append(ev)
        peak = max(int(row["peak_score"] or 0), scr)
        fd = str(ev.get("decision") or "").strip().upper()[:32] or None
        fr = str(ev.get("reason") or "")[:500] if ev.get("reason") else None
        execute(
            """UPDATE watch_history SET audit_json=?, peak_score=?,
                      alert_id=COALESCE(?, alert_id), updated_ts=?,
                      final_decision=?, final_reason=?
                WHERE id=?""",
            (
                json.dumps(audit, default=str),
                peak,
                alert_id,
                time.time(),
                fd,
                fr,
                hid,
            ),
        )
        return hid
    isc = scr if scr else None
    return watch_history_insert(
        ticker=t,
        added_ts=float(added_ts),
        ended_ts=0.0,
        reason=WATCH_ACTIVE_REASON,
        reviews=0,
        initial_score=isc,
        peak_score=isc,
        final_score=None,
        final_decision=str(ev.get("decision") or "").strip().upper()[:32] or None,
        final_reason=str(ev.get("reason") or "")[:500] if ev.get("reason") else None,
        trade_id=None,
        alert_id=alert_id,
        episode_type="WATCH",
        audit={"events": [ev]},
    )


def watch_episode_append_review(
    ticker: str,
    *,
    event: dict[str, Any],
    score: int,
    reviews: int,
) -> None:
    """Append a periodic review event to the active watch episode."""
    t = ticker.strip().upper().lstrip("$")
    if not t or t == "?":
        return
    row = fetchone(
        "SELECT id, audit_json, peak_score FROM watch_history "
        "WHERE ticker=? AND reason=? ORDER BY id DESC LIMIT 1",
        (t, WATCH_ACTIVE_REASON),
    )
    if not row:
        return
    audit = _audit_load(row["audit_json"])
    audit.setdefault("events", []).append(event)
    peak = max(int(row["peak_score"] or 0), int(score))
    fd = str(event.get("decision") or "").strip().upper()[:32] or None
    fr = str(event.get("reason") or "")[:500] if event.get("reason") else None
    execute(
        """UPDATE watch_history SET audit_json=?, peak_score=?, reviews=?,
                  updated_ts=?, final_decision=?, final_reason=?
            WHERE id=?""",
        (
            json.dumps(audit, default=str),
            peak,
            int(reviews),
            time.time(),
            fd,
            fr,
            int(row["id"]),
        ),
    )


def watch_episode_finalize(
    ticker: str,
    *,
    added_ts_fallback: float | None,
    ended_ts: float,
    reason: str,
    reviews: int,
    initial_score: int | None,
    peak_score: int | None,
    final_score: int | None,
    final_decision: str | None,
    final_reason: str | None,
    trade_id: int | None,
    alert_id: int | None,
    audit_tail: dict[str, Any] | None = None,
) -> int:
    """Close WATCH_ACTIVE row in-place, or INSERT a terminal episode if none open."""
    t = ticker.strip().upper().lstrip("$")
    if not t or t == "?":
        return 0
    row = fetchone(
        "SELECT id, added_ts, audit_json, initial_score, peak_score FROM watch_history "
        "WHERE ticker=? AND reason=? ORDER BY id DESC LIMIT 1",
        (t, WATCH_ACTIVE_REASON),
    )
    if row:
        hid = int(row["id"])
        audit = _audit_load(row["audit_json"])
        if audit_tail:
            audit.setdefault("events", []).append(audit_tail)
        peak_old = int(row["peak_score"] or 0)
        ps = int(peak_score) if peak_score is not None else peak_old
        peak_new = max(peak_old, ps, int(final_score or 0))
        init_sc = row["initial_score"]
        if init_sc is None and initial_score is not None:
            init_sc = initial_score
        execute(
            """UPDATE watch_history SET reason=?, ended_ts=?, reviews=?, initial_score=?,
                      peak_score=?, final_score=?, final_decision=?, final_reason=?,
                      trade_id=COALESCE(?, trade_id), alert_id=COALESCE(?, alert_id),
                      audit_json=?, updated_ts=?
                WHERE id=?""",
            (
                reason,
                float(ended_ts),
                int(reviews),
                init_sc,
                peak_new,
                final_score,
                final_decision,
                final_reason,
                trade_id,
                alert_id,
                json.dumps(audit, default=str),
                float(ended_ts),
                hid,
            ),
        )
        return hid
    af = float(added_ts_fallback) if added_ts_fallback is not None else float(ended_ts) - 1.0
    return watch_history_insert(
        ticker=t,
        added_ts=af,
        ended_ts=float(ended_ts),
        reason=reason,
        reviews=int(reviews or 0),
        initial_score=initial_score,
        peak_score=peak_score,
        final_score=final_score,
        final_decision=final_decision,
        final_reason=final_reason,
        trade_id=trade_id,
        alert_id=alert_id,
        episode_type="WATCH",
        audit={"events": [audit_tail]} if audit_tail else None,
    )


def trade_audit_open(
    *,
    trade_id: int,
    alert_id: int | None,
    ticker_t212: str,
    added_ts: float,
    audit: dict[str, Any],
) -> int:
    """Insert ``TRADE_OPEN`` row with full entry-time audit (scorer, alert, levels, fill)."""
    ex = fetchone(
        """SELECT id FROM watch_history WHERE trade_id=? AND episode_type='TRADE'
            AND reason='TRADE_OPEN' ORDER BY id DESC LIMIT 1""",
        (trade_id,),
    )
    if ex:
        return int(ex["id"])
    dec = audit.get("scorer_decision")
    headline = ""
    isc: int | None = None
    if isinstance(dec, dict):
        headline = str(dec.get("reason") or "")[:500]
        try:
            raw_s = dec.get("score")
            isc = int(raw_s) if raw_s is not None else None
        except (TypeError, ValueError):
            isc = None
    return watch_history_insert(
        ticker=ticker_t212,
        added_ts=added_ts,
        ended_ts=added_ts,
        reason="TRADE_OPEN",
        reviews=0,
        initial_score=isc,
        peak_score=isc,
        final_score=None,
        final_decision="TRADE",
        final_reason=headline or "trade opened",
        trade_id=trade_id,
        alert_id=alert_id,
        episode_type="TRADE",
        audit=audit,
    )


def trade_audit_failed(
    *,
    trade_id: int,
    alert_id: int | None,
    ticker_t212: str,
    added_ts: float,
    audit: dict[str, Any],
    final_reason: str | None = None,
) -> int:
    """Terminal TRADE episode when a ``trades`` row is REJECTED (never opened a position)."""
    ex = fetchone(
        """SELECT id FROM watch_history WHERE trade_id=? AND episode_type='TRADE'
            AND reason='TRADE_FAILED' ORDER BY id DESC LIMIT 1""",
        (int(trade_id),),
    )
    if ex:
        return int(ex["id"])
    dec = audit.get("scorer_decision")
    headline = ""
    isc: int | None = None
    if isinstance(dec, dict):
        headline = str(dec.get("reason") or "")[:500]
        try:
            raw_s = dec.get("score")
            isc = int(raw_s) if raw_s is not None else None
        except (TypeError, ValueError):
            isc = None
    br = audit.get("broker_rejection")
    fr = (final_reason or "").strip()
    if not fr and isinstance(br, dict):
        fr = str(br.get("brief") or br.get("kind") or "")[:500]
    if not fr:
        fr = "broker rejected order — no position opened"
    fr = fr[:500]
    peak = isc or 0
    try:
        for row in scores_for_alert(int(alert_id)) if alert_id else []:
            peak = max(peak, int(row.get("score") or 0))
    except Exception:
        pass
    return watch_history_insert(
        ticker=ticker_t212,
        added_ts=float(added_ts),
        ended_ts=float(added_ts),
        reason="TRADE_FAILED",
        reviews=0,
        initial_score=isc,
        peak_score=peak or isc,
        final_score=isc,
        final_decision="TRADE_FAILED",
        final_reason=fr,
        trade_id=int(trade_id),
        alert_id=alert_id,
        episode_type="TRADE",
        audit=audit,
    )


def _body_is_close_only(body: Any) -> bool:
    """Match :func:`t212_ai.is_close_only_error` without importing ``t212_ai`` (heavy deps)."""
    if isinstance(body, dict):
        et = str(body.get("type") or "").lower().replace("\\", "/")
        if "instrument-close-only-mode" in et:
            return True
        detail = str(body.get("detail") or "").lower()
        if "close only" in detail or "close-only" in detail:
            return True
        try:
            blob = json.dumps(body).lower()
        except Exception:
            blob = ""
        if "instrument-close-only-mode" in blob or "close-only" in blob:
            return True
    elif body:
        s = str(body).lower()
        if "close-only" in s or "close only" in s or "instrument-close-only-mode" in s:
            return True
    return False


def backfill_rejected_trades_without_failed_history(limit: int = 200) -> int:
    """Create ``TRADE_FAILED`` watch_history rows for old REJECTED trades missing an audit episode."""
    lim = max(1, min(int(limit), 2000))
    rows = fetchall(
        """
        SELECT t.* FROM trades t
        WHERE t.status IN ('REJECTED', 'BLOCKED_T212')
          AND NOT EXISTS (
                SELECT 1 FROM watch_history h
                 WHERE h.trade_id = t.id
                   AND h.episode_type = 'TRADE'
                   AND h.reason = 'TRADE_FAILED'
              )
        ORDER BY t.open_ts DESC
        LIMIT ?
        """,
        (lim,),
    )
    n = 0
    for tr in rows:
        tdict = dict(tr)
        tid = int(tdict["id"])
        aid = int(tdict["alert_id"]) if tdict.get("alert_id") else None
        t212_sym = str(tdict.get("ticker") or "")
        raw_sym = t212_sym.split("_")[0].upper() if t212_sym else ""
        ar_sum = None
        if aid:
            ar = fetchone(
                "SELECT id, ts, type, raw, parsed_json, news_class, ticker FROM alerts WHERE id=?",
                (aid,),
            )
            if ar:
                pj = None
                if ar["parsed_json"]:
                    try:
                        pj = json.loads(str(ar["parsed_json"]))
                    except Exception:
                        pj = None
                altk = (str(ar["ticker"] or "").strip().upper().lstrip("$") or raw_sym)
                if altk:
                    raw_sym = altk
                ar_sum = {
                    "id": int(ar["id"]),
                    "ts": float(ar["ts"]),
                    "type": ar["type"],
                    "news_class": ar["news_class"],
                    "parsed": pj,
                    "raw_excerpt": (str(ar["raw"] or "")[:800]),
                }
        score_chain: list[dict[str, Any]] = []
        if aid:
            try:
                score_chain = scores_for_alert(int(aid))
            except Exception:
                score_chain = []
        err_raw = str(tdict.get("t212_error") or "")
        brief = str(tdict.get("exit_reason") or "rejected")[:500]
        body_obj: Any = None
        if err_raw:
            try:
                body_obj = json.loads(err_raw)
            except Exception:
                body_obj = {"raw": err_raw[:2000]}
        http_status = 0
        if isinstance(body_obj, dict):
            http_status = int(body_obj.get("http_status") or 0)
            inner = body_obj.get("body")
            if _body_is_close_only(inner if inner is not None else body_obj):
                try:
                    t212_blacklist_add(
                        raw_sym,
                        reason="CLOSE_ONLY",
                        detail=brief or None,
                        t212_instrument=t212_sym or None,
                    )
                except Exception:
                    _log.exception("backfill blacklist CLOSE_ONLY %s", raw_sym)
        detail_json = ""
        try:
            detail_json = json.dumps(body_obj, default=str)[:8000] if body_obj else ""
        except Exception:
            detail_json = err_raw[:8000]
        audit: dict[str, Any] = {
            "failed_ts": time.time(),
            "backfilled": True,
            "raw_scanner_ticker": raw_sym,
            "t212_instrument": t212_sym,
            "scorer_decision": None,
            "alert_at_trade": ar_sum.get("parsed") if ar_sum else None,
            "alert_row": ar_sum,
            "scores_for_alert": score_chain,
            "planned_entry": {
                "planned_entry": tdict.get("entry_price"),
                "tp": tdict.get("tp"),
                "stop": tdict.get("stop"),
                "quantity_attempted": tdict.get("quantity"),
            },
            "broker_rejection": {
                "kind": "historical_reject",
                "http_status": http_status,
                "brief": brief,
                "detail_json": detail_json,
            },
        }
        try:
            if score_chain:
                last = score_chain[-1]
                audit["scorer_decision"] = {
                    "decision": last.get("decision"),
                    "score": last.get("score"),
                    "reason": last.get("reason"),
                }
        except Exception:
            pass
        try:
            trade_audit_failed(
                trade_id=tid,
                alert_id=aid,
                ticker_t212=t212_sym,
                added_ts=float(tdict.get("open_ts") or time.time()),
                audit=audit,
                final_reason=brief,
            )
            n += 1
        except Exception:
            _log.exception("backfill trade_audit_failed trade_id=%s", tid)
    return n


def trade_audit_finalize(
    trade_id: int,
    *,
    exit_ts: float,
    exit_reason: str,
    risk_at_exit: dict[str, Any],
    close_order_id: str | None,
) -> None:
    """Merge monitor log + exit into the ``TRADE_OPEN`` row, or insert ``TRADE_CLOSED`` if missing."""
    if fetchone(
        """SELECT 1 FROM watch_history WHERE trade_id=? AND episode_type='TRADE'
            AND reason='TRADE_CLOSED' LIMIT 1""",
        (trade_id,),
    ):
        return

    row = fetchone(
        """SELECT id, audit_json, initial_score FROM watch_history
            WHERE trade_id=? AND episode_type='TRADE' AND reason='TRADE_OPEN'
            ORDER BY id DESC LIMIT 1""",
        (trade_id,),
    )
    trow = fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))
    mon_rows = fetchall(
        """SELECT ts, price, unreal_pct, ai_decision, raw_response FROM monitor_log
            WHERE trade_id=? ORDER BY ts ASC""",
        (trade_id,),
    )
    mon = [dict(x) for x in mon_rows]

    base: dict[str, Any] = {}
    init_sc: int | None = None
    hist_id: int | None = None
    if row:
        hist_id = int(row["id"])
        if row["initial_score"] is not None:
            try:
                init_sc = int(row["initial_score"])
            except (TypeError, ValueError):
                init_sc = None
        if row["audit_json"]:
            try:
                base = json.loads(row["audit_json"])
            except Exception:
                base = {}

    exit_block: dict[str, Any] = {
        "ts": exit_ts,
        "reason": exit_reason,
        "close_order_id": close_order_id,
        "risk_at_exit": risk_at_exit,
    }
    if trow:
        tr = dict(trow)
        exit_block["pnl_pct"] = tr.get("pnl_pct")
        exit_block["pnl_gbp"] = tr.get("pnl_gbp")
        exit_block["exit_price"] = tr.get("exit_price")
    base["exit"] = exit_block
    base["monitor_log"] = mon

    fj = json.dumps(base, default=str)  # used for UPDATE audit_json payload
    fr = (exit_reason or "")[:500]
    fts = time.time()

    if hist_id is not None:
        cur = execute(
            """UPDATE watch_history SET reason='TRADE_CLOSED', ended_ts=?, final_reason=?,
                final_decision='CLOSED', audit_json=?, updated_ts=?,
                final_score=COALESCE(?, final_score)
                WHERE id=? AND reason='TRADE_OPEN'""",
            (
                float(exit_ts),
                fr,
                fj,
                fts,
                init_sc,
                hist_id,
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            return

    if fetchone(
        """SELECT 1 FROM watch_history WHERE trade_id=? AND episode_type='TRADE'
            AND reason='TRADE_CLOSED' LIMIT 1""",
        (trade_id,),
    ):
        return

    # No live audit row (legacy trade, external path, or already finalized)
    if not trow:
        _log.warning("trade_audit_finalize: no trade row id=%s", trade_id)
        return
    tr = dict(trow)
    if not base:
        base = {"synthetic_closed": True, "trade_snapshot": tr}
    base["exit"] = exit_block
    base["monitor_log"] = mon
    watch_history_insert(
        ticker=str(tr.get("ticker") or "?"),
        added_ts=float(tr.get("open_ts") or exit_ts),
        ended_ts=float(exit_ts),
        reason="TRADE_CLOSED",
        reviews=0,
        initial_score=init_sc,
        peak_score=init_sc,
        final_score=init_sc,
        final_decision="CLOSED",
        final_reason=fr,
        trade_id=trade_id,
        alert_id=int(tr["alert_id"]) if tr.get("alert_id") else None,
        episode_type="TRADE",
        audit=base,
    )


def trade_audit_ensure_open_for_resume(trade_id: int) -> None:
    """If an OPEN trade was resumed from SQLite but has no ``TRADE_OPEN`` audit, create a stub."""
    ex = fetchone(
        "SELECT id FROM watch_history WHERE trade_id=? AND episode_type='TRADE' AND reason='TRADE_OPEN'",
        (trade_id,),
    )
    if ex:
        return
    t = fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not t:
        return
    tr = dict(t)
    aid = int(tr["alert_id"]) if tr.get("alert_id") else None
    ot = float(tr.get("open_ts") or time.time())
    audit = {
        "resumed_after_restart": True,
        "trade_id": trade_id,
        "ticker": tr.get("ticker"),
        "entry_price": tr.get("entry_price"),
        "tp": tr.get("tp"),
        "stop": tr.get("stop"),
        "quantity": tr.get("quantity"),
        "alert_id": aid,
    }
    try:
        trade_audit_open(
            trade_id=trade_id,
            alert_id=aid,
            ticker_t212=str(tr.get("ticker") or "?"),
            added_ts=ot,
            audit=audit,
        )
    except Exception:
        _log.exception("trade_audit_ensure_open_for_resume failed trade_id=%s", trade_id)


def trade_audit_note_external_close(
    trade_id: int,
    *,
    reason: str,
    exit_ts: float | None = None,
    risk_extra: dict[str, Any] | None = None,
) -> None:
    """Broker flat / reconcile without going through the monitor — still log full audit."""
    ts = float(exit_ts) if exit_ts is not None else time.time()
    risk = {"note": "external_close_or_reconcile"}
    if risk_extra:
        risk = {**risk, **risk_extra}
    trade_audit_finalize(
        trade_id,
        exit_ts=ts,
        exit_reason=reason,
        risk_at_exit=risk,
        close_order_id=None,
    )


def wipe_all_tables() -> None:
    """Delete all AI sandbox rows (SQLite). Safe to call while idle; restart the engine after.

    **Keeps** :func:`t212_blacklist` rows (dead / close-only symbols). Also clears usage /
    quota tables. Resets AUTOINCREMENT rowid sequences for a clean id space.
    """
    _TABLES_CLEAR = (
        "monitor_log",
        "trades",
        "scores",
        "alerts",
        "watch_queue",
        "watch_history",
        "news_scanner_log",
        "offering_blocks",
        "gemini_usage_log",
        "openai_usage_log",
        "ticker_states",
        "ai_decisions",
        "news_web_search_quota",
        "news_web_search_ticker_cool",
    )
    with _lock:
        c = _connect()
        for table in _TABLES_CLEAR:
            c.execute(f"DELETE FROM {table}")
        for table in _TABLES_CLEAR:
            c.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        # Keep t212_blacklist — survives wipe so we do not re-spend on dead symbols.


def reset_scanner_feed_files() -> None:
    """Truncate the relay JSONL feed and reset the tail offset (fresh scanner stream only)."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.SCANNER_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.SCANNER_FEED_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass
    try:
        config.SCANNER_FEED_POS_PATH.write_text("0", encoding="utf-8")
    except OSError:
        pass
    try:
        config.NEWS_SCANNER_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.NEWS_SCANNER_FEED_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass
    try:
        config.NEWS_SCANNER_FEED_POS_PATH.write_text("0", encoding="utf-8")
    except OSError:
        pass


def wipe_ai_trade_state() -> None:
    wipe_all_tables()
    reset_scanner_feed_files()


def watch_mark_reviewed(
    ticker: str,
    *,
    score: int,
    decision: dict[str, Any],
) -> None:
    execute(
        """UPDATE watch_queue
              SET score=?, decision_json=?, last_decision=?,
                  last_reviewed_ts=?, reviews=reviews+1
            WHERE ticker=?""",
        (
            int(score),
            json.dumps(decision, default=str),
            decision.get("decision"),
            time.time(),
            ticker.upper(),
        ),
    )


def recent_ticker_alerts(ticker: str, hours: float, limit: int = 50) -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600.0
    rows = fetchall(
        "SELECT id, ts, type, raw, news_class FROM alerts WHERE ticker=? AND ts>=? ORDER BY ts DESC LIMIT ?",
        (ticker.upper(), cutoff, limit),
    )
    return [dict(r) for r in rows]
