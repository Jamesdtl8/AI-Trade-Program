#!/usr/bin/env python3
"""Stream Finnhub WebSocket trades / last-price updates (US stocks).

Docs: https://finnhub.io/docs/api/websocket-trades — one connection per API key.

Usage (repo root, venv):
  FINNHUB_API_KEY=your_key .venv/bin/python ai_sandbox/finnhub_trades_ws.py
  FINNHUB_API_KEY=your_key .venv/bin/python ai_sandbox/finnhub_trades_ws.py MSFT

Loads ``FINNHUB_API_KEY`` from the environment or from repo ``.env`` if unset.
Do not commit real keys; rotate if a key was ever pasted in chat or VCS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    import websockets  # type: ignore
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    p = _ROOT / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


FINNHUB_WS_BASE = "wss://ws.finnhub.io"


async def _run(symbol: str) -> None:
    _load_dotenv()
    token = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not token:
        print(
            "Missing FINNHUB_API_KEY. Export it or add FINNHUB_API_KEY=... to .env",
            file=sys.stderr,
        )
        raise SystemExit(1)

    uri = f"{FINNHUB_WS_BASE}?token={token}"
    sym = symbol.strip().upper().lstrip("$")
    print(f"Connecting {FINNHUB_WS_BASE} … symbol={sym}", flush=True)

    async with websockets.connect(uri, ping_interval=20, max_size=10_000_000) as ws:
        await ws.send(json.dumps({"type": "subscribe", "symbol": sym}))
        print("Subscribed. Printing messages (Ctrl+C to stop).\n", flush=True)
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                print(raw, flush=True)
                continue
            if obj.get("type") == "trade" and isinstance(obj.get("data"), list):
                for row in obj["data"]:
                    if not isinstance(row, dict):
                        continue
                    print(
                        f"{row.get('s')}  p={row.get('p')}  v={row.get('v')}  "
                        f"t={row.get('t')}  c={row.get('c')}",
                        flush=True,
                    )
            else:
                print(json.dumps(obj)[:500], flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Finnhub WebSocket trades stream")
    ap.add_argument(
        "symbol",
        nargs="?",
        default="AAPL",
        help="Finnhub symbol (default: AAPL)",
    )
    args = ap.parse_args()
    try:
        asyncio.run(_run(args.symbol))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
