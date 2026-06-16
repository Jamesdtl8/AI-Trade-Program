"""Shared price-data access for the AI sandbox.

Calls the same yfinance helpers as the dashboard so the cache is shared in
process — no second yfinance poller, no internal HTTP round trip.

If those helpers aren't importable (e.g. running the AI engine standalone for
a unit test) we degrade gracefully by hitting yfinance directly with a small
local TTL cache.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_log = logging.getLogger("ai_sandbox.price_data")

_local_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_local_lock = threading.Lock()
_LOCAL_TTL = 5.0


def _shared_quote(symbol: str) -> dict[str, Any] | None:
    """Use the dashboard's in-process Yahoo cache when available."""
    try:
        from app import _yahoo_fast_quote_batch  # type: ignore

        out = _yahoo_fast_quote_batch([symbol]) or {}
        return out.get(symbol)
    except Exception:
        return None


def _shared_history(symbol: str, tf: str) -> list[dict[str, Any]] | None:
    try:
        from app import _cached_yahoo_history  # type: ignore
        import yfinance as yf  # noqa: F401  # ensure available before fetch
    except Exception:
        return None

    def _fetch_bars():
        import yfinance as yf  # type: ignore

        df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
        out: list[dict[str, Any]] = []
        for ts, row in df.iterrows():
            out.append(
                {
                    "t": ts.isoformat(),
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                }
            )
        return out

    try:
        return _cached_yahoo_history(symbol, tf, _fetch_bars)  # type: ignore
    except Exception as exc:
        _log.warning("shared history %s failed: %s", symbol, exc)
        return None


def _direct_quote(symbol: str) -> dict[str, Any]:
    with _local_lock:
        cached = _local_cache.get(symbol)
        if cached and (time.time() - cached[0]) < _LOCAL_TTL:
            return cached[1]
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        df = t.history(period="1d", interval="1m", prepost=True)
        hist = t.history(period="5d", interval="1d")
        price = float(df["Close"].iloc[-1]) if len(df) else None
        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
        out = {
            "symbol": symbol,
            "p": price,
            "pc": prev,
            "h": float(df["High"].max()) if len(df) else None,
            "l": float(df["Low"].min()) if len(df) else None,
        }
    except Exception as exc:
        _log.warning("direct quote %s failed: %s", symbol, exc)
        out = {"symbol": symbol, "error": str(exc)[:120]}
    with _local_lock:
        _local_cache[symbol] = (time.time(), out)
    return out


def quote(symbol: str) -> dict[str, Any]:
    """Return current price + previous close for ``symbol``."""
    try:
        from . import massive_bridge

        if massive_bridge.enabled():
            sym = massive_bridge.scanner_symbol(symbol)
            px = massive_bridge.live_price(sym)
            if px and px > 0:
                return {"symbol": sym, "p": px, "price": px, "source": "massive"}
    except Exception:
        pass
    q = _shared_quote(symbol)
    if q and (q.get("p") is not None or q.get("price") is not None):
        return q
    return _direct_quote(symbol)


def yahoo_symbol(ticker: str) -> str:
    """Best-effort Yahoo symbol for a scanner / T212 ticker."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return ""
    if tk.endswith("_US_EQ"):
        tk = tk.split("_", 1)[0]
    return tk.lstrip("$")


def _parse_bar_ts(iso: str) -> float | None:
    if not iso:
        return None
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_HISTORY_TTL = 25.0


def _direct_history(symbol: str) -> list[dict[str, Any]]:
    """Fetch today's 1m bars directly from yfinance (fallback when shared cache empty)."""
    sym = yahoo_symbol(symbol) or symbol
    with _local_lock:
        cached = _history_cache.get(sym)
        if cached and (time.time() - cached[0]) < _HISTORY_TTL:
            return list(cached[1])
    try:
        import yfinance as yf

        df = yf.Ticker(sym).history(period="1d", interval="1m", prepost=True)
        out: list[dict[str, Any]] = []
        for ts, row in df.iterrows():
            bar = {
                "t": ts.isoformat(),
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
            }
            parsed = _parse_bar_ts(bar["t"])
            if parsed is not None:
                bar["ts"] = parsed
            out.append(bar)
    except Exception as exc:
        _log.warning("direct history %s failed: %s", sym, exc)
        out = []
    with _local_lock:
        _history_cache[sym] = (time.time(), out)
    return out


