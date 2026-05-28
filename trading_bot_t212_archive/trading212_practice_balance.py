"""
One-off test: Trading 212 **practice (paper)** account cash balance.

Uses HTTP Basic auth (API key:user, secret:password) per Trading 212 Public API.
Loads the repository root ``.env`` via python-dotenv.

Env:
    TRADING_212_KEY — API key from the Trading 212 app (paper keys → demo URL).
    TRADING_212_SECRET — API secret (``TRADING_212_SECERT`` typo is also accepted).

Run from repo root or from this folder:

    python "Main_Website/trading212_practice_balance.py"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from curl_cffi import requests
from dotenv import load_dotenv

DEMO_BASE = "https://demo.trading212.com/api/v0"
CASH_PATH = "/equity/account/cash"


def _load_env() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")


def _credentials() -> tuple[str, str]:
    _load_env()
    key = (os.environ.get("TRADING_212_KEY") or "").strip()
    secret = (
        os.environ.get("TRADING_212_SECRET")
        or os.environ.get("TRADING_212_SECERT")
        or ""
    ).strip()
    if not key or not secret:
        print(
            "Missing TRADING_212_KEY or TRADING_212_SECRET in repository root .env "
            "(TRADING_212_SECERT typo is accepted for the secret).",
            file=sys.stderr,
        )
        sys.exit(1)
    return key, secret


def fetch_practice_cash() -> dict:
    key, secret = _credentials()
    url = f"{DEMO_BASE}{CASH_PATH}"
    # urllib gets Cloudflare 1010 here; Trading 212 is reachable via browser-like TLS (same as discord.py-self).
    r = requests.get(
        url,
        auth=(key, secret),
        impersonate="chrome",
        timeout=30,
    )
    if not r.ok:
        hint = ""
        if r.status_code == 401:
            hint = (
                "\nHint: `/equity/account/cash` on **demo** needs API keys issued from your "
                "**Practice / paper** Trading 212 app profile. Live-account keys authenticate "
                "against ``https://live.trading212.com`` only.\n"
            )
        print(f"HTTP {r.status_code}: {r.reason}\n{r.text}{hint}", file=sys.stderr)
        sys.exit(1)
    return r.json()


def main() -> None:
    try:
        data = fetch_practice_cash()
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
