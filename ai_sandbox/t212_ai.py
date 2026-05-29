"""Trading 212 client for the AI sandbox account.

Deliberately independent of ``Trading_AI.t212`` because:
- ``Trading_AI.t212`` runs on the **production** bot loop with ``TRADING_212_KEY``
  / ``TRADING_212_SECRET`` — separate credentials and separate ``GET /equity/positions``
  rate limits from this module.
- The AI sandbox runs on its own asyncio loop (daemon thread). **Exactly one**
  task here polls ``GET /equity/positions`` for the AI account; everyone else
  reads the shared snapshot via :func:`get_positions` / :func:`broker_quote_long_qty`.

If ``AI_TRADING_ENABLED=0`` the AI sandbox engine also stands down (no scanners,
Gemini, or monitors). Here, order APIs are short-circuited to a no-op (still
logged). If credentials are missing, the same stubs apply. Live vs paper is
controlled by ``T212_ENV_AI`` (defaults to ``live``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from curl_cffi import requests as _ccrequests

from . import config

_log = logging.getLogger("ai_sandbox.t212_ai")

_RATE_LOCKS: dict[int, asyncio.Lock] = {}  # one lock per event loop
_LAST_CALL_MONO: dict[str, float] = {}

# Shared snapshot from ``run_positions_poller`` — single GET /equity/positions
# producer for the AI account; consumers never hit HTTP here.
_POSITIONS_LOCK = asyncio.Lock()
_POSITIONS_CACHE: list[dict[str, Any]] | None = None
_POSITIONS_CACHE_MONO: float = 0.0

# Shared account summary from ``run_account_summary_poller`` (GBP cash + totals).
_ACCOUNT_LOCK = asyncio.Lock()
_ACCOUNT_CACHE: dict[str, Any] | None = None
_ACCOUNT_CACHE_MONO: float = 0.0


def _lock_for_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _RATE_LOCKS.get(id(loop))
    if lk is None:
        lk = asyncio.Lock()
        _RATE_LOCKS[id(loop)] = lk
    return lk
_MIN_GAP = {
    "positions": 1.05,
    "orders": 1.05,
    "limit": 2.05,
    "stop_limit": 2.05,
    "market": 1.3,
    "cancel": 1.3,
    "history_orders": 10.2,
    "account": 5.05,
    "default": 0.6,
}


class T212AIError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"T212 HTTP {status}: {body!r}")
        self.status = status
        self.body = body


def _remember_max_open_qty(t212_code: str, broker_max_total: float) -> None:
    """Cache broker max position size learned from a reject or instruments map."""
    code = (t212_code or "").strip().upper()
    if not code:
        return
    try:
        v = float(broker_max_total)
    except (TypeError, ValueError):
        return
    if v > 0:
        _AI_MAX_OPEN[code] = v
        head = code.split("_", 1)[0].upper()
        if head:
            _AI_MAX_OPEN[head] = v


async def _buy_qty_after_max_position_error(
    t212_code: str,
    attempted_qty: float,
    body: Any,
) -> float | None:
    """Return a smaller buy size after ``max-position-quantity-exceeded``, or None if flat out."""
    max_hint = _parse_max_qty_from_body(body)
    if not max_hint or max_hint <= 0:
        return None
    _remember_max_open_qty(t212_code, max_hint)
    retry_qty = await qty_for_buy_under_cap(t212_code, attempted_qty, max_hint)
    if retry_qty > 0:
        return retry_qty
    return None


class T212MaxPositionError(T212AIError):
    """Raised when the account cannot add any more size for this instrument."""

    def __init__(self, status: int, body: Any, *, t212_code: str, attempted_qty: float):
        super().__init__(status, body)
        self.t212_code = t212_code
        self.attempted_qty = attempted_qty


def _parse_max_qty_from_text(text: str | None) -> float | None:
    """Extract broker max qty from ``max-position-quantity-exceeded`` detail (same regex set as trading_ai/order_flow)."""
    if not text:
        return None
    s = text.strip()
    patterns = (
        r"maximum position quantity.*?is\s+([0-9]+(?:\.[0-9]+)?)",
        r"is\s+([0-9]+(?:\.[0-9]+)?)",
        r"(?:maximum|max)\s*(?:position\s*)?(?:quantity|qty|allowed)?\s*(?:for this instrument\s*)?(?:is|=|:)?\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:can't|cannot|can't)\s+exceed\s+([0-9]+(?:\.[0-9]+)?)",
        r"limited\s+to\s+([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:shares?|units?|qty)\s*$",
    )
    for pat in patterns:
        m = re.search(pat, s, re.I)
        if m:
            try:
                q = float(m.group(1))
                return q if q > 0 else None
            except ValueError:
                continue
    return None


def _parse_max_qty_from_body(body: Any) -> float | None:
    if isinstance(body, dict):
        q = _parse_max_qty_from_text(str(body.get("detail") or ""))
        if q is not None:
            return q
        for k in ("maxQuantity", "maxAllowedQuantity", "allowedQuantity"):
            raw = body.get(k)
            if raw is None:
                continue
            try:
                v = float(raw)
                return v if v > 0 else None
            except (TypeError, ValueError):
                continue
        try:
            return _parse_max_qty_from_text(json.dumps(body))
        except Exception:
            pass
    return None


def _is_max_position_qty_error(body: Any) -> bool:
    """True when T212 rejects because total position cannot exceed broker max (often penny / volatile names)."""
    blob = ""
    if isinstance(body, dict):
        blob = json.dumps(body).lower()
    elif body:
        blob = str(body).lower()
    et = ""
    if isinstance(body, dict):
        et = str(body.get("type") or "").lower()
    detail = ""
    if isinstance(body, dict) and body.get("detail"):
        detail = str(body["detail"]).lower()
    return (
        "max-position-quantity-exceeded" in et
        or "max-position-quantity-exceeded" in blob
        or "max position quantity" in detail
        or "max position quantity" in blob
    )


def invalidate_positions_cache() -> None:
    """Clear snapshot so readers see empty until the positions poller refreshes.

    Does not stop the poller; next successful poll repopulates state.
    """
    global _POSITIONS_CACHE
    _POSITIONS_CACHE = None


async def wait_for_positions_cache(timeout: float = 45.0) -> bool:
    """Block until the positions poller has populated at least one snapshot."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        async with _POSITIONS_LOCK:
            if _POSITIONS_CACHE is not None:
                return True
        await asyncio.sleep(0.4)
    return False


