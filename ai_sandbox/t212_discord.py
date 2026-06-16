"""Trading 212 client for the Discord Trader account (separate credentials).

Mirrors the AI sandbox pattern: background pollers own HTTP; Flask/dashboard
reads cached snapshots only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import t212_ai

_log = logging.getLogger("t212_discord")

_LOOP: asyncio.AbstractEventLoop | None = None
_POSITIONS_LOCK = asyncio.Lock()
_POSITIONS_CACHE: list[dict[str, Any]] | None = None
_POSITIONS_CACHE_MONO: float = 0.0
_ACCOUNT_LOCK = asyncio.Lock()
_ACCOUNT_CACHE: dict[str, Any] | None = None
_ACCOUNT_CACHE_MONO: float = 0.0
_TICKER_MAP: dict[str, str] = {}
_INST_BY_ROOT: dict[str, dict[str, Any]] = {}
_INST_DISPLAY: dict[str, str] = {}
_MAP_BUILT_TS: float = 0.0
_MAP_TTL_S: float = 3600.0


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def credentials_ok() -> bool:
    return bool(_env("TRADING_212_KEY_DISCORD") and _env("TRADING_212_SECRET_DISCORD"))


def _base_url() -> str:
    env = _env("T212_ENV_DISCORD", "demo").lower()
    host = "https://live.trading212.com" if env == "live" else "https://demo.trading212.com"
    return f"{host}/api/v0"


def _auth() -> tuple[str, str]:
    return (_env("TRADING_212_KEY_DISCORD"), _env("TRADING_212_SECRET_DISCORD"))


# ── rate limiting (mirrors ai_sandbox.t212_ai) ────────────────────────────────

class T212DiscordError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"T212 HTTP {status}: {body!r}")
        self.status = status
        self.body = body


_RATE_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_CALL_MONO: dict[str, float] = {}
_MIN_GAP = {
    "positions": 1.05,
    "orders": 1.05,
    "limit": 2.05,
    "stop_limit": 2.05,
    "market": 1.25,
    "cancel": 1.25,
    "history_orders": 10.2,
    "account": 5.05,
    "default": 1.05,
}


def _lock_for_key(key: str) -> asyncio.Lock:
    lk = _RATE_LOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _RATE_LOCKS[key] = lk
    return lk


def _rate_key(method: str, path: str) -> str:
    pl = (path or "").lower()
    if "/orders/market" in pl:
        return "market"
    if method == "DELETE" and "/orders/" in pl:
        return "cancel"
    if "/equity/positions" in pl:
        return "positions"
    if "/equity/account/" in pl:
        return "account"
    if "/history/orders" in pl:
        return "history_orders"
    if "/equity/orders" in pl:
        return "orders"
    return "default"


async def _throttle(key: str) -> None:
    gap = _MIN_GAP.get(key, _MIN_GAP["default"])
    async with _lock_for_key(key):
        last = _LAST_CALL_MONO.get(key, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL_MONO[key] = time.monotonic()


def _do_request(method: str, url: str, json_body: dict[str, Any] | None) -> tuple[int, Any]:
    from curl_cffi import requests as _r

    auth = _auth()
    if method == "GET":
        r = _r.get(url, auth=auth, impersonate="chrome", timeout=30)
    elif method == "POST":
        r = _r.post(url, json=json_body, auth=auth, impersonate="chrome", timeout=30)
    elif method == "DELETE":
        r = _r.delete(url, auth=auth, impersonate="chrome", timeout=30)
    else:
        raise ValueError(f"unsupported method {method}")
    try:
        body: Any = r.json()
    except Exception:
        body = r.text
    return r.status_code, body


async def request(method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
    """Central T212 HTTP for Discord account — per-endpoint throttle + 429 backoff."""
    url = f"{_base_url()}{path}"
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
            backoff = 2.0 * (3 ** attempt)
            _log.warning(
                "Discord T212 rate limited (%s %s) — backing off %.0fs (attempt %d/3)",
                method, path, backoff, attempt + 1,
            )
            await asyncio.sleep(backoff)
            continue
        if 200 <= status < 300:
            return body
        if status in (401, 403):
            raise T212DiscordError(status, body)
        if status == 408 and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        raise T212DiscordError(status, body)
    if last_err:
        raise T212DiscordError(0, {"detail": f"network: {last_err}"})
    raise T212DiscordError(0, {"detail": "unknown failure"})


# ── positions snapshot (single HTTP producer) ───────────────────────────────

_positions_loop: asyncio.AbstractEventLoop | None = None
_positions_updated: asyncio.Event | None = None
_wake_poller: asyncio.Event | None = None
_positions_version: int = 0
_PENDING_ENTRIES: dict[str, float] = {}


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _LOOP
    _LOOP = loop


def loop() -> asyncio.AbstractEventLoop | None:
    return _LOOP


def _round_qty(q: float) -> float:
    return t212_ai.round_qty(q)


def _parse_min_qty_hint(detail: str) -> float | None:
    import re

    m = re.search(r"at least\s+([0-9]+(?:\.[0-9]+)?)", detail, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _bind_positions_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _positions_loop, _positions_updated, _wake_poller
    _positions_loop = loop
    _positions_updated = asyncio.Event()
    _wake_poller = asyncio.Event()


def _wake_positions_poller() -> None:
    ev = _wake_poller
    loop = _positions_loop
    if ev is None or loop is None or not loop.is_running():
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass


def _notify_positions_cache_updated() -> None:
    global _positions_version
    _positions_version += 1
    ev = _positions_updated
    loop = _positions_loop
    if ev is None or loop is None or not loop.is_running():
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass


def positions_version() -> int:
    return int(_positions_version)


async def wait_positions_update(*, since_version: int, timeout: float) -> bool:
    if positions_version() > since_version:
        return True
    ev = _positions_updated
    if ev is None:
        await asyncio.sleep(min(max(0.05, float(timeout)), 1.0))
        return positions_version() > since_version
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.05, float(timeout)))
    except asyncio.TimeoutError:
        return positions_version() > since_version
    return positions_version() > since_version


def register_pending_entry(t212_code: str) -> None:
    tk = (t212_code or "").strip().upper()
    if tk:
        _PENDING_ENTRIES[tk] = time.monotonic()
        _wake_positions_poller()


def clear_pending_entry(t212_code: str) -> None:
    tk = (t212_code or "").strip().upper()
    if tk:
        _PENDING_ENTRIES.pop(tk, None)


def has_pending_entries() -> bool:
    return bool(_PENDING_ENTRIES)


async def _ensure_instruments(*, force: bool = False) -> None:
    """Build scanner symbol → T212 instrument map (includes shortName aliases like NIVF→ASCA_US_EQ)."""
    global _TICKER_MAP, _INST_BY_ROOT, _INST_DISPLAY, _MAP_BUILT_TS
    if _TICKER_MAP and not force and (time.time() - _MAP_BUILT_TS) < _MAP_TTL_S:
        return
    try:
        data = await request("GET", "/equity/metadata/instruments")
    except Exception as exc:
        _log.warning("Discord instrument map failed: %s", exc)
        if not _TICKER_MAP:
            # Same instrument universe as AI sandbox — use its map if already loaded.
            if t212_ai.instrument_map_ready():
                _TICKER_MAP = dict(t212_ai._AI_MAP)
                _MAP_BUILT_TS = time.time()
        return
    ticker_map: dict[str, str] = {}
    inst_by_root: dict[str, dict[str, Any]] = {}
    inst_display: dict[str, str] = {}
    if isinstance(data, list):
        for inst in data:
            if not isinstance(inst, dict) or not inst.get("ticker"):
                continue
            code = str(inst["ticker"])
            code_u = code.upper()
            head = code.split("_")[0].upper()
            ticker_map[code_u] = code
            existing = inst_by_root.get(head)
            if existing is None or "_US_" in code:
                ticker_map[head] = code
                inst_by_root[head] = inst
            short = inst.get("shortName")
            if isinstance(short, str):
                up = short.strip().upper()
                if up:
                    ticker_map.setdefault(up, code)
                    inst_display[code_u] = up
    for sym, inst_code in t212_ai._SCANNER_INSTRUMENT_ALIASES.items():
        sym_u = sym.strip().upper()
        inst_u = inst_code.strip().upper()
        if sym_u and inst_u:
            ticker_map[sym_u] = inst_u
    _TICKER_MAP = ticker_map
    _INST_BY_ROOT = inst_by_root
    _INST_DISPLAY = inst_display
    _MAP_BUILT_TS = time.time()
    _log.info("Discord T212 instrument map ready (%d entries)", len(_TICKER_MAP))


async def resolve_ticker(scanner_ticker: str) -> str | None:
    raw = (scanner_ticker or "").strip().upper().lstrip("$")
    if not raw:
        return None
    await _ensure_instruments()
    alias = t212_ai._SCANNER_INSTRUMENT_ALIASES.get(raw)
    if alias:
        return alias
    hit = _TICKER_MAP.get(raw)
    if hit:
        return hit
    if "_" in raw and raw in _TICKER_MAP.values():
        return raw
    # Fallback to AI sandbox map (loaded in-process, same T212 metadata).
    fb = t212_ai.resolve_ticker(raw)
    if fb:
        return fb
    return None


def scanner_ticker_from_inst(inst: str) -> str:
    """T212 shortName for display/storage (what you see in the app and on Discord).

    Falls back to instrument ticker root when shortName is missing.
  Instrument-level dedupe uses ``instrument_for_ticker``, not this label.
    """
    inst_u = (inst or "").upper()
    if inst_u in _INST_DISPLAY:
        return _INST_DISPLAY[inst_u]
    return inst_u.split("_")[0].upper()


def normalize_ticker(ticker: str) -> str:
    """Map any alias (e.g. UGRO) to the T212 shortName (e.g. FLZH) when known."""
    raw = (ticker or "").strip().upper().lstrip("$")
    if not raw:
        return raw
    inst = instrument_for_ticker(raw)
    if inst:
        return scanner_ticker_from_inst(inst)
    return raw


def instrument_for_ticker(ticker: str) -> str | None:
    """Best-effort sync resolve: scanner symbol → T212 instrument code."""
    raw = (ticker or "").strip().upper().lstrip("$")
    if not raw:
        return None
    hit = _TICKER_MAP.get(raw)
    if hit:
        return hit.upper()
    if "_" in raw:
        return raw.upper()
    return f"{raw}_US_EQ"


def humanize_error(err: str | None) -> str:
    """Short dashboard-friendly rejection text."""
    if not err:
        return "Order rejected"
    e = str(err).strip()
    el = e.lower()
    if e == "instrument_not_found":
        return "Not listed on T212 (symbol not in instrument map)"
    if "no broker fill" in el:
        return "Order sent but no fill confirmed (rate limit or broker delay)"
    if "insufficient" in el:
        return "Insufficient funds"
    if "invalid payload" in el or "invalid-request" in el:
        return "Invalid order payload"
    if "instrument-close-only" in el or "cannot be traded" in el:
        return "Instrument not tradeable (close-only)"
    if "min-quantity" in el or "must trade at least" in el:
        return "Below minimum order size"
    # T212 JSON blob in DB
    if "detail" in el and "'detail'" in e:
        import ast
        try:
            blob = ast.literal_eval(e) if e.startswith("{") else None
            if isinstance(blob, dict) and blob.get("detail"):
                return str(blob["detail"])
        except Exception:
            pass
    return e[:200]


async def place_market(instrument_ticker: str, quantity: float, *, retries: int = 6) -> dict[str, Any]:
    """Market order — confirmed payload: {ticker, quantity, extendedHours}."""
    qty = _round_qty(quantity)
    last: dict[str, Any] | None = None
    for _ in range(retries):
        payload = {"ticker": instrument_ticker, "quantity": qty, "extendedHours": True}
        try:
            body = await request("POST", "/equity/orders/market", payload)
            register_pending_entry(instrument_ticker)
            return {"http_status": 201, "body": body}
        except T212DiscordError as exc:
            last = {"http_status": exc.status, "body": exc.body}
            if exc.status != 400 or not isinstance(exc.body, dict):
                return last
            body = exc.body
        detail = str(body.get("detail") or "")
        err_type = str(body.get("type") or "")
        dl = detail.lower()
        if "min-quantity" in err_type or "must trade at least" in dl:
            hint = _parse_min_qty_hint(detail)
            if hint and hint > qty:
                qty = _round_qty(hint)
                continue
        if "insufficient" in dl or "insufficient-free" in err_type:
            smaller = _round_qty(qty * 0.9)
            if 0 < smaller < qty:
                qty = smaller
                continue
        return last or {"http_status": 400, "body": body}
    return last or {"http_status": 0, "body": "no response"}


async def place_limit(
    instrument_ticker: str,
    quantity: float,
    limit_price: float,
    *,
    retries: int = 6,
) -> dict[str, Any]:
    """Limit order. Accepted outside regular hours but rests with extendedHours=false until RTH."""
    qty = _round_qty(quantity)
    price = t212_ai._round(float(limit_price))
    last: dict[str, Any] | None = None
    for _ in range(retries):
        payload = {
            "ticker": instrument_ticker,
            "quantity": qty,
            "limitPrice": price,
            "timeValidity": "GOOD_TILL_CANCEL",
        }
        try:
            body = await request("POST", "/equity/orders/limit", payload)
            if qty > 0:
                register_pending_entry(instrument_ticker)
            return {"http_status": 201, "body": body}
        except T212DiscordError as exc:
            last = {"http_status": exc.status, "body": exc.body}
            if exc.status != 400 or not isinstance(exc.body, dict):
                return last
            body = exc.body
        detail = str(body.get("detail") or "")
        err_type = str(body.get("type") or "")
        dl = detail.lower()
        if "min-quantity" in err_type or "must trade at least" in dl:
            hint = _parse_min_qty_hint(detail)
            if hint and abs(hint) > abs(qty):
                qty = _round_qty(hint if qty > 0 else -hint)
                continue
        if "insufficient" in dl or "insufficient-free" in err_type:
            smaller = _round_qty(qty * 0.9)
            if abs(smaller) > 0 and abs(smaller) < abs(qty):
                qty = smaller
                continue
        return last or {"http_status": 400, "body": body}
    return last or {"http_status": 0, "body": "no response"}


async def cancel_order(order_id: str | int | None) -> bool:
    oid = str(order_id or "").strip()
    if not oid or not credentials_ok():
        return False
    try:
        await request("DELETE", f"/equity/orders/{oid}")
        return True
    except T212DiscordError as exc:
        _log.warning("cancel_order(%s) failed: %s", oid, exc.body)
        return False


async def get_order(order_id: str | int) -> dict[str, Any]:
    if not credentials_ok():
        return {}
    try:
        res = await request("GET", f"/equity/orders/{order_id}")
        return res if isinstance(res, dict) else {}
    except T212DiscordError:
        return {}


async def wait_limit_fill(
    order_id: str | int,
    instrument_ticker: str,
    *,
    requested_qty: float,
    timeout_sec: float = 120.0,
    cancel_if_unfilled: bool = True,
) -> tuple[float, float] | None:
    """Poll order until filled; returns (qty, avg_price) or None."""
    from . import config

    deadline = time.time() + float(timeout_sec)
    threshold = float(config.FILL_PARTIAL_THRESHOLD)
    requested = abs(float(requested_qty))
    last_status = ""

    while time.time() < deadline:
        order = await get_order(order_id)
        if order:
            last_status = str(order.get("status") or "")
            filled = abs(float(order.get("filledQuantity") or 0))
            order_qty = abs(float(order.get("quantity") or requested))
            avg_v = order.get("filledValue")
            avg_price: float | None = None
            if avg_v and filled > 0:
                avg_price = float(avg_v) / filled
            if avg_price is None:
                for k in ("averagePrice", "filledPrice", "limitPrice"):
                    try:
                        v = float(order.get(k) or 0)
                        if v > 0:
                            avg_price = v
                            break
                    except (TypeError, ValueError):
                        pass
            if last_status == "FILLED" and filled > 0 and avg_price:
                clear_pending_entry(instrument_ticker.upper())
                return filled, avg_price
            if last_status == "PARTIALLY_FILLED" and order_qty > 0:
                if (filled / order_qty) >= threshold and avg_price:
                    clear_pending_entry(instrument_ticker.upper())
                    return filled, avg_price
            if last_status in ("REJECTED", "CANCELLED", "CANCELED", "EXPIRED"):
                return None
        else:
            pos_fill = await wait_entry_fill(
                instrument_ticker, requested, order_id=str(order_id), timeout_sec=2.0,
            )
            if pos_fill:
                return pos_fill
        await asyncio.sleep(2.0)

    if cancel_if_unfilled:
        await cancel_order(order_id)
        _log.warning(
            "limit order %s unfilled (%s) — cancelled", order_id, last_status or "TIMEOUT",
        )
    return None


async def run_positions_poller() -> None:
    """Single HTTP producer for GET /equity/positions — mirrors ai_sandbox.t212_ai."""
    from . import config, db as _db

    global _POSITIONS_CACHE, _POSITIONS_CACHE_MONO
    _log.info("Discord T212 positions poller started")
    bind_loop(asyncio.get_running_loop())
    _bind_positions_loop(asyncio.get_running_loop())
    _consecutive_429s = 0
    while True:
        if not credentials_ok():
            await asyncio.sleep(5.0)
            continue

        phase = config.market_phase()
        market_active = phase in ("regular", "extended")
        has_open = bool(
            _db.fetchone(
                "SELECT 1 FROM dt_trades WHERE status IN ('OPEN','SELL_PENDING') LIMIT 1"
            )
        )
        if has_open or has_pending_entries() or market_active:
            inter_poll_s = 1.0
        else:
            inter_poll_s = 60.0

        if _consecutive_429s > 0:
            backoff = min(60.0, inter_poll_s * (2 ** _consecutive_429s))
            await asyncio.sleep(backoff)
        else:
            wake = _wake_poller
            if wake is not None:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=inter_poll_s)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(inter_poll_s)

        try:
            res = await request("GET", "/equity/positions")
            parsed = t212_ai._positions_from_body(res)
            async with _POSITIONS_LOCK:
                _POSITIONS_CACHE = parsed
                _POSITIONS_CACHE_MONO = time.monotonic()
            _notify_positions_cache_updated()
            _consecutive_429s = 0
        except T212DiscordError as exc:
            if exc.status == 429:
                _consecutive_429s += 1
                pause = min(120.0, 15.0 * _consecutive_429s)
                _log.warning(
                    "Discord positions poller rate limited (streak=%d) — pausing %.0fs",
                    _consecutive_429s, pause,
                )
                await asyncio.sleep(pause)
            else:
                _log.warning("Discord positions poller failed (%s): %s", exc.status, exc.body)
                await asyncio.sleep(2.0)
        except Exception as exc:
            _log.warning("Discord positions poller unexpected error: %s", exc)
            await asyncio.sleep(2.0)


async def run_account_poller() -> None:
    global _ACCOUNT_CACHE, _ACCOUNT_CACHE_MONO
    _log.info("Discord T212 account poller started")
    while True:
        if not credentials_ok():
            await asyncio.sleep(5.0)
            continue
        try:
            raw = await request("GET", "/equity/account/cash")
            if isinstance(raw, dict):
                async with _ACCOUNT_LOCK:
                    _ACCOUNT_CACHE = raw
                    _ACCOUNT_CACHE_MONO = time.monotonic()
        except T212DiscordError as exc:
            if exc.status == 429:
                _log.warning("Discord account poller rate limited — pausing 30s")
                await asyncio.sleep(30.0)
                continue
            _log.warning("Discord account poller failed (%s): %s", exc.status, exc.body)
        except Exception as exc:
            _log.warning("Discord account poller failed: %s", exc)
        await asyncio.sleep(5.0)


async def get_positions(*, bypass_cache: bool = False) -> list[dict[str, Any]]:
    """Return cached positions — never hits the network (same as t212_ai)."""
    _ = bypass_cache
    if not credentials_ok():
        return []
    async with _POSITIONS_LOCK:
        if _POSITIONS_CACHE is None:
            return []
        return [dict(x) for x in _POSITIONS_CACHE]


def positions_snapshot_sync() -> list[dict[str, Any]]:
    """Read cached positions from Flask (no HTTP)."""
    if _POSITIONS_CACHE is None:
        return []
    return [dict(x) for x in _POSITIONS_CACHE]


def cash_snapshot() -> dict[str, Any] | None:
    if _ACCOUNT_CACHE is None:
        return None
    return dict(_ACCOUNT_CACHE)


def wallet_map() -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for p in positions_snapshot_sync():
        tk = str(p.get("ticker") or "").strip().upper()
        if tk:
            out[tk] = t212_ai.wallet_metrics_from_row(p)
    return out


async def wait_entry_fill(
    instrument_ticker: str,
    requested_qty: float,
    *,
    order_id: str | None = None,
    timeout_sec: float = 120.0,
) -> tuple[float, float] | None:
    """Confirm fill via positions snapshot — mirrors ai_sandbox.entry_fill.wait_market_fill."""
    from . import config

    deadline = time.time() + float(timeout_sec)
    threshold = float(config.FILL_PARTIAL_THRESHOLD)
    inst = instrument_ticker.upper()
    ver = positions_version()
    while time.time() < deadline:
        try:
            for pos in await get_positions():
                if str(pos.get("ticker") or "").upper() != inst:
                    continue
                qty = float(pos.get("quantity") or 0)
                ep = pos.get("averagePrice") or pos.get("averagePricePaid")
                try:
                    entry = float(ep) if ep is not None else 0.0
                except (TypeError, ValueError):
                    entry = 0.0
                if qty > 0 and entry > 0:
                    if requested_qty <= 0 or (qty / requested_qty) >= threshold:
                        clear_pending_entry(inst)
                        return qty, entry
        except Exception as exc:
            _log.warning("wait_entry_fill %s: %s", inst, exc)
        remain = deadline - time.time()
        if remain <= 0:
            break
        await wait_positions_update(since_version=ver, timeout=min(1.0, remain))
        ver = positions_version()

    if order_id:
        avg_px, _, fill_qty = await fetch_order_fill_from_history(
            order_id, inst, max_attempts=4, initial_delay_s=0.5,
        )
        if fill_qty and fill_qty > 0:
            clear_pending_entry(inst)
            return fill_qty, avg_px or 0.0
    clear_pending_entry(inst)
    return None


def wallet_metrics_from_row(row: dict[str, Any]) -> dict[str, float | None]:
    return t212_ai.wallet_metrics_from_row(row)


def _normalize_history_next_path(raw: Any) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    from urllib.parse import urlparse

    p = raw.strip()
    if not p:
        return None
    if p.startswith("http"):
        u = urlparse(p)
        p = (u.path or "") + (("?" + u.query) if u.query else "")
    for prefix in ("/api/v0",):
        if p.startswith(prefix):
            p = p[len(prefix):]
    if p and not p.startswith("/"):
        p = "/" + p
    return p if p else None


async def iter_order_history_pages(
    instrument_ticker: str,
    *,
    limit: int = 50,
    max_pages: int = 40,
):
    from urllib.parse import urlencode

    safe_limit = max(1, min(int(limit), 50))
    path: str | None = None
    for _ in range(max(1, int(max_pages))):
        if path is None:
            path = "/equity/history/orders?" + urlencode(
                {"limit": str(safe_limit), "ticker": instrument_ticker}
            )
        raw = await request("GET", path)
        path = None
        if not isinstance(raw, dict):
            break
        items = raw.get("items")
        page = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        yield page
        nxt = _normalize_history_next_path(raw.get("nextPagePath"))
        if not nxt:
            break
        path = nxt


async def fetch_order_fill_from_history(
    order_id: Any,
    instrument_ticker: str,
    *,
    max_attempts: int = 8,
    initial_delay_s: float = 2.0,
    page_walk: int = 35,
) -> tuple[float | None, float | None, float | None]:
    """FILLED row from order history — same contract as t212_ai."""
    want_id = str(order_id).strip()
    if not want_id or not credentials_ok():
        return None, None, None

    async def _scan_once() -> tuple[float | None, float | None, float | None] | None:
        async for page in iter_order_history_pages(instrument_ticker, limit=50, max_pages=page_walk):
            for item in page:
                if not isinstance(item, dict):
                    continue
                oid, status, ord_data, fill_data = t212_ai._history_order_parts(item)
                if str(oid).strip() != want_id or status != "FILLED":
                    continue
                avg_px = t212_ai._avg_fill_price_usd(ord_data, fill_data)
                realised = t212_ai._wallet_realised_gbp(fill_data, ord_data)
                fq_raw = ord_data.get("filledQuantity") or ord_data.get("quantity")
                fill_qty: float | None = None
                try:
                    fq = float(fq_raw)
                    fill_qty = fq if fq > 1e-9 else None
                except (TypeError, ValueError):
                    fill_qty = None
                return avg_px, realised, fill_qty
        return None

    await asyncio.sleep(max(0.0, float(initial_delay_s)))
    for attempt in range(max(1, int(max_attempts))):
        if attempt:
            await asyncio.sleep(5.0)
        try:
            hit = await _scan_once()
            if hit is not None:
                return hit
        except Exception as exc:
            _log.warning(
                "Discord fetch_order_fill_from_history order=%s attempt=%s: %s",
                want_id,
                attempt + 1,
                exc,
            )
    return None, None, None


async def backfill_closed_trade_pnl(
    trade_id: int,
    *,
    instrument_ticker: str,
    close_order_id: str,
    entry_price: float | None = None,
) -> float | None:
    from . import db

    oid = str(close_order_id or "").strip()
    if not oid:
        return None
    avg_px, realised, fill_qty = await fetch_order_fill_from_history(oid, instrument_ticker, page_walk=50)
    if realised is None:
        return None
    exit_px = avg_px if avg_px is not None and avg_px > 0 else None
    entry = float(entry_price or 0.0)
    pnl_pct: float | None = None
    if exit_px is not None and entry > 0:
        pnl_pct = round((exit_px - entry) / entry * 100.0, 4)
    db.execute(
        """UPDATE dt_trades SET pnl_gbp=?, exit_price=COALESCE(?, exit_price),
                              pnl_pct=COALESCE(?, pnl_pct),
                              quantity=COALESCE(?, quantity)
           WHERE id=? AND status IN ('SELL_PENDING', 'CLOSED')""",
        (
            round(float(realised), 4),
            exit_px,
            pnl_pct,
            fill_qty,
            int(trade_id),
        ),
    )
    return float(realised)
