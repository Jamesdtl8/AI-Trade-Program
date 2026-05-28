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
    q = _shared_quote(symbol)
    if q and (q.get("p") is not None or q.get("price") is not None):
        return q
    return _direct_quote(symbol)


def candles_1m(symbol: str, count: int = 20) -> list[dict[str, Any]]:
    """Return last ``count`` × 1m candles as compact dicts (uses shared cache)."""
    bars = _shared_history(symbol, "1MIN") or []
    if not bars:
        return []
    return bars[-count:]


def last_price(symbol: str) -> float | None:
    """Best-effort spot from Yahoo (same pipeline as dashboard)."""
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