async def broker_long_quantity(
    t212_code: str,
    *,
    retries: int = 4,
    retry_delay_s: float = 1.5,
) -> float:
    """Long quantity from the shared positions snapshot, with brief retries."""
    tgt = (t212_code or "").strip().upper()
    if not tgt:
        return 0.0
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        _px, qty = await broker_quote_long_qty(tgt, bypass_cache=False)
        if qty is not None and qty > 1e-6:
            return float(qty)
        if attempt + 1 < attempts:
            await asyncio.sleep(retry_delay_s)
    return 0.0


def _normalize_position_row(p: dict[str, Any]) -> dict[str, Any]:
    row = dict(p)
    inst = row.get("instrument")
    if isinstance(inst, dict):
        if not row.get("ticker") and inst.get("ticker"):
            row["ticker"] = inst["ticker"]
    ap_alt = row.get("averagePricePaid")
    if row.get("averagePrice") is None and ap_alt is not None:
        row["averagePrice"] = ap_alt
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
    if "/orders/market" in pl:
        return "market"
    if method == "DELETE" and "/orders/" in pl:
        return "cancel"
    if "/equity/positions" in pl:
        return "positions"
    if "/equity/account/" in pl:
        return "account"
    if "/equity/orders" in pl:
        return "orders"
    return "default"