def candles_1m(symbol: str, count: int = 20) -> list[dict[str, Any]]:
    """Return last ``count`` × 1m candles (shared cache, then direct yfinance)."""
    sym = yahoo_symbol(symbol) or symbol
    bars = _shared_history(sym, "1MIN") or []
    if not bars:
        bars = _direct_history(sym)
    if not bars:
        return []
    out = []
    for b in bars[-count:]:
        row = dict(b)
        if "ts" not in row:
            ts = _parse_bar_ts(str(row.get("t") or ""))
            if ts is not None:
                row["ts"] = ts
        out.append(row)
    return out


_t212_buckets: dict[str, dict[str, Any]] = {}
_t212_bar_history: dict[str, list[dict[str, Any]]] = {}
_T212_HISTORY_MAX = 240


def record_t212_price(symbol: str, price: float, ts: float | None = None) -> dict[str, Any] | None:
    """Bucket T212 ticks into synthetic 1m closes; return completed bar on minute roll."""
    sym = yahoo_symbol(symbol) or symbol
    px = float(price)
    if px <= 0:
        return None
    now = float(ts if ts is not None else time.time())
    minute = int(now // 60) * 60
    state = _t212_buckets.get(sym)
    if state is None:
        _t212_buckets[sym] = {"minute": minute, "close": px}
        return None
    if minute > int(state["minute"]):
        completed_ts = float(state["minute"]) + 60.0
        bar = {
            "t": "",
            "ts": completed_ts,
            "o": float(state["close"]),
            "h": float(state["close"]),
            "l": float(state["close"]),
            "c": float(state["close"]),
            "v": 0,
            "source": "t212",
        }
        hist = _t212_bar_history.setdefault(sym, [])
        hist.append(bar)
        if len(hist) > _T212_HISTORY_MAX:
            del hist[: len(hist) - _T212_HISTORY_MAX]
        state["minute"] = minute
        state["close"] = px
        return bar
    state["close"] = px
    return None


def t212_synthetic_bars(symbol: str, *, count: int = 120) -> list[dict[str, Any]]:
    sym = yahoo_symbol(symbol) or symbol
    return list(_t212_bar_history.get(sym, [])[-count:])


def candles_1m_after(
    symbol: str,
    since_ts: float,
    *,
    count: int = 120,
    t212_price: float | None = None,
) -> list[dict[str, Any]]:
    """1m candles with bar close timestamp strictly after ``since_ts``."""
    try:
        from . import massive_bridge

        if massive_bridge.enabled():
            bar = massive_bridge.last_closed_1m_bar(symbol)
            if bar:
                end_ms = int(bar.get("e") or 0)
                ts = end_ms / 1000.0 if end_ms > 0 else 0.0
                if ts > float(since_ts):
                    return [
                        {
                            "ts": ts,
                            "c": float(bar.get("c") or 0),
                            "o": bar.get("o"),
                            "h": bar.get("h"),
                            "l": bar.get("l"),
                            "source": "massive_am",
                        }
                    ]
    except Exception:
        pass
    if t212_price is not None and float(t212_price) > 0:
        record_t212_price(symbol, float(t212_price))
    bars = candles_1m(symbol, count=count)
    if not bars:
        bars = t212_synthetic_bars(symbol, count=count)
    return [b for b in bars if float(b.get("ts") or 0) > float(since_ts)]


def last_price(symbol: str) -> float | None:
    """Best-effort spot — Massive websocket when enabled, else Yahoo."""
    try:
        from . import massive_bridge

        if massive_bridge.enabled():
            px = massive_bridge.live_price(symbol)
            if px and px > 0:
                return float(px)
    except Exception:
        pass
    q = quote(symbol)
    if not isinstance(q, dict):
        return None
    v = q.get("p") or q.get("price") or q.get("last") or q.get("regularMarketPrice")
    if v is None:
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def price_pack(symbol: str) -> dict[str, Any]:
    """Bundle everything a scorer / monitor call needs in one go."""
    q = quote(symbol)
    bars = candles_1m(symbol, 20)
    hod = max((b.get("h") or 0.0) for b in bars) if bars else None
    lod = min((b.get("l") or 0.0) for b in bars if b.get("l")) if bars else None
    return {
        "symbol": symbol,
        "price": q.get("p") or q.get("price"),
        "prev_close": q.get("pc"),
        "day_high": q.get("h"),
        "day_low": q.get("l"),
        "hod_recent": hod,
        "lod_recent": lod,
        "candles": bars,
    }
