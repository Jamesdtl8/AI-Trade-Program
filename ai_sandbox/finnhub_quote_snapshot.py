#!/usr/bin/env python3
"""Fetch Finnhub US equity context: last, open, day range, change vs prior close, change since open.

WebSocket trades (`finnhub_trades_ws.py`) do not include OHLC; use REST ``/quote``:

  https://finnhub.io/docs/api/quote

- ``d`` / ``dp`` from Finnhub are **change vs previous close** (``pc``), not vs open.
- **Change since market open** (regular-session open in ``o``): ``c - o`` (computed here).

Usage:
  FINNHUB_API_KEY=... .venv/bin/python ai_sandbox/finnhub_quote_snapshot.py AAPL
  .venv/bin/python ai_sandbox/finnhub_quote_snapshot.py MSFT --poll 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

QUOTE_URL = "https://finnhub.io/api/v1/quote"


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


def fetch_quote(symbol: str, token: str) -> dict:
    q = urllib.parse.urlencode({"symbol": symbol.strip().upper(), "token": token})
    url = f"{QUOTE_URL}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "finnhub-quote-snapshot/1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _fmt(x: object, *, decimals: int = 2) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v != v:  # NaN
        return "—"
    return f"{v:.{decimals}f}"


def print_snapshot(sym: str, data: dict) -> None:
    c = data.get("c")  # current
    o = data.get("o")  # open (Finnhub: generally today's official open)
    h = data.get("h")
    l = data.get("l")
    pc = data.get("pc")  # previous close
    d = data.get("d")  # vs prev close
    dp = data.get("dp")
    ts = data.get("t")

    since_open = None
    since_open_pct = None
    try:
        if c is not None and o is not None and float(o) > 0:
            since_open = float(c) - float(o)
            since_open_pct = 100.0 * since_open / float(o)
    except (TypeError, ValueError):
        pass

    print(f"Symbol: {sym}")
    print(f"  Last (c):        {_fmt(c)}")
    print(f"  Open (o):        {_fmt(o)}")
    print(f"  Day high / low:  {_fmt(h)} / {_fmt(l)}")
    print(f"  Prev close (pc): {_fmt(pc)}")
    print(f"  Chg vs prev (d): {_fmt(d)}  ({_fmt(dp, decimals=3)}%)  [Finnhub d/dp]")
    if since_open is not None:
        print(
            f"  Since open:      {_fmt(since_open)}  ({_fmt(since_open_pct, decimals=3)}%)  [c − o]",
        )
    else:
        print("  Since open:      — (need valid c and o)")
    if ts is not None:
        print(f"  Quote time (t):  {ts}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Finnhub /quote snapshot + change since open")
    ap.add_argument("symbol", nargs="?", default="AAPL", help="Symbol (default AAPL)")
    ap.add_argument(
        "--poll",
        type=float,
        metavar="SEC",
        default=0,
        help="Re-fetch every SEC seconds (0 = once)",
    )
    args = ap.parse_args()

    _load_dotenv()
    token = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not token:
        print("Set FINNHUB_API_KEY or add to .env", file=sys.stderr)
        sys.exit(1)

    sym = args.symbol.strip().upper().lstrip("$")
    interval = float(args.poll or 0)

    while True:
        try:
            data = fetch_quote(sym, token)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
            sys.exit(1)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

        print_snapshot(sym, data)
        if interval <= 0:
            break
        print("---")
        time.sleep(interval)


if __name__ == "__main__":
    main()
