"""Trading 212 REST client (async wrapper over curl_cffi for Cloudflare bypass).

Uses the **main** bot credentials (``TRADING_212_KEY`` / ``TRADING_212_SECRET``). The AI
sandbox account is separate: ``ai_sandbox.t212_ai`` with ``TRADING_212_KEY_AI`` / ``SECRET_AI``,
including its own ``GET /equity/positions`` rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from curl_cffi import requests as _ccrequests

from . import config
from .broker_errors import BrokerError

_log = logging.getLogger("Trading_AI.t212")


class T212Error(BrokerError):
    """Trading 212 REST failure."""



_RATE_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_CALL: dict[str, float] = {}
_MIN_GAP = {
    "limit": 2.05,
    "stop_limit": 2.05,
    "stop": 2.05,
    "market": 1.3,
    "cancel": 1.3,
    "positions": 1.05,  # published limit 1 req / 1s for GET /equity/positions
    "orders": 1.05,
    # Published: 6 req / 60s for GET /equity/history/orders
    "history_orders": 10.2,
    "default": 0.6,
}


_POSITIONS_LOCK = asyncio.Lock()
_POSITIONS_CACHE: list[dict[str, Any]] | None = None
_POSITIONS_CACHE_MONO: float = 0.0
# Minimum wall-clock gap between actual GET /equity/positions HTTP calls process-wide.
_POSITIONS_HTTP_MIN_GAP_SEC = 1.05


def _normalize_position_row(p: dict[str, Any]) -> dict[str, Any]:
    """Merge legacy/portfolio shapes with GET /equity/positions responses."""
    row = dict(p)
    inst = row.get("instrument")
    if isinstance(inst, dict):
        if not row.get("ticker") and inst.get("ticker"):
            row["ticker"] = inst["ticker"]
    ap_alt = row.get("averagePricePaid")
    if row.get("averagePrice") is None and ap_alt is not None:
        row["averagePrice"] = ap_alt
    # Public API often omits top-level ppl; unrealised P/L is in walletImpact (account currency).
    if row.get("ppl") is None:
        wi = row.get("walletImpact")
        if isinstance(wi, dict):
            unrl = wi.get("unrealizedProfitLoss")
            if unrl is None:
                unrl = wi.get("unrealisedProfitLoss")
            if unrl is not None:
                try:
                    row["ppl"] = float(unrl)
                except (TypeError, ValueError):
                    pass
    return row


def _positions_from_body(body: Any) -> list[dict[str, Any]]:
    raw: list[Any]
    if isinstance(body, list):
        raw = body
    elif isinstance(body, dict):
        inner = body.get("positions")
        raw = inner if isinstance(inner, list) else []
    else:
        raw = []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(_normalize_position_row(item))
    return out


def _rate_key(method: str, path: str) -> str:
    pl = path.lower()
    if "/equity/history/orders" in pl:
        return "history_orders"
    if "/orders/limit" in pl:
        return "limit"
    if "/orders/stop_limit" in pl:
        return "stop_limit"
    if "/orders/stop" in pl:
        return "stop"
    if "/orders/market" in pl:
        return "market"
    if method == "DELETE" and "/orders/" in pl:
        return "cancel"
    if "/equity/positions" in pl or "/equity/portfolio" in pl:
        return "positions"
    if "/equity/orders" in pl:
        return "orders"
    return "default"


async def _throttle(key: str) -> None:
    lock = _RATE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        gap = _MIN_GAP.get(key, _MIN_GAP["default"])
        last = _LAST_CALL.get(key, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL[key] = time.monotonic()


def _auth() -> tuple[str, str]:
    key, secret = config.t212_credentials()
    if not key or not secret:
        raise T212Error(0, {"detail": "TRADING_212_KEY / TRADING_212_SECRET missing in .env"})
    return key, secret


def _do_request(method: str, url: str, json_body: dict[str, Any] | None) -> tuple[int, Any]:
    auth = _auth()
    if method == "GET":
        r = _ccrequests.get(url, auth=auth, impersonate="chrome", timeout=30)
    elif method == "POST":
        r = _ccrequests.post(url, auth=auth, json=json_body, impersonate="chrome", timeout=30)
    elif method == "DELETE":
        r = _ccrequests.delete(url, auth=auth, impersonate="chrome", timeout=30)
    else:
        raise ValueError(f"unsupported method {method}")
    try:
        body: Any = r.json()
    except Exception:
        body = r.text
    return r.status_code, body


async def request(method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
    base = config.t212_base_url()
    url = f"{base}{path}"
    key = _rate_key(method, path)
    await _throttle(key)
    loop = asyncio.get_running_loop()
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            status, body = await loop.run_in_executor(None, _do_request, method, url, json_body)
        except Exception as exc:
            last_err = exc
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if status == 429:
            _log.warning("T212 rate limited (%s %s) — backing off", method, path)
            await asyncio.sleep(2.0)
            continue
        if 200 <= status < 300:
            return body
        if status in (401, 403):
            raise T212Error(status, body)
        if status == 408 and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        raise T212Error(status, body)
    if last_err:
        raise T212Error(0, {"detail": f"network: {last_err}"})
    raise T212Error(0, {"detail": "unknown failure"})


async def get_cash() -> dict[str, Any]:
    return await request("GET", "/equity/account/cash")


async def get_instruments() -> list[dict[str, Any]]:
    res = await request("GET", "/equity/metadata/instruments")
    if isinstance(res, list):
        return res
    return []


async def get_positions() -> list[dict[str, Any]]:
    """Return open positions; at most one HTTP GET /equity/positions per ~1s for the whole process.

    Concurrent callers share the same cached snapshot when inside the window,
    satisfying Trading 212's per-account rate limit.
    """
    global _POSITIONS_CACHE, _POSITIONS_CACHE_MONO
    async with _POSITIONS_LOCK:
        now = time.monotonic()
        if _POSITIONS_CACHE is not None and (now - _POSITIONS_CACHE_MONO) < _POSITIONS_HTTP_MIN_GAP_SEC:
            return [dict(x) for x in _POSITIONS_CACHE]
        res = await request("GET", "/equity/positions")
        parsed = _positions_from_body(res)
        _POSITIONS_CACHE = parsed
        _POSITIONS_CACHE_MONO = time.monotonic()
        return [dict(x) for x in parsed]


async def get_order(order_id: str | int) -> dict[str, Any]:
    res = await request("GET", f"/equity/orders/{order_id}")
    return res if isinstance(res, dict) else {}


async def cancel_order(order_id: str | int | None) -> bool:
    if order_id in (None, "", 0):
        return False
    try:
        await request("DELETE", f"/equity/orders/{order_id}")
        return True
    except T212Error as exc:
        _log.warning("Cancel order %s failed: %s", order_id, exc)
        return False


async def place_limit(ticker: str, quantity: float, limit_price: float) -> dict[str, Any]:
    return await request(
        "POST",
        "/equity/orders/limit",
        {"ticker": ticker, "quantity": _round_qty(quantity), "limitPrice": _round(limit_price), "timeValidity": "DAY"},
    )


async def place_stop_limit(ticker: str, quantity: float, stop_price: float, limit_price: float) -> dict[str, Any]:
    return await request(
        "POST",
        "/equity/orders/stop_limit",
        {
            "ticker": ticker,
            "quantity": _round_qty(quantity),
            "stopPrice": _round(stop_price),
            "limitPrice": _round(limit_price),
            "timeValidity": "DAY",
        },
    )


async def place_stop(ticker: str, quantity: float, stop_price: float) -> dict[str, Any]:
    return await request(
        "POST",
        "/equity/orders/stop",
        {"ticker": ticker, "quantity": _round_qty(quantity), "stopPrice": _round(stop_price), "timeValidity": "DAY"},
    )


async def place_market(ticker: str, quantity: float) -> dict[str, Any]:
    return await request("POST", "/equity/orders/market", {"ticker": ticker, "quantity": _round_qty(quantity), "extendedHours": False})


async def get_live_price(ticker: str) -> float | None:
    """Best-effort live quote for a non-position instrument from T212."""
    if not ticker:
        return None
    paths = [
        f"/equity/price/{ticker}",
        f"/equity/metadata/instruments/{ticker}",
    ]
    for path in paths:
        try:
            res = await request("GET", path)
        except Exception:
            continue
        if isinstance(res, dict):
            for k in ("price", "currentPrice", "lastPrice", "bid", "ask"):
                v = res.get(k)
                try:
                    f = float(v)
                    if f == f and f > 0:
                        return f
                except (TypeError, ValueError):
                    continue
    return None


def _normalize_history_next_path(next_path: str | None) -> str | None:
    """Turn ``nextPagePath`` from T212 into a path + query usable with ``t212_base_url()``."""
    if not next_path or not isinstance(next_path, str):
        return None
    from urllib.parse import urlparse

    p = next_path.strip()
    if p.startswith("http://") or p.startswith("https://"):
        u = urlparse(p)
        p = (u.path or "") + (("?" + u.query) if u.query else "")
    p = p.strip()
    for prefix in ("/api/v0",):
        if p.startswith(prefix):
            p = p[len(prefix) :]
    if p and not p.startswith("/"):
        p = "/" + p
    return p if p else None


async def iter_order_history_pages(
    ticker: str,
    *,
    limit: int = 50,
    max_pages: int = 40,
):
    """Yield one page of ``items`` at a time for ``GET /equity/history/orders``.

    Uses the official ``ticker`` query parameter. Walks ``nextPagePath`` until
    exhausted or ``max_pages``. Rate-limited to respect 6 req/min.
    """
    from urllib.parse import urlencode

    safe_limit = max(1, min(int(limit), 50))
    path: str | None = None
    for _ in range(max(1, int(max_pages))):
        if path is None:
            path = "/equity/history/orders?" + urlencode({"limit": str(safe_limit), "ticker": ticker})
        res = await request("GET", path)
        path = None
        if not isinstance(res, dict):
            break
        items = res.get("items")
        page = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        yield page
        nxt = _normalize_history_next_path(res.get("nextPagePath"))
        if not nxt:
            break
        path = nxt


async def get_order_history(
    ticker: str,
    limit: int = 10,
    *,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    """Return merged order history rows for ``ticker`` (up to ``max_pages`` pages)."""
    out: list[dict[str, Any]] = []
    async for page in iter_order_history_pages(ticker, limit=limit, max_pages=max_pages):
        out.extend(page)
    return out


def _round(p: float) -> float:
    return round(float(p), 4)


def _round_qty(q: float) -> float:
    """Normalise a quantity to at most 4 decimal places before sending to T212.

    order_flow already snaps to the instrument's declared quantityPrecision
    (0 dp for most US equities).  This is a final safety net in case a quantity
    arrives here with floating-point noise (e.g. 3500.0000000003).
    """
    v = float(q)
    rounded = round(v, 4)
    # If the rounded value is a whole number, return it as int-cast float so
    # the JSON serialiser emits 3500 rather than 3500.0 — some T212 builds are
    # fussy about trailing zeros on integer quantities.
    if rounded == int(rounded):
        return float(int(rounded))
    return rounded