async def _throttle(key: str) -> None:
    gap = _MIN_GAP.get(key, _MIN_GAP["default"])
    async with _lock_for_loop():
        last = _LAST_CALL_MONO.get(key, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL_MONO[key] = time.monotonic()


def _auth() -> tuple[str, str]:
    k, s = config.t212_credentials()
    if not k or not s:
        raise T212AIError(0, {"detail": "TRADING_212_KEY_AI / TRADING_212_SECRET_AI missing"})
    return k, s


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
    url = f"{config.t212_base_url()}{path}"
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
            _log.warning("AI T212 rate limited (%s %s) — backing off", method, path)
            await asyncio.sleep(2.0)
            continue
        if 200 <= status < 300:
            return body
        if status in (401, 403):
            raise T212AIError(status, body)
        if status == 408 and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        raise T212AIError(status, body)
    if last_err:
        raise T212AIError(0, {"detail": f"network: {last_err}"})
    raise T212AIError(0, {"detail": "unknown failure"})


def _round(p: float) -> float:
    return round(float(p), 4)


def round_qty(q: float) -> float:
    v = float(q)
    r = round(v, 4)
    if r == int(r):
        return float(int(r))
    return r


def _suppressed(action: str, **kw) -> dict[str, Any]:
    _log.info("AI %s suppressed (enabled=%s, creds_ok=%s) %s",
              action, config.trading_enabled(), config.t212_credentials_ok(), kw)
    return {"stub": True, "ts": time.time(), **kw}


# ── ticker resolution (own cache + main bot's map as fallback) ─────────────

# AI sandbox keeps its own instrument cache populated by its own T212 client.
# This avoids cross-loop coupling with the main bot's ticker_map (which has
# asyncio.Locks bound to the main bot's event loop) AND avoids the chicken-
# and-egg problem where the main bot only builds its map after Gemini's first
# trade — meaning the AI sandbox could be live for an hour with an empty map.
_AI_MAP: dict[str, str] = {}
_AI_DISPLAY_RAW: dict[str, str] = {}
_AI_MIN_TRADE_QTY: dict[str, float] = {}
_AI_PRECISION: dict[str, int] = {}
_AI_MAX_OPEN: dict[str, float] = {}
_AI_MAP_BUILT_TS: float = 0.0

# Same as ``Trading_AI.ticker_map`` — broker code → newer public symbol for UI.
_INSTRUMENT_DISPLAY_OVERRIDES: dict[str, str] = {
    "ONTX_US_EQ": "TRAW",
    "SGBX_US_EQ": "OLOX",
    "SGBX": "OLOX",
}

# Scanner / Discord symbols → T212 instrument when metadata still lists the old code.
_SCANNER_INSTRUMENT_ALIASES: dict[str, str] = {
    "OLOX": "SGBX_US_EQ",
}


async def refresh_ticker_map(force: bool = False) -> int:
    """Build / refresh the AI sandbox's own copy of the T212 instrument map.

    Returns the number of entries indexed. Safe to call repeatedly — honours
    :func:`config.ai_t212_instrument_map_ttl_seconds` unless ``force=True``.
    Errors are swallowed and logged.
    """
    global _AI_MAP_BUILT_TS
    ttl = float(config.ai_t212_instrument_map_ttl_seconds())
    if not force and _AI_MAP and (time.time() - _AI_MAP_BUILT_TS) < ttl:
        return len(_AI_MAP)
    try:
        instruments = await request("GET", "/equity/metadata/instruments")
    except Exception as exc:
        _log.warning("AI sandbox ticker-map refresh failed: %s", exc)
        return len(_AI_MAP)
    if not isinstance(instruments, list):
        _log.warning("AI sandbox ticker-map: unexpected response shape %r", type(instruments))
        return len(_AI_MAP)

    new: dict[str, str] = {}
    new_disp: dict[str, str] = {}
    new_min_q: dict[str, float] = {}
    new_prec: dict[str, int] = {}
    new_max_open: dict[str, float] = {}
    for inst in instruments:
        if not isinstance(inst, dict):
            continue
        code = inst.get("ticker")
        if not isinstance(code, str) or not code:
            continue
        try:
            prec = int(inst.get("quantityPrecision") or 0)
        except (TypeError, ValueError):
            prec = 0
        new_prec[code] = max(0, prec)
        new[code] = code
        head = code.split("_", 1)[0]
        short = inst.get("shortName")
        if isinstance(short, str) and short.strip():
            new_disp[code] = short.strip().upper()
        else:
            new_disp[code] = head or code
        ovr = _INSTRUMENT_DISPLAY_OVERRIDES.get(code.strip().upper())
        if ovr:
            new_disp[code] = ovr
        for _mq_key in (
            "minTradeQuantity",
            "minimumTradeQuantity",
            "minQuantity",
            "minimumQuantity",
        ):
            raw_mq = inst.get(_mq_key)
            if raw_mq is None:
                continue
            try:
                mq = float(raw_mq)
                if mq > 0 and mq == mq:
                    new_min_q[code] = mq
                    if head:
                        new_min_q.setdefault(head, mq)
            except (TypeError, ValueError):
                pass
        if head and head not in new:
            new[head] = code
        raw_mx = inst.get("maxOpenQuantity")
        try:
            if raw_mx is not None:
                mx = float(raw_mx)
                if mx > 0 and mx == mx:
                    new_max_open[code] = mx
                    if head:
                        new_max_open.setdefault(head, mx)
        except (TypeError, ValueError):
            pass
    for sym, inst_code in _SCANNER_INSTRUMENT_ALIASES.items():
        sym_u = sym.strip().upper()
        inst_u = inst_code.strip().upper()
        if sym_u and inst_u:
            new[sym_u] = inst_u
            ovr = _INSTRUMENT_DISPLAY_OVERRIDES.get(inst_u)
            if ovr:
                new_disp[inst_u] = ovr
    for inst in instruments:
        if not isinstance(inst, dict):
            continue
        code = inst.get("ticker")
        short = inst.get("shortName")
        if isinstance(code, str) and isinstance(short, str):
            up = short.strip().upper()
            if up and up not in new:
                new[up] = code

    if new:
        _AI_MAP.clear()
        _AI_MAP.update(new)
        _AI_DISPLAY_RAW.clear()
        _AI_DISPLAY_RAW.update(new_disp)
        _AI_MIN_TRADE_QTY.clear()
        _AI_MIN_TRADE_QTY.update(new_min_q)
        _AI_PRECISION.clear()
        _AI_PRECISION.update(new_prec)
        _AI_MAX_OPEN.clear()
        _AI_MAX_OPEN.update(new_max_open)
        _AI_MAP_BUILT_TS = time.time()
        sample_keys: set[str] = set()
        for inst in instruments[:5]:
            if isinstance(inst, dict):
                sample_keys.update(str(k) for k in inst.keys())
        _log.info(
            "AI sandbox built T212 ticker map (%d entries, sample fields: %s)",
            len(_AI_MAP),
            ", ".join(sorted(sample_keys)) if sample_keys else "?",
        )
    return len(_AI_MAP)


def instrument_map_ready() -> bool:
    """True once ``/equity/metadata/instruments`` has been loaded into memory."""
    return bool(_AI_MAP) and _AI_MAP_BUILT_TS > 0


def resolve_ticker(raw: str | None) -> str | None:
    """Resolve a raw scanner ticker (e.g. ``CREG``) to a T212 instrument code
    (e.g. ``CREG_US_EQ``).

    Tries the AI sandbox's own cache first, then falls back to the main bot's
    ticker_map dict (in case it has entries we don't yet). Returns ``None``
    if the symbol is genuinely unknown to T212.

    Safety: when using the Trading_AI fallback, the resolved T212 code head
    (e.g. ``RYB`` from ``RYB_US_EQ``) must match the scanner ticker exactly.
    If they differ and the scanner ticker is not a known alias, the mapping is
    treated as a stale/corrupt cross-mapping and discarded.  This prevents
    trading the wrong instrument (e.g. MYND resolving to RYB_US_EQ).
    """
    if not raw:
        return None
    raw_up = raw.strip().upper().lstrip("$")
    if not raw_up:
        return None
    alias = _SCANNER_INSTRUMENT_ALIASES.get(raw_up)
    if alias:
        return alias
    code = _AI_MAP.get(raw_up)
    if code:
        return code
    if "_" in raw_up and raw_up in _AI_MAP.values():
        return raw_up
    try:
        from Trading_AI import ticker_map  # type: ignore
        code = ticker_map._MAP.get(raw_up)  # noqa: SLF001
        if code:
            code_head = code.split("_", 1)[0].upper()
            if code_head != raw_up:
                # The T212 code head doesn't match the scanner ticker.
                # This is a stale or cross-mapped entry (e.g. MYND→RYB_US_EQ).
                # Only trust it if the scanner ticker is a documented alias.
                _log.warning(
                    "resolve_ticker: BLOCKED Trading_AI mapping %s→%s "
                    "(code head %r ≠ scanner ticker %r) — add to "
                    "_SCANNER_INSTRUMENT_ALIASES if this rename is intentional",
                    raw_up, code, code_head, raw_up,
                )
                return None
            return code
        if "_" in raw_up and raw_up in ticker_map._MAP.values():
            return raw_up
    except Exception:
        pass
    return None


def display_raw_for(t212_code: str | None) -> str:
    """Human-facing symbol for a T212 instrument code (uses ``shortName`` from metadata)."""
    if not t212_code:
        return ""
    code = t212_code.strip().upper()
    ovr = _INSTRUMENT_DISPLAY_OVERRIDES.get(code)
    if ovr:
        return ovr
    if code in _AI_DISPLAY_RAW:
        return _AI_DISPLAY_RAW[code]
    head = code.split("_", 1)[0]
    ovr_head = _INSTRUMENT_DISPLAY_OVERRIDES.get(head)
    if ovr_head:
        return ovr_head
    return head or code


def minimum_buy_quantity(t212_code: str) -> float:
    """Smallest order size we will send when the slot-based qty floors to zero or below broker minimum."""
    prec = quantity_precision(t212_code)
    head = (t212_code or "").split("_", 1)[0].upper()
    code = (t212_code or "").strip().upper()
    broker_min: float | None = None
    if code in _AI_MIN_TRADE_QTY:
        broker_min = float(_AI_MIN_TRADE_QTY[code])
    elif head and head in _AI_MIN_TRADE_QTY:
        broker_min = float(_AI_MIN_TRADE_QTY[head])
    if broker_min is not None and broker_min > 0:
        return snap_quantity(broker_min, prec)
    if prec <= 0:
        return 1.0
    unit = 10 ** (-prec)
    return snap_quantity(max(unit, 0.01), prec)


def _is_min_order_qty_error(body: Any) -> bool:
    blob = ""
    if isinstance(body, dict):
        blob = json.dumps(body).lower()
    elif body:
        blob = str(body).lower()
    detail = ""
    if isinstance(body, dict) and body.get("detail"):
        detail = str(body["detail"]).lower()
    return (
        ("minimum" in detail and ("quantity" in detail or "order" in detail or "size" in detail))
        or "below the minimum" in detail
        or "minimum quantity" in blob
        or "minimum order" in blob
    )


def _parse_min_qty_hint_from_body(body: Any) -> float | None:
    if isinstance(body, dict):
        q = _parse_max_qty_from_text(str(body.get("detail") or ""))
        if q is not None:
            return q
    try:
        return _parse_max_qty_from_text(json.dumps(body))
    except Exception:
        return None


def is_close_only_error(body: Any) -> bool:
    """True when T212 rejects new buys because the instrument is untradeable.

    Covers both close-only mode (instrument-close-only-mode) and the generic
    'Instrument can not be traded' broker rejection seen on VCIG-type blocks.
    Both warrant an immediate day-long blacklist so we stop wasting AI grades.
    """
    _NOT_TRADEABLE_PHRASES = (
        "instrument-close-only-mode",
        "close-only",
        "close only",
        "instrument can not be traded",
        "instrument cannot be traded",
        "cannot be traded",
        "can not be traded",
        "not tradeable",
        "not tradable",
    )
    if isinstance(body, dict):
        et = str(body.get("type") or "").lower().replace("\\", "/")
        detail = str(body.get("detail") or "").lower()
        try:
            blob = json.dumps(body).lower()
        except Exception:
            blob = ""
        haystack = et + " " + detail + " " + blob
    elif body:
        haystack = str(body).lower()
    else:
        return False
    return any(phrase in haystack for phrase in _NOT_TRADEABLE_PHRASES)


def quantity_precision(t212_code: str) -> int:
    """Decimal places allowed by T212 for this instrument's quantity (0 = whole shares)."""
    if not t212_code:
        return 0
    if t212_code in _AI_PRECISION:
        return int(_AI_PRECISION[t212_code])
    try:
        from Trading_AI import ticker_map  # type: ignore
        return int(ticker_map._PRECISION.get(t212_code, 0))  # noqa: SLF001
    except Exception:
        return 0


def snap_quantity(quantity: float, precision: int) -> float:
    """Floor *quantity* to *precision* decimals — never round up.

    Mirrors ``Trading_AI.ticker_map.snap_quantity`` so we honour the broker's
    fractional-share rules without over-ordering.
    """
    import math
    q = float(quantity)
    if precision <= 0:
        return float(math.floor(q))
    factor = 10 ** precision
    return math.floor(q * factor) / factor


def max_open_quantity_for(t212_code: str | None) -> float | None:
    """Return broker ``maxOpenQuantity`` for *t212_code*, if cached from instruments snapshot."""
    if not t212_code:
        return None
    code = t212_code.strip().upper()
    if code in _AI_MAX_OPEN:
        v = float(_AI_MAX_OPEN[code])
        return v if v > 0 else None
    head = code.split("_", 1)[0].upper()
    if head and head in _AI_MAX_OPEN:
        v = float(_AI_MAX_OPEN[head])
        return v if v > 0 else None
    return None


async def account_long_quantity(t212_code: str) -> float:
    """Current long quantity on this account for instrument *t212_code* (0 if flat)."""
    tgt = (t212_code or "").strip().upper()
    if not tgt:
        return 0.0
    try:
        for pos in await get_positions():
            if isinstance(pos, dict) and pos.get("ticker") == tgt:
                q = float(pos.get("quantity") or 0.0)
                return q if q > 0 else 0.0
    except Exception:
        _log.warning("positions fetch failed during account_long_quantity for %s", t212_code)
    return 0.0


async def qty_for_buy_under_cap(
    t212_code: str, desired_qty: float, broker_max_total_position: float
) -> float:
    """Desired buy size floored so total position stays at or under *broker_max_total_position*."""
    prec = quantity_precision(t212_code)
    want = snap_quantity(round_qty(float(desired_qty)), prec)
    try:
        cap_tot = float(broker_max_total_position)
    except (TypeError, ValueError):
        return want
    if cap_tot <= 0:
        return snap_quantity(0.0, prec)
    held = await account_long_quantity(t212_code)
    room_raw = float(cap_tot) - float(held)
    room = snap_quantity(max(0.0, room_raw), prec)
    out = min(want, room)
    return snap_quantity(round_qty(out), prec)


async def cap_order_buy_quantity(t212_code: str, desired_qty: float) -> float:
    """Size order to ``min(desired, maxOpenQuantity − holdings)`` when metadata exposes a limit."""
    prec = quantity_precision(t212_code)
    want = snap_quantity(round_qty(float(desired_qty)), prec)
    mq = max_open_quantity_for(t212_code)
    if mq is None:
        return want
    out = await qty_for_buy_under_cap(t212_code, want, mq)
    if out < want:
        h = await account_long_quantity(t212_code)
        _log.info(
            "AI %s sizing to broker allowance: qty %.6f → %.6f (max_open=%s held=%.6f)",
            t212_code,
            want,
            out,
            mq,
            h,
        )
    return out


# ── public surface ─────────────────────────────────────────────────────────


def _normalize_history_next_path(next_path: str | None) -> str | None:
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


def _hist_coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _history_order_parts(item: dict[str, Any]) -> tuple[Any, str, dict[str, Any], dict[str, Any]]:
    ord_raw = item.get("order")
    ord_data: dict[str, Any] = ord_raw if isinstance(ord_raw, dict) else {}
    fill_raw = item.get("fill")
    fill_data: dict[str, Any] = fill_raw if isinstance(fill_raw, dict) else {}
    if not ord_data and isinstance(item.get("id"), (int, str, float)):
        ord_data = item
    oid = ord_data.get("id") if ord_data else None
    status = str(ord_data.get("status") or "").strip().upper()
    return oid, status, ord_data, fill_data


def _avg_fill_price_usd(ord_data: dict[str, Any], fill_data: dict[str, Any]) -> float | None:
    px = _hist_coerce_float(fill_data.get("price"))
    if px and px > 0:
        return px
    fq = ord_data.get("filledQuantity")
    fv = ord_data.get("filledValue")
    try:
        q = float(fq)
        v = float(fv)
        if q > 0 and v > 0:
            return round(v / q, 6)
    except (TypeError, ValueError):
        pass
    return _hist_coerce_float(ord_data.get("averagePrice")) or _hist_coerce_float(
        ord_data.get("filledPrice")
    )


def _wallet_realised_gbp(fill_data: dict[str, Any], ord_data: dict[str, Any]) -> float | None:
    for blob in (fill_data, ord_data):
        wi = blob.get("walletImpact") if isinstance(blob, dict) else None
        if isinstance(wi, dict):
            hit = _hist_coerce_float(wi.get("realisedProfitLoss"))
            if hit is not None:
                return hit
    return None


async def fetch_order_fill_from_history(
    order_id: Any,
    ticker: str,
    *,
    max_attempts: int = 8,
    initial_delay_s: float = 2.0,
    page_walk: int = 35,
) -> tuple[float | None, float | None, float | None]:
    """Best-effort FILLED row from ``GET /equity/history/orders`` for *any* order id.

    Works for **buy** and **sell** fills. Pending orders disappear from
    ``GET /equity/orders/{id}`` once filled; history holds average fill, quantity,
    and for closes ``walletImpact.realisedProfitLoss`` (typically GBP).

    Returns ``(avg_fill_usd, realised_pnl_gbp_or_None, filled_qty)``.
    """
    if not config.t212_credentials_ok():
        return None, None, None
    want_id = str(order_id).strip()
    if not want_id:
        return None, None, None

    async def _scan_once() -> tuple[float | None, float | None, float | None] | None:
        async for page in iter_order_history_pages(ticker, limit=50, max_pages=page_walk):
            for item in page:
                if not isinstance(item, dict):
                    continue
                oid, status, ord_data, fill_data = _history_order_parts(item)
                if str(oid).strip() != want_id or status != "FILLED":
                    continue
                avg_px = _avg_fill_price_usd(ord_data, fill_data)
                realised = _wallet_realised_gbp(fill_data, ord_data)
                fq_raw = ord_data.get("filledQuantity") or ord_data.get("quantity")
                fill_qty: float | None = None
                try:
                    fq = float(fq_raw)
                    fill_qty = fq if fq > 1e-9 else None
                except (TypeError, ValueError):
                    fill_qty = None
                _log.info(
                    "AI order history FILLED order=%s ticker=%s avg_px=%s realised_gbp=%s qty=%s",
                    want_id,
                    ticker,
                    avg_px,
                    realised,
                    fill_qty,
                )
                return (avg_px, realised, fill_qty)
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
                "AI fetch_order_fill_from_history order=%s attempt=%s: %s",
                want_id,
                attempt + 1,
                exc,
            )
    _log.warning(
        "AI order fill not found in T212 history order=%s ticker=%s after %s attempts",
        want_id,
        ticker,
        max_attempts,
    )
    return None, None, None


