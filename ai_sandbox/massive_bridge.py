"""Bridge to Trading Platform Massive.com websocket + 1m AM cache.

Loads ``MASSIVE_API_KEY`` from Trading Platform ``.env`` (via app.py dotenv).
When ``AI_MASSIVE_WS=1``, live ticks and completed 1m closes come from
``massive_l1_cache``. Default is off so only Trading Platform holds the WS.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from . import config

_log = logging.getLogger("ai_sandbox.massive_bridge")

_AITP_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _AITP_ROOT.parent
_TP_ROOT = _REPO_ROOT / "Trading Platform"
if _TP_ROOT.is_dir() and str(_TP_ROOT) not in sys.path:
    sys.path.insert(0, str(_TP_ROOT))


def _ensure_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env", override=False)
    load_dotenv(_TP_ROOT / ".env", override=False)
    load_dotenv(_AITP_ROOT / ".env", override=False)


_ensure_env()


def _import_massive():
    from Trading_AI import massive_chart, massive_l1_cache, massive_rest, massive_ws_streamer

    return massive_chart, massive_l1_cache, massive_rest, massive_ws_streamer


def ws_enabled() -> bool:
    """True when this app may open/maintain its own Massive websocket."""
    return config.massive_ws_enabled()


def enabled() -> bool:
    if not ws_enabled():
        return False
    try:
        _, _, mr, mws = _import_massive()
        return bool(mr.massive_enabled() and mws.should_run_massive_stream())
    except Exception:
        return False


def scanner_symbol(ticker: str) -> str:
    tk = (ticker or "").strip().upper().lstrip("$")
    if tk.endswith("_US_EQ"):
        tk = tk.split("_", 1)[0]
    return tk


def start_streams() -> bool:
    """Start Massive websocket on the running asyncio loop (idempotent)."""
    if not ws_enabled():
        _log.info("Massive websocket disabled for AI sandbox (AI_MASSIVE_WS=0)")
        return False
    if not enabled():
        _log.info("Massive stream not started (no API key or MASSIVE_WS_QUOTES off)")
        return False
    try:
        _, mlc, _, mws = _import_massive()
        mws.start_streams()
        _log.info("Massive websocket streamer started for AI sandbox")
        return True
    except Exception:
        _log.exception("Massive start_streams failed")
        return False


def stream_status() -> dict[str, Any]:
    if not ws_enabled():
        return {
            "enabled": False,
            "ai_ws_enabled": False,
            "reason": "AI_MASSIVE_WS=0",
        }
    try:
        _, _, _, mws = _import_massive()
        st = mws.stream_status()
        st["ai_ws_enabled"] = True
        return st
    except Exception as exc:
        return {"enabled": False, "ai_ws_enabled": True, "error": str(exc)[:120]}


def register_symbols(symbols: list[str]) -> None:
    if not enabled():
        return
    try:
        _, _, _, mws = _import_massive()
        clean = [scanner_symbol(s) for s in symbols if scanner_symbol(s)]
        if clean:
            mws.register_extra_symbols(clean)
    except Exception:
        _log.debug("Massive register_symbols failed", exc_info=True)


async def refresh_subscriptions() -> None:
    if not enabled():
        return
    try:
        _, _, _, mws = _import_massive()
        await mws.refresh_wanted_symbols()
        await mws.prime_snapshots()
    except Exception:
        _log.debug("Massive refresh_subscriptions failed", exc_info=True)


def live_price(ticker: str) -> float | None:
    if not enabled():
        return None
    try:
        _, mlc, _, _ = _import_massive()
        sym = scanner_symbol(ticker)
        px = mlc.get_price(sym)
        return float(px) if px and px > 0 else None
    except Exception:
        return None


def bar_version(ticker: str) -> int:
    if not enabled():
        return 0
    try:
        _, mlc, _, _ = _import_massive()
        return int(mlc.bar_version(scanner_symbol(ticker)))
    except Exception:
        return 0


async def wait_bar_update(ticker: str, *, since_version: int, timeout: float) -> bool:
    if not enabled():
        return False
    try:
        _, mlc, _, _ = _import_massive()
        return await mlc.wait_bar_update(
            scanner_symbol(ticker),
            timeout=float(timeout),
            since_version=int(since_version),
        )
    except Exception:
        return False


def last_closed_1m_close(ticker: str) -> float | None:
    if not enabled():
        return None
    try:
        _, mlc, _, _ = _import_massive()
        px = mlc.get_last_closed_1m_close(scanner_symbol(ticker))
        return float(px) if px and px > 0 else None
    except Exception:
        return None


def last_closed_1m_bar(ticker: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    try:
        _, mlc, _, _ = _import_massive()
        bar = mlc.get_last_closed_1m_bar(scanner_symbol(ticker))
        return dict(bar) if bar else None
    except Exception:
        return None


async def last_closed_1m_close_async(ticker: str) -> float | None:
    """Websocket AM cache first, REST fallback."""
    px = last_closed_1m_close(ticker)
    if px is not None:
        return px
    if not enabled():
        return None
    try:
        mc, _, _, _ = _import_massive()
        return await mc.last_closed_1m_close(scanner_symbol(ticker))
    except Exception:
        return None
