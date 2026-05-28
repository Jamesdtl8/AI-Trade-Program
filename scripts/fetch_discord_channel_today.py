#!/usr/bin/env python3
"""One-shot: dump today's UTC messages from a named guild text channel."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

if load_dotenv:
    load_dotenv(_REPO / ".env")

import discord


def _norm(name: str) -> str:
    return name.lower().replace(" ", "-").strip()


def _channel_slug(channel_name: str) -> str:
    """Strip common 'emoji│prefix' so e.g. '🤖│all-in-one-scanner' matches."""
    tail = channel_name.replace("│", "|").split("|")[-1]
    return _norm(tail)


def _message_to_dict(m: discord.Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "timestamp": m.created_at.isoformat(),
        "author": str(m.author),
        "author_id": str(m.author.id),
        "content": m.content or "",
        "channel_id": str(m.channel.id),
        "guild_id": str(m.guild.id) if m.guild else None,
        "embeds": [e.to_dict() for e in m.embeds],
        "attachments": [{"id": str(a.id), "url": a.url, "filename": a.filename} for a in m.attachments],
        "reference_message_id": str(m.reference.message_id) if m.reference and m.reference.message_id else None,
    }


def _find_text_channel(client: discord.Client, want: str) -> discord.TextChannel | discord.Thread | None:
    n = _norm(want)
    for guild in client.guilds:
        for ch in guild.text_channels:
            if _channel_slug(ch.name) == n:
                return ch
        for th in guild.threads:
            if _channel_slug(th.name) == n:
                return th
    # Fallback: .env channel IDs (name match)
    raw = (os.environ.get("DISCORD_CHANNEL_ID") or "").replace(" ", "")
    for part in raw.split(","):
        if not part.isdigit():
            continue
        ch = client.get_channel(int(part))
        if ch is not None and hasattr(ch, "name") and _channel_slug(str(ch.name)) == n:
            return ch  # type: ignore[return-value]
    return None


async def _run(channel_query: str) -> dict[str, Any]:
    token = (os.environ.get("DISCORD_USER_TOKEN") or "").strip()
    if not token:
        return {"error": "DISCORD_USER_TOKEN missing"}

    now = datetime.now(timezone.utc)
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    found: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}

    class FetchClient(discord.Client):
        async def on_ready(self) -> None:
            nonlocal meta
            ch = _find_text_channel(self, channel_query)
            if ch is None:
                meta = {"error": f"No channel matching {channel_query!r}", "user": str(self.user)}
                await self.close()
                return
            meta = {
                "channel_id": str(ch.id),
                "channel_name": ch.name,
                "guild": ch.guild.name if ch.guild else None,
                "range_utc": {"start": day_start.isoformat(), "end_exclusive": day_end.isoformat()},
            }
            async for m in ch.history(limit=None, after=day_start, before=day_end, oldest_first=True):
                found.append(_message_to_dict(m))
            await self.close()

    client = FetchClient(chunk_guilds_at_startup=False, guild_subscriptions=True)
    async with client:
        await client.start(token)

    if "error" in meta and "channel_id" not in meta:
        return meta
    out = dict(meta)
    out["message_count"] = len(found)
    out["messages"] = found
    return out


def main() -> None:
    q = (sys.argv[1] if len(sys.argv) > 1 else "all-in-one-scanner").strip()
    data = asyncio.run(_run(q))
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
