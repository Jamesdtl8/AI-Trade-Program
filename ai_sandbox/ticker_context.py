"""Build a per-ticker enrichment context from recent DB history.

Used by the hard filter and the scorer to make smarter decisions than
"first alert only". Computed cheap from the alerts table on every message.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from . import db


def build(ticker: str, current_alert: dict[str, Any]) -> dict[str, Any]:
    """Return a context dict for ``ticker`` based on its alert history.

    Fields:
      alert_number                           - SCANNER pings for this ticker since UTC midnight
      alert1_tags, alert1_price               - tags & price from first SCANNER row of that day (from DB)
      prev_pct, prev_rv, prev_alert_age_s   - last SCANNER alert before this one
      rv_growth                              - current_rv / prev_rv (None if no prev)
      pct_jump                               - percent price move from immediate prior SCANNER
                                                 quoted price to this alert price:
                                                 ((cur_price-prev_price)/prev_price)*100;
                                                 falls back to scanner-display intraday pct
                                                 delta (cur-prev) only when a price is
                                                 missing on either side (None if no prev)
      scanner_alerts_5m                      - count of SCANNER alerts for this ticker in last 5 min
      halt_count_60m                         - count of HALT alerts in last 60 min
      last_halt_age_s                        - seconds since last HALT (None if none today)
      recently_halted                        - True if last_halt_age_s <= 1800 (30 min)
      news_in_history                        - True if any prior alert had a news headline
      fast_mover                             - True when prev_alert within 10 min AND rv_growth >= 5x
                                               AND pct_jump >= 15 (uses same pct_jump semantics)
      whale_count_60m                        - WHALE messages in last 60 min
    """
    if not ticker:
        return {}
    now = time.time()
    day_start_ts = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    scanner_today = db.fetchall(
        """SELECT parsed_json FROM alerts WHERE ticker=? AND type='SCANNER'
            AND ts >= ? ORDER BY ts ASC, id ASC""",
        (ticker.upper(), day_start_ts),
    )
    alert_number = len(scanner_today)
    alert1_tags: list[str] = []
    alert1_price: float | None = None
    if scanner_today:
        try:
            first_p = json.loads(scanner_today[0]["parsed_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            first_p = {}
        raw_tags = first_p.get("tags")
        if isinstance(raw_tags, list):
            alert1_tags = [str(t) for t in raw_tags]
        p0 = first_p.get("price")
        if p0 is not None:
            try:
                alert1_price = float(p0)
            except (TypeError, ValueError):
                pass

    rows = db.fetchall(
        "SELECT id, ts, type, parsed_json FROM alerts WHERE ticker=? AND ts <= ? ORDER BY ts DESC LIMIT 80",
        (ticker.upper(), now),
    )

    prev_pct = None
    prev_rv = None
    prev_price = None
    prev_alert_age_s = None
    scanner_alerts_5m = 0
    halt_count_60m = 0
    last_halt_age_s: float | None = None
    news_in_history = bool(current_alert.get("news_headline"))
    whale_count_60m = 0
    current_id = current_alert.get("_alert_id")  # set by engine when known

    dmid = (current_alert.get("discord_message_id") or "").strip() or None

    for row in rows:
        ts = float(row["ts"])
        age = now - ts
        # Skip the current alert (we may or may not have written it yet)
        if current_id is not None and row["id"] == current_id:
            continue
        try:
            parsed = json.loads(row["parsed_json"]) if row["parsed_json"] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        atype = row["type"]
        # Revisions of the same Discord message share one snowflake — prior rows
        # for the same message must not define "previous SCANNER" for pct_jump.
        if dmid and str(parsed.get("discord_message_id") or "") == dmid:
            if atype in ("SCANNER", "SCANNER_EDIT"):
                continue
        if atype == "SCANNER":
            if age <= 300:
                scanner_alerts_5m += 1
            if prev_pct is None:
                prev_pct = parsed.get("pct")
                prev_rv = parsed.get("rv")
                prev_alert_age_s = age
                pp = parsed.get("price")
                if pp is not None:
                    try:
                        prev_price = float(pp)
                    except (TypeError, ValueError):
                        prev_price = None
        elif atype == "HALT":
            if age <= 3600:
                halt_count_60m += 1
            if last_halt_age_s is None:
                last_halt_age_s = age
        elif atype == "WHALE":
            if age <= 3600:
                whale_count_60m += 1
        if parsed.get("news_headline"):
            news_in_history = True

    cur_rv = current_alert.get("rv")
    cur_pct = current_alert.get("pct")
    cur_price = current_alert.get("price")
    rv_growth = (cur_rv / prev_rv) if (cur_rv and prev_rv and prev_rv > 0) else None

    pct_jump: float | None = None
    if prev_price is not None and prev_price > 0 and cur_price is not None:
        try:
            pct_jump = round(
                (float(cur_price) - prev_price) / prev_price * 100.0,
                4,
            )
        except (TypeError, ValueError):
            pct_jump = None
    if pct_jump is None and cur_pct is not None and prev_pct is not None:
        pct_jump = round(cur_pct - prev_pct, 4)

    fast_mover = bool(
        prev_alert_age_s is not None
        and prev_alert_age_s <= 600
        and rv_growth is not None
        and rv_growth >= 5.0
        and pct_jump is not None
        and pct_jump >= 15.0
    )

    recently_halted = bool(last_halt_age_s is not None and last_halt_age_s <= 1800)

    return {
        "alert_number": alert_number,
        "alert1_tags": alert1_tags,
        "alert1_price": alert1_price,
        "prev_pct": prev_pct,
        "prev_rv": prev_rv,
        "prev_price": prev_price,
        "prev_alert_age_s": prev_alert_age_s,
        "rv_growth": round(rv_growth, 2) if rv_growth else None,
        "pct_jump": pct_jump,
        "scanner_alerts_5m": scanner_alerts_5m,
        "halt_count_60m": halt_count_60m,
        "last_halt_age_s": last_halt_age_s,
        "recently_halted": recently_halted,
        "news_in_history": news_in_history,
        "fast_mover": fast_mover,
        "whale_count_60m": whale_count_60m,
    }
