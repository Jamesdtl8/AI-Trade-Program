#!/usr/bin/env python3
"""Smoke-test IBKR Client Portal WebSocket per their guide (session from /tickle).

  wss://localhost:<port>/v1/api/ws
  Cookie: api={"session":"<from /tickle>"}

Or after connect, if you see {"message":"waiting for session"}, send:
  {"session":"<value>"}

Run from repo root with venv:
  .venv/bin/python ai_sandbox/ibkr_ws_guide_smoke.py AAPL 120

Env: IB_GATEWAY_BASE_URL / GATEWAY · IBKR_WS_SMOKE_TICKER · IBKR_WS_SMOKE_LISTEN_SEC
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import websockets  # type: ignore
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

# Allow running without full package path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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


def gateway_rest_root() -> str:
    _load_dotenv()
    raw = (os.environ.get("GATEWAY") or os.environ.get("IB_GATEWAY_BASE_URL") or "https://localhost:7560").strip().rstrip("/")
    if not raw.endswith("/v1/api"):
        raw = f"{raw}/v1/api"
    return raw


def gateway_ws_url(rest_root: str) -> str:
    base = rest_root.replace("/v1/api", "").rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/v1/api/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/v1/api/ws"
    raise SystemExit(f"Bad gateway URL: {rest_root}")


def http_get(path: str, ctx: ssl.SSLContext, params: dict[str, str] | None = None) -> object:
    url = gateway_rest_root() + path
    if params:
        url = url + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ibkr-ws-smoke/1"})
    with urlopen(req, context=ctx, timeout=15) as r:
        return json.loads(r.read().decode())


def pick_stock_conid(search_rows: object, want_sym: str) -> tuple[int, str]:
    want_sym = want_sym.strip().upper().lstrip("$")
    if not isinstance(search_rows, list) or not search_rows:
        raise SystemExit(f"secdef search returned no rows for {want_sym!r}")
    picked: dict | None = None
    for row in search_rows:
        if not isinstance(row, dict):
            continue
        st = str(row.get("secType") or "").upper()
        if st and st != "STK":
            continue
        try:
            cid = int(row.get("conid"))
        except (TypeError, ValueError):
            continue
        if cid > 0:
            picked = row
            break
    if picked is None and isinstance(search_rows[0], dict) and search_rows[0].get("conid"):
        picked = search_rows[0]
    if not picked or picked.get("conid") is None:
        raise SystemExit(f"no STK conid in search for {want_sym}")
    cid = int(picked["conid"])
    sym = str(picked.get("symbol") or want_sym).upper()
    return cid, sym


async def main() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    rest = gateway_rest_root()
    wss = gateway_ws_url(rest)
    ticker = (
        (sys.argv[1] if len(sys.argv) > 1 else "").strip()
        or os.environ.get("IBKR_WS_SMOKE_TICKER", "").strip()
        or "AAPL"
    )
    listen_sec = 12.0
    if len(sys.argv) > 2:
        try:
            listen_sec = max(1.0, float(sys.argv[2]))
        except ValueError:
            pass
    else:
        try:
            listen_sec = max(1.0, float(os.environ.get("IBKR_WS_SMOKE_LISTEN_SEC", "12")))
        except ValueError:
            listen_sec = 12.0
    print("REST:", rest)
    print("WS:  ", wss)
    print("Ticker:", ticker)
    print(f"Listen: {listen_sec:.0f}s — streaming smd+ lines (Ctrl+C to stop)\n")

    auth = http_get("/iserver/auth/status", ctx)
    if not isinstance(auth, dict):
        raise SystemExit("unexpected auth response")
    print("auth:", json.dumps(auth, indent=2)[:400])

    tickle = http_get("/tickle", ctx)
    sess = tickle.get("session") if isinstance(tickle, dict) else None
    print("tickle session:", (str(sess)[:20] + "…") if sess and len(str(sess)) > 20 else sess)
    if not sess:
        print("No session in /tickle — log in at the gateway in a browser first.")
        return

    # Method 1 (guide): Cookie on the WebSocket handshake — same as Trading_AI.ibkr_smd_streamer
    cookie = f'api={json.dumps({"session": str(sess)})}'
    print("Connecting with Cookie api={session…} …")

    async with websockets.connect(
        wss,
        ssl=ctx if wss.startswith("wss") else None,
        additional_headers=[("Cookie", cookie)],
        ping_interval=20,
        max_size=10_000_000,
    ) as ws:
        print("WebSocket open OK.")

        first_raw: bytes | str | None = None
        try:
            first_raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
            fr = first_raw.decode("utf-8", "replace") if isinstance(first_raw, bytes) else str(first_raw)
            print("first recv:", fr[:500])
        except asyncio.TimeoutError:
            print("no frame in 4s (idle is OK)")

        # IB guide method 2: only if socket says it's waiting (Cookie path usually skips this).
        if first_raw is not None:
            fr = first_raw.decode("utf-8", "replace") if isinstance(first_raw, bytes) else str(first_raw)
            if "waiting for session" in fr.lower():
                await ws.send(json.dumps({"session": str(sess)}))
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    print("after session JSON:", raw2[:500] if isinstance(raw2, str) else str(raw2)[:500])
                except asyncio.TimeoutError:
                    print("no reply after session JSON (OK)")

        # Market data: same wire format as Trading_AI.ibkr_smd_streamer
        rows = http_get("/iserver/secdef/search", ctx, {"symbol": ticker.upper().lstrip("$"), "secType": "STK"})
        conid, ib_sym = pick_stock_conid(rows, ticker)
        print(f"secdef → {ib_sym} conid={conid}")

        fields = ["31", "82", "83", "84", "86"]  # last, chg%, vol, bid, ask
        payload_txt = json.dumps({"fields": [str(x) for x in fields]})
        sub_msg = f"smd+{conid}+{payload_txt}"
        await ws.send(sub_msg)
        print("sent:", sub_msg[:120] + ("…" if len(sub_msg) > 120 else ""))

        listen_until = time.monotonic() + listen_sec
        smd_hits = 0
        while time.monotonic() < listen_until:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, bytes):
                raw_s = raw.decode("utf-8", "replace")
            else:
                raw_s = str(raw)
            try:
                obj = json.loads(raw_s)
            except json.JSONDecodeError:
                print("recv (non-json):", raw_s[:200])
                continue
            topic = str(obj.get("topic") or "")
            if topic.startswith("smd+"):
                smd_hits += 1
                print(f"smd quote #{smd_hits}:", json.dumps(obj)[:600])
            elif topic in ("system", "sts", "act"):
                pass
            else:
                print("recv:", raw_s[:300])

        if smd_hits == 0:
            print(
                f"No smd+ frames in {listen_sec:.0f}s "
                "(paper / no entitlement / quiet market: connect + subscribe still OK).",
            )
        else:
            print(f"\nDone: {smd_hits} smd+ message(s) in {listen_sec:.0f}s.")


if __name__ == "__main__":
    asyncio.run(main())
