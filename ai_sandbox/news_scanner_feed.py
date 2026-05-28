"""Tail Discord #news-scanner JSONL (separate from TrendVision scanner_feed)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from . import config

_log = logging.getLogger("ai_sandbox.news_scanner_feed")


def _read_pos() -> int:
    try:
        return int(config.NEWS_SCANNER_FEED_POS_PATH.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_pos(pos: int) -> None:
    try:
        config.NEWS_SCANNER_FEED_POS_PATH.write_text(str(pos), encoding="utf-8")
    except OSError as exc:
        _log.warning("news scanner pos write failed: %s", exc)


async def tail(interval: float = 1.0, *, start_at_end: bool = True) -> AsyncIterator[dict[str, Any]]:
    config.NEWS_SCANNER_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.NEWS_SCANNER_FEED_PATH.touch(exist_ok=True)

    pos = _read_pos()
    if pos == 0 and start_at_end:
        try:
            pos = config.NEWS_SCANNER_FEED_PATH.stat().st_size
            _write_pos(pos)
        except OSError:
            pos = 0

    while True:
        try:
            with open(config.NEWS_SCANNER_FEED_PATH, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size < pos:
                    pos = 0
                if size > pos:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                    pos = size
                    _write_pos(pos)
                else:
                    chunk = b""
        except OSError as exc:
            _log.warning("news scanner feed read failed: %s", exc)
            await asyncio.sleep(interval)
            continue

        if chunk:
            for line in chunk.splitlines():
                if not line.strip():
                    continue
                try:
                    yield json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    _log.warning("news scanner feed: bad json line skipped")

        await asyncio.sleep(interval)
