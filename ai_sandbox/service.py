"""Process-level entrypoint. Started by main_website/app.py at boot."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from . import db
from .engine import Engine

_log = logging.getLogger("ai_sandbox.service")

_engine: Optional[Engine] = None
_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def get_engine() -> Optional[Engine]:
    return _engine


def start_in_background() -> None:
    """Start the engine on its own asyncio loop in a daemon thread.

    Safe to call multiple times — no-ops if already running.
    """
    global _engine, _thread, _loop
    if _thread is not None and _thread.is_alive():
        return
    db.init()
    _engine = Engine()

    def _runner() -> None:
        global _loop
        loop = asyncio.new_event_loop()
        _loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_engine.run())  # type: ignore[union-attr]
        except Exception:
            _log.exception("AI sandbox engine crashed")
        finally:
            loop.close()

    _thread = threading.Thread(target=_runner, name="ai-sandbox-engine", daemon=True)
    _thread.start()
    _log.info("AI sandbox engine thread started")