# Back-compat name used by older call sites.
fetch_closed_order_fill_details = fetch_order_fill_from_history


async def backfill_closed_trade_pnl_from_broker(
    trade_id: int,
    *,
    ticker: str,
    close_order_id: str,
    entry_price: float | None = None,
) -> float | None:
    """Update ``trades.pnl_gbp`` from T212 order history ``realisedProfitLoss`` (GBP)."""
    from . import db

    oid = str(close_order_id or "").strip()
    if not oid:
        return None
    avg_px, realised, fill_qty = await fetch_order_fill_from_history(oid, ticker, page_walk=50)
    if realised is None:
        return None
    exit_px = avg_px if avg_px is not None and avg_px > 0 else None
    entry = float(entry_price or 0.0)
    pnl_pct: float | None = None
    if exit_px is not None and entry > 0:
        pnl_pct = round((exit_px - entry) / entry * 100.0, 4)
    db.execute(
        """UPDATE trades SET pnl_gbp=?, exit_price=COALESCE(?, exit_price),
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
    return round(float(realised), 4)


async def get_order(order_id: str | int) -> dict[str, Any]:
    if not config.t212_credentials_ok():
        return {}
    res = await request("GET", f"/equity/orders/{order_id}")
    return res if isinstance(res, dict) else {}


async def cancel_pending_open_order(order_id: str | int | None) -> bool:
    """Cancel a resting entry order regardless of AI_TRADING_ENABLED (cleanup after timeout).

    Mirrors production behaviour: we always want stray limits lifted when fills time out.
    """
    if order_id in (None, "", "0", 0):
        return False
    if not config.t212_credentials_ok():
        return False
    try:
        await request("DELETE", f"/equity/orders/{order_id}")
        return True
    except T212AIError:
        _log.warning("cancel_pending_open_order(%s) failed", order_id)
        return False


async def run_positions_poller() -> None:
    """Background loop: the **only** HTTP caller for ``GET /equity/positions`` on the AI account.

    All monitors, Flask bridges, reconcilers, and fills read the shared snapshot via
    :func:`get_positions` (never performs HTTP).

    Uses the same ``request`` throttle key as before (~1 req/s per API key); the main
    trading bot uses ``Trading_AI.t212`` with different credentials — independent limits.
    """
    _log.info("AI T212 positions poller started (single producer for /equity/positions)")
    while True:
        if not config.t212_credentials_ok():
            await asyncio.sleep(2.0)
            continue
        if not config.trading_enabled():
            await asyncio.sleep(2.0)
            continue
        try:
            res = await request("GET", "/equity/positions")
            parsed = _positions_from_body(res)
            async with _POSITIONS_LOCK:
                global _POSITIONS_CACHE, _POSITIONS_CACHE_MONO
                _POSITIONS_CACHE = parsed
                _POSITIONS_CACHE_MONO = time.monotonic()
        except (T212AIError, Exception) as exc:
            if not isinstance(exc, T212AIError):
                _log.warning("AI positions poller unexpected error: %s", exc)
            elif exc.status == 429:
                _log.debug("AI positions poller rate limited — keeping last snapshot")
            else:
                _log.warning(
                    "AI positions poller failed (%s): %s",
                    getattr(exc, "status", "?"),
                    getattr(exc, "body", exc),
                )
            await asyncio.sleep(1.0)


def _normalize_account_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Flatten T212 account summary into dashboard-friendly cash fields (broker currency)."""
    cash = summary.get("cash") if isinstance(summary.get("cash"), dict) else {}
    inv = summary.get("investments") if isinstance(summary.get("investments"), dict) else {}
    out: dict[str, Any] = {
        "currency": str(summary.get("currency") or "GBP").upper(),
        "free": cash.get("availableToTrade"),
        "blocked": cash.get("reservedForOrders"),
        "pieCash": cash.get("inPies"),
        "total": summary.get("totalValue"),
        "invested": inv.get("totalCost"),
        "currentValue": inv.get("currentValue"),
        "ppl": inv.get("unrealizedProfitLoss"),
        "result": inv.get("realizedProfitLoss"),
    }
    return out


async def run_account_summary_poller() -> None:
    """Background loop: the **only** HTTP caller for ``GET /equity/account/summary``."""
    _log.info("AI T212 account summary poller started (single producer for /equity/account/summary)")
    while True:
        if not config.t212_credentials_ok():
            await asyncio.sleep(2.0)
            continue
        if not config.trading_enabled():
            await asyncio.sleep(2.0)
            continue
        try:
            res = await request("GET", "/equity/account/summary")
            if isinstance(res, dict):
                parsed = _normalize_account_summary(res)
                async with _ACCOUNT_LOCK:
                    global _ACCOUNT_CACHE, _ACCOUNT_CACHE_MONO
                    _ACCOUNT_CACHE = parsed
                    _ACCOUNT_CACHE_MONO = time.monotonic()
        except (T212AIError, Exception) as exc:
            if not isinstance(exc, T212AIError):
                _log.warning("AI account summary poller unexpected error: %s", exc)
            elif exc.status == 429:
                _log.debug("AI account summary poller rate limited — keeping last snapshot")
            else:
                _log.warning(
                    "AI account summary poller failed (%s): %s",
                    getattr(exc, "status", "?"),
                    getattr(exc, "body", exc),
                )
        await asyncio.sleep(5.0)


def cash_snapshot() -> dict[str, Any] | None:
    """Latest account cash metrics (sync, no HTTP). Populated by :func:`run_account_summary_poller`."""
    if _ACCOUNT_CACHE is None:
        return None
    return dict(_ACCOUNT_CACHE)


async def get_cash() -> dict[str, Any]:
    """Return cached account cash/summary metrics (never hits the network)."""
    if not config.t212_credentials_ok():
        return {"error": "no_credentials"}
    async with _ACCOUNT_LOCK:
        if _ACCOUNT_CACHE is not None:
            return dict(_ACCOUNT_CACHE)
    return {"error": "cache_empty"}


async def get_positions(*, bypass_cache: bool = False) -> list[dict[str, Any]]:
    """Return the latest AI-account positions snapshot.

    Populated exclusively by :func:`run_positions_poller` (single HTTP producer).
    Never hits the network. ``bypass_cache`` is retained for backwards compatibility.
    """
    _ = bypass_cache
    if not config.t212_credentials_ok():
        return []
    async with _POSITIONS_LOCK:
        if _POSITIONS_CACHE is None:
            return []
        return [dict(x) for x in _POSITIONS_CACHE]


async def position_row_for_ticker(t212_code: str, *, bypass_cache: bool = False) -> dict[str, Any] | None:
    tgt = (t212_code or "").strip().upper()
    if not tgt:
        return None
    rows = await get_positions(bypass_cache=bypass_cache)
    for r in rows:
        if isinstance(r, dict) and str(r.get("ticker") or "").strip().upper() == tgt:
            return r
    return None


async def broker_quote_long_qty(
    t212_code: str, *, bypass_cache: bool = False
) -> tuple[float | None, float]:
    """Broker ``currentPrice`` for the open long (from positions) plus long quantity."""
    row = await position_row_for_ticker(t212_code, bypass_cache=bypass_cache)
    if not row:
        return None, 0.0
    q = float(row.get("quantity") or 0.0)
    long_q = q if q > 0 else 0.0
    raw = row.get("currentPrice")
    if raw is None:
        return None, long_q
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None, long_q
    return (px if px == px else None), long_q


async def position_average_entry_usd(t212_code: str, *, bypass_cache: bool = True) -> float | None:
    """Broker ``averagePrice`` for the open long from ``GET /equity/positions``."""
    row = await position_row_for_ticker(t212_code, bypass_cache=bypass_cache)
    if not row:
        return None
    raw = row.get("averagePrice")
    if raw is None:
        return None
    try:
        ap = float(raw)
    except (TypeError, ValueError):
        return None
    return ap if ap > 0 and ap == ap else None


def _position_unrealized_pct_from_row(row: dict[str, Any]) -> float | None:
    """Derive live unrealized P&L % from a normalized positions snapshot row."""
    for key in ("pplPercentage", "pplPct", "unrealizedProfitLossPercent", "unrealisedProfitLossPercent"):
        raw = row.get(key)
        if raw is not None:
            try:
                pct = float(raw)
                if pct == pct:
                    return pct
            except (TypeError, ValueError):
                pass
    try:
        ap = float(row.get("averagePrice") or 0.0)
        cp = float(row.get("currentPrice") or 0.0)
        if ap > 0 and cp > 0:
            return (cp - ap) / ap * 100.0
    except (TypeError, ValueError):
        pass
    try:
        ap = float(row.get("averagePrice") or 0.0)
        qty = abs(float(row.get("quantity") or 0.0))
        ppl = row.get("ppl")
        if ap > 0 and qty > 0 and ppl is not None:
            cost = ap * qty
            if cost > 0:
                return float(ppl) / cost * 100.0
    except (TypeError, ValueError):
        pass
    return None


async def position_unrealized_pct(t212_code: str, *, bypass_cache: bool = False) -> float | None:
    """Live unrealized P&L % for *t212_code* from the shared positions snapshot."""
    row = await position_row_for_ticker(t212_code, bypass_cache=bypass_cache)
    if not row:
        return None
    return _position_unrealized_pct_from_row(row)


def wallet_metrics_from_row(row: dict[str, Any]) -> dict[str, float | None]:
    """GBP walletImpact fields plus derived unrealized % for dashboard / monitor."""
    wi = row.get("walletImpact")
    if not isinstance(wi, dict):
        wi = {}
    out: dict[str, float | None] = {
        "total_cost_gbp": None,
        "current_value_gbp": None,
        "unreal_gbp": None,
        "fx_impact_gbp": None,
        "unreal_pct": _position_unrealized_pct_from_row(row),
    }
    for src, dst in (
        ("totalCost", "total_cost_gbp"),
        ("currentValue", "current_value_gbp"),
        ("unrealizedProfitLoss", "unreal_gbp"),
        ("unrealisedProfitLoss", "unreal_gbp"),
        ("fxImpact", "fx_impact_gbp"),
    ):
        raw = wi.get(src)
        if raw is None and dst == "unreal_gbp":
            raw = row.get("ppl")
        if raw is None:
            continue
        try:
            val = float(raw)
            if val == val:
                out[dst] = val
        except (TypeError, ValueError):
            pass
    if out["unreal_pct"] is None and out["total_cost_gbp"] and out["unreal_gbp"] is not None:
        cost = float(out["total_cost_gbp"])
        if cost > 0:
            out["unreal_pct"] = round(float(out["unreal_gbp"]) / cost * 100.0, 4)
    return out


async def position_wallet_metrics(t212_code: str, *, bypass_cache: bool = False) -> dict[str, float | None] | None:
    row = await position_row_for_ticker(t212_code, bypass_cache=bypass_cache)
    if not row:
        return None
    return wallet_metrics_from_row(row)


async def place_limit(ticker: str, quantity: float, limit_price: float) -> dict[str, Any]:
    if not config.trading_enabled() or not config.t212_credentials_ok():
        return _suppressed("place_limit", ticker=ticker, qty=quantity, lim=limit_price)
    prec = quantity_precision(ticker)
    qty = snap_quantity(round_qty(float(quantity)), prec)
    for attempt in range(6):
        sent = qty
        try:
            return await request(
                "POST",
                "/equity/orders/limit",
                {
                    "ticker": ticker,
                    "quantity": round_qty(sent),
                    "limitPrice": _round(limit_price),
                    "timeValidity": "DAY",
                },
            )
        except T212AIError as exc:
            if exc.status != 400:
                raise
            if _is_max_position_qty_error(exc.body):
                retry_qty = await _buy_qty_after_max_position_error(ticker, sent, exc.body)
                if retry_qty is not None and retry_qty > 0 and retry_qty != sent:
                    _log.info(
                        "AI place_limit %s: max-position error → qty %.6f → %.6f",
                        ticker,
                        sent,
                        retry_qty,
                    )
                    qty = retry_qty
                    continue
                raise T212MaxPositionError(
                    exc.status,
                    exc.body,
                    t212_code=ticker,
                    attempted_qty=sent,
                ) from exc
            if _is_min_order_qty_error(exc.body):
                hint = _parse_min_qty_hint_from_body(exc.body)
                floor_min = minimum_buy_quantity(ticker)
                target_min = floor_min
                if hint and hint > 0:
                    target_min = max(floor_min, snap_quantity(hint, prec))
                bumped = await cap_order_buy_quantity(ticker, max(sent, target_min))
                if bumped > sent:
                    _log.info(
                        "AI place_limit %s: minimum-qty / value error → qty %.6f → %.6f",
                        ticker,
                        sent,
                        bumped,
                    )
                    qty = bumped
                    continue
            raise


async def cancel_order(order_id: int | str) -> dict[str, Any]:
    if not config.trading_enabled() or not config.t212_credentials_ok():
        return _suppressed("cancel_order", id=order_id)
    return await request("DELETE", f"/equity/orders/{order_id}")


async def place_market(ticker: str, quantity: float) -> dict[str, Any]:
    if not config.trading_enabled() or not config.t212_credentials_ok():
        return _suppressed("place_market", ticker=ticker, qty=quantity)
    prec = quantity_precision(ticker)
    qty = snap_quantity(round_qty(float(quantity)), prec)
    for attempt in range(6):
        sent = qty
        try:
            return await request(
                "POST",
                "/equity/orders/market",
                {"ticker": ticker, "quantity": round_qty(sent), "extendedHours": True},
            )
        except T212AIError as exc:
            if exc.status != 400:
                raise
            if _is_max_position_qty_error(exc.body):
                retry_qty = await _buy_qty_after_max_position_error(ticker, sent, exc.body)
                if retry_qty is not None and retry_qty > 0 and retry_qty != sent:
                    _log.info(
                        "AI place_market %s: max-position error → qty %.6f → %.6f",
                        ticker,
                        sent,
                        retry_qty,
                    )
                    qty = retry_qty
                    continue
                raise T212MaxPositionError(
                    exc.status,
                    exc.body,
                    t212_code=ticker,
                    attempted_qty=sent,
                ) from exc
            if _is_min_order_qty_error(exc.body):
                hint = _parse_min_qty_hint_from_body(exc.body)
                floor_min = minimum_buy_quantity(ticker)
                target_min = floor_min
                if hint and hint > 0:
                    target_min = max(floor_min, snap_quantity(hint, prec))
                bumped = await cap_order_buy_quantity(ticker, max(sent, target_min))
                if bumped > sent:
                    _log.info(
                        "AI place_market %s: minimum-qty / value error → qty %.6f → %.6f",
                        ticker,
                        sent,
                        bumped,
                    )
                    qty = bumped
                    continue
            raise


async def fetch_active_orders() -> list[dict[str, Any]]:
    if not config.t212_credentials_ok():
        return []
    res = await request("GET", "/equity/orders")
    if isinstance(res, list):
        return [x for x in res if isinstance(x, dict)]
    return []


async def cancel_all_active_orders() -> int:
    orders = await fetch_active_orders()
    n = 0
    for o in orders:
        oid = o.get("id")
        if oid in (None, "", 0):
            continue
        try:
            await cancel_order(oid)
            n += 1
        except Exception as exc:
            _log.warning("cancel_all_active_orders id=%s: %s", oid, exc)
    return n


async def close_all_positions_market() -> list[dict[str, Any]]:
    """Market-sell every open long on the AI T212 account."""
    if not config.t212_credentials_ok():
        return []
    try:
        raw = await request("GET", "/equity/positions")
        rows = raw if isinstance(raw, list) else []
    except Exception as exc:
        _log.warning("close_all_positions_market positions fetch: %s", exc)
        rows = await get_positions(bypass_cache=True)
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tk = str(row.get("ticker") or "").strip()
        try:
            qty = float(row.get("quantity") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if not tk or qty <= 1e-6:
            continue
        sell_signed = -round_qty(snap_quantity(qty, quantity_precision(tk)))
        try:
            res = await place_market(tk, sell_signed)
            out.append({"ticker": tk, "quantity": sell_signed, "result": res})
        except Exception as exc:
            out.append({"ticker": tk, "error": str(exc)})
            _log.warning("close_all_positions_market %s failed: %s", tk, exc)
    return out
