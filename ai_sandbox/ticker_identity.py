"""Map scanner symbols (OLOX) to broker instruments (SGBX_US_EQ) for trades + re-entry."""

from __future__ import annotations

from typing import Any


def normalize_scanner(ticker: str | None) -> str:
    return (ticker or "").strip().upper().lstrip("$")


def trade_identity_keys(ticker: str | None) -> frozenset[str]:
    """All ticker strings that refer to the same tradable symbol."""
    tk = normalize_scanner(ticker)
    if not tk:
        return frozenset()
    keys: set[str] = {tk}
    if not tk.endswith("_US_EQ"):
        keys.add(f"{tk}_US_EQ")
    head = tk.split("_", 1)[0]
    if head:
        keys.add(head)
        keys.add(f"{head}_US_EQ")

    try:
        from . import t212_ai

        code = t212_ai.resolve_ticker(tk) or (tk if "_" in tk else None)
        if not code and head:
            code = t212_ai.resolve_ticker(head)
        if code:
            code_u = code.strip().upper()
            keys.add(code_u)
            keys.add(code_u.split("_", 1)[0])
            disp = t212_ai.display_raw_for(code_u)
            if disp:
                keys.add(normalize_scanner(disp))
            for sym, inst in t212_ai._AI_MAP.items():
                if inst == code_u:
                    keys.add(normalize_scanner(sym))
    except Exception:
        pass

    return frozenset(k for k in keys if k)


def trades_ticker_where_clause(
    ticker: str | None,
    *,
    column: str = "ticker",
) -> tuple[str, tuple[Any, ...]]:
    """SQL boolean expression matching trades for a scanner or instrument ticker."""
    keys = sorted(trade_identity_keys(ticker))
    if not keys:
        return "1=0", ()
    ph = ",".join("?" * len(keys))
    clause = f"""(
        UPPER({column}) IN ({ph})
        OR UPPER(REPLACE({column}, '_US_EQ', '')) IN ({ph})
        OR alert_id IN (SELECT id FROM alerts WHERE UPPER(ticker) IN ({ph}))
    )"""
    params: tuple[Any, ...] = tuple(keys) * 3
    return clause, params


def grader_ticker_for_trade_row(row: dict[str, Any] | Any) -> str:
    """Scanner-facing symbol for grader state (prefer alert ticker, then display map)."""
    from . import db

    aid = row.get("alert_id") if isinstance(row, dict) else row["alert_id"]
    if aid:
        alert = db.fetchone("SELECT ticker FROM alerts WHERE id=?", (int(aid),))
        if alert and alert["ticker"]:
            return normalize_scanner(str(alert["ticker"]))

    raw = str(row.get("ticker") if isinstance(row, dict) else row["ticker"] or "").strip()
    if not raw:
        return ""
    try:
        from . import t212_ai

        disp = t212_ai.display_raw_for(raw)
        if disp:
            return normalize_scanner(disp)
    except Exception:
        pass
    return normalize_scanner(raw.split("_", 1)[0])
