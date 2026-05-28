# ── AI sandbox endpoints ─────────────────────────────────────────────────────


def _ai_engine():
    try:
        from ai_sandbox import service as _ai_service
    except Exception as exc:
        _log.warning("ai_sandbox import failed: %s", exc)
        return None
    return _ai_service.get_engine()


def _ai_db():
    from ai_sandbox import db as _ai_db_mod

    return _ai_db_mod


def _ai_config():
    from ai_sandbox import config as _ai_cfg

    return _ai_cfg


def _ai_t212():
    from ai_sandbox import t212_ai

    return t212_ai


@app.get("/api/ai/status")
def api_ai_status():
    eng = _ai_engine()
    if eng is None:
        return jsonify(ok=False, error="ai_engine_not_running"), 503
    cfg = _ai_config()
    db = _ai_db()
    today_pnl_row = db.fetchone(
        "SELECT COALESCE(SUM(pnl_gbp),0) AS s FROM trades WHERE status='CLOSED' AND exit_ts >= ?",
        (time.time() - 86400,),
    )
    today_pnl_realized = float(today_pnl_row["s"]) if today_pnl_row else 0.0
    px_map = _ai_live_price_usd_by_ticker()
    today_pnl_open_unreal = _ai_open_trades_unrealized_gbp(px_map)
    today_pnl = round(today_pnl_realized + today_pnl_open_unreal, 2)
    open_trades = db.fetchone("SELECT COUNT(*) AS c FROM trades WHERE status='OPEN'")
    broker_rows = _ai_broker_position_rows()
    broker_open = len(broker_rows)
    open_trades_n = broker_open if broker_open else (int(open_trades["c"]) if open_trades else 0)
    closed_today = db.fetchone(
        "SELECT COUNT(*) AS c FROM trades WHERE status='CLOSED' AND exit_ts >= ?",
        (time.time() - 86400,),
    )
    rejected_today = db.fetchone(
        "SELECT COUNT(*) AS c FROM trades WHERE status IN ('REJECTED','BLOCKED_T212') AND open_ts >= ?",
        (time.time() - 86400,),
    )
    cash = _ai_cash_cached()
    gemini_usage_payload: dict = {}
    try:
        import datetime as _dt

        day0 = (
            _dt.datetime.now(_dt.timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        all_s = db.gemini_usage_stats_since(0.0)
        day_s = db.gemini_usage_stats_since(day0)
        gemini_usage_payload = {
            "usage_gbp_total": round(float(all_s["sum_gbp"]), 2),
            "usage_gbp_today": round(float(day_s["sum_gbp"]), 2),
            "usage_calls_total": int(all_s["calls"]),
            "usage_calls_today": int(day_s["calls"]),
        }
    except Exception as _exc:
        _log.debug("gemini usage on ai status: %s", _exc)
    return jsonify(
        ok=True,
        engine=eng.status(),
        slot_capital_gbp=cfg.SLOT_CAPITAL_GBP,
        gbp_usd_rate=cfg.GBP_USD_RATE,
        slot_count=cfg.SLOT_COUNT,
        today_pnl_gbp=today_pnl,
        today_pnl_realized_gbp=round(today_pnl_realized, 2),
        today_pnl_open_unreal_gbp=today_pnl_open_unreal,
        open_trades=open_trades_n,
        broker_open_positions=broker_open,
        closed_today=int(closed_today["c"]) if closed_today else 0,
        rejected_today=int(rejected_today["c"]) if rejected_today else 0,
        cash=cash,
        gemini_usage=gemini_usage_payload,
    )


_AI_CASH_CACHE: dict[str, Any] = {"ts": 0.0, "cash": None}
_AI_CASH_TTL = 1.0

_AI_BROKER_PX_LAST_GOOD: dict[str, float] = {}
_AI_BROKER_PX_LAST_LOCK = threading.Lock()


def _ai_live_price_usd_by_ticker() -> dict[str, float]:
    """T212 position marks (USD) keyed by instrument code.

    Reads the AI sandbox snapshot only — HTTP is performed once per second by
    ``ai_sandbox.t212_ai.run_positions_poller`` (not here). Separate from the main
    bot's ``Trading_AI.t212`` account.
    """
    import asyncio as _asyncio

    from ai_sandbox import service as _ai_svc, t212_ai as _t212_ai

    eng = _ai_engine()
    cfg = _ai_config()
    if eng is None or not cfg.t212_credentials_ok():
        with _AI_BROKER_PX_LAST_LOCK:
            return dict(_AI_BROKER_PX_LAST_GOOD)

    loop = _ai_svc._loop
    if loop is None:
        with _AI_BROKER_PX_LAST_LOCK:
            return dict(_AI_BROKER_PX_LAST_GOOD)

    try:
        fut = _asyncio.run_coroutine_threadsafe(
            _t212_ai.get_positions(bypass_cache=False),
            loop,
        )
        rows = fut.result(timeout=8)
    except Exception as exc:
        _log.debug("ai positions map failed: %s", exc)
        with _AI_BROKER_PX_LAST_LOCK:
            return dict(_AI_BROKER_PX_LAST_GOOD)

    out: dict[str, float] = {}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        tk = str(p.get("ticker") or "").strip().upper()
        if not tk:
            continue
        cp = p.get("currentPrice")
        if cp is None:
            continue
        try:
            px = float(cp)
        except (TypeError, ValueError):
            continue
        if px > 0 and px == px:
            out[tk] = px

    with _AI_BROKER_PX_LAST_LOCK:
        if out:
            _AI_BROKER_PX_LAST_GOOD.clear()
            _AI_BROKER_PX_LAST_GOOD.update(out)
        else:
            _AI_BROKER_PX_LAST_GOOD.clear()
        return dict(out)


def _ai_broker_position_rows() -> list[dict[str, Any]]:
    """Normalized T212 position rows from the shared poller snapshot."""
    import asyncio as _asyncio

    from ai_sandbox import service as _ai_svc, t212_ai as _t212_ai

    eng = _ai_engine()
    cfg = _ai_config()
    if eng is None or not cfg.t212_credentials_ok():
        return []
    loop = _ai_svc._loop
    if loop is None:
        return []
    try:
        fut = _asyncio.run_coroutine_threadsafe(
            _t212_ai.get_positions(bypass_cache=False),
            loop,
        )
        rows = fut.result(timeout=8)
    except Exception as exc:
        _log.debug("ai broker positions failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        tk = str(p.get("ticker") or "").strip().upper()
        try:
            qty = float(p.get("quantity") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if not tk or qty <= 1e-6:
            continue
        wm = _t212_ai.wallet_metrics_from_row(p)
        try:
            avg = float(p.get("averagePrice") or 0.0)
        except (TypeError, ValueError):
            avg = 0.0
        try:
            mark = float(p.get("currentPrice") or 0.0)
        except (TypeError, ValueError):
            mark = 0.0
        out.append(
            {
                "ticker": tk,
                "quantity": qty,
                "average_price_usd": avg if avg > 0 else None,
                "current_price_usd": mark if mark > 0 else None,
                "wallet_total_cost_gbp": wm.get("total_cost_gbp"),
                "wallet_current_value_gbp": wm.get("current_value_gbp"),
                "unreal_gbp": wm.get("unreal_gbp"),
                "unreal_pct": wm.get("unreal_pct"),
                "wallet_fx_impact_gbp": wm.get("fx_impact_gbp"),
            }
        )
    out.sort(key=lambda x: float(x.get("wallet_current_value_gbp") or 0.0), reverse=True)
    return out


def _ai_merge_broker_into_slots(snap: dict[str, Any], broker_rows: list[dict[str, Any]]) -> None:
    """Overlay broker positions onto slot cards — broker is the display source of truth."""
    if not broker_rows:
        return
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp
    except Exception:
        _t212_ai_disp = None

    slots = snap.get("slots") or []
    by_ticker = {str(r["ticker"]).upper(): r for r in broker_rows}
    broker_tickers = set(by_ticker.keys())
    assigned: set[str] = set()

    for s in slots:
        st = str(s.get("state") or "").upper()
        tk = str(s.get("ticker") or "").strip().upper()
        if st == "ACTIVE" and tk and tk in by_ticker:
            br = by_ticker[tk]
            assigned.add(tk)
            _ai_apply_broker_row_to_slot(s, br, _t212_ai_disp)
            s["broker_managed"] = True
        elif s.get("broker_unassigned") and tk and tk not in broker_tickers:
            s.pop("broker_unassigned", None)
            s.pop("broker_managed", None)
            if str(s.get("last_decision") or "").startswith("broker:"):
                s["last_decision"] = None
            if not s.get("trade_id"):
                s["ticker"] = None
                s["display_ticker"] = None
                s["state"] = "OPEN"
                for k in (
                    "wallet_total_cost_gbp",
                    "wallet_current_value_gbp",
                    "unreal_gbp",
                    "unreal_pct",
                    "wallet_fx_impact_gbp",
                    "capital_gbp",
                    "entry",
                    "last_price",
                ):
                    s.pop(k, None)

    free_indices = [
        i
        for i, s in enumerate(slots)
        if str(s.get("state") or "").upper() != "ACTIVE" or not s.get("trade_id")
    ]
    orphan_rows = [r for r in broker_rows if r["ticker"] not in assigned]
    for idx, br in zip(free_indices, orphan_rows):
        s = slots[idx]
        s["broker_managed"] = True
        s["broker_unassigned"] = True
        s["display_ticker"] = (
            _t212_ai_disp.display_raw_for(str(br["ticker"])) if _t212_ai_disp else br["ticker"]
        )
        s["ticker"] = br["ticker"]
        if s.get("state") == "OPEN":
            s["state"] = "ACTIVE"
        _ai_apply_broker_row_to_slot(s, br, _t212_ai_disp)
        if s.get("entry") is None and br.get("average_price_usd"):
            s["entry"] = br["average_price_usd"]
        if s.get("last_price") is None and br.get("current_price_usd"):
            s["last_price"] = br["current_price_usd"]
        if not s.get("last_decision"):
            s["last_decision"] = "broker:awaiting_reconcile"


def _ai_apply_broker_row_to_slot(s: dict, br: dict, disp_mod) -> None:
    if br.get("wallet_total_cost_gbp") is not None:
        s["wallet_total_cost_gbp"] = round(float(br["wallet_total_cost_gbp"]), 2)
        s["capital_gbp"] = round(float(br["wallet_total_cost_gbp"]), 2)
    if br.get("wallet_current_value_gbp") is not None:
        s["wallet_current_value_gbp"] = round(float(br["wallet_current_value_gbp"]), 2)
    if br.get("unreal_gbp") is not None:
        s["unreal_gbp"] = round(float(br["unreal_gbp"]), 2)
    if br.get("unreal_pct") is not None:
        s["unreal_pct"] = round(float(br["unreal_pct"]), 3)
    if br.get("wallet_fx_impact_gbp") is not None:
        s["wallet_fx_impact_gbp"] = round(float(br["wallet_fx_impact_gbp"]), 2)
    if br.get("current_price_usd"):
        s["last_price"] = float(br["current_price_usd"])
    tk = str(br.get("ticker") or "")
    if disp_mod and tk:
        s["display_ticker"] = disp_mod.display_raw_for(tk)


def _ai_trade_broker_confirmed(row: dict) -> bool:
    cfg = _ai_config()
    return cfg.trade_close_broker_confirmed(
        status=str(row.get("status") or ""),
        exit_reason=row.get("exit_reason"),
        t212_close_order_id=row.get("t212_close_order_id"),
        pnl_gbp=row.get("pnl_gbp"),
    )


def _ai_positions_wallet_map() -> dict[str, dict[str, float | None]]:
    """T212 walletImpact metrics keyed by instrument code (GBP from broker)."""
    import asyncio as _asyncio

    from ai_sandbox import service as _ai_svc, t212_ai as _t212_ai

    eng = _ai_engine()
    cfg = _ai_config()
    if eng is None or not cfg.t212_credentials_ok():
        return {}

    loop = _ai_svc._loop
    if loop is None:
        return {}

    try:
        fut = _asyncio.run_coroutine_threadsafe(
            _t212_ai.get_positions(bypass_cache=False),
            loop,
        )
        rows = fut.result(timeout=8)
    except Exception as exc:
        _log.debug("ai positions wallet map failed: %s", exc)
        return {}

    out: dict[str, dict[str, float | None]] = {}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        tk = str(p.get("ticker") or "").strip().upper()
        if not tk:
            continue
        out[tk] = _t212_ai.wallet_metrics_from_row(p)
    return out


def _ai_open_trades_unrealized_gbp(px_map: dict[str, float]) -> float:
    """Sum unrealized GBP P&L from T212 positions (broker source of truth)."""
    wallet_map = _ai_positions_wallet_map()
    if wallet_map:
        total = 0.0
        for wm in wallet_map.values():
            if wm and wm.get("unreal_gbp") is not None:
                total += float(wm["unreal_gbp"])
        return round(total, 2)

    if not px_map:
        return 0.0
    db = _ai_db()
    cfg = _ai_config()
    rows = db.fetchall(
        "SELECT ticker, entry_price, quantity FROM trades WHERE status='OPEN'",
    )
    total = 0.0
    for r in rows:
        tk = str(r["ticker"] or "").strip().upper()
        px = px_map.get(tk)
        if px is None or px <= 0:
            continue
        try:
            entry = float(r["entry_price"] or 0)
            qty = float(r["quantity"] or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or qty <= 0:
            continue
        total += float(cfg.usd_notionals_to_gbp(qty * (px - entry)))
    return round(total, 2)


def _ai_cash_cached() -> dict | None:
    """Return AI account cash from the engine's account-summary poller (no HTTP on request)."""
    from ai_sandbox import t212_ai

    snap = t212_ai.cash_snapshot()
    if snap and not snap.get("error"):
        _AI_CASH_CACHE["cash"] = snap
        _AI_CASH_CACHE["ts"] = time.time()
        return snap
    return _AI_CASH_CACHE.get("cash")


@app.get("/api/ai/slots")
def api_ai_slots():
    eng = _ai_engine()
    if eng is None:
        return jsonify(ok=False, error="ai_engine_not_running"), 503
    snap = eng.slots_snapshot()
    px_map = _ai_live_price_usd_by_ticker()
    wallet_map = _ai_positions_wallet_map()
    broker_rows = _ai_broker_position_rows()
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        for s in snap.get("slots", []):
            tk = s.get("ticker")
            if tk:
                s["display_ticker"] = _t212_ai_disp.display_raw_for(str(tk))
            tku = str(tk or "").strip().upper()
            wm = wallet_map.get(tku) if tku else None
            if wm:
                if wm.get("total_cost_gbp") is not None:
                    s["wallet_total_cost_gbp"] = round(float(wm["total_cost_gbp"]), 2)
                    s["capital_gbp"] = round(float(wm["total_cost_gbp"]), 2)
                if wm.get("current_value_gbp") is not None:
                    s["wallet_current_value_gbp"] = round(float(wm["current_value_gbp"]), 2)
                if wm.get("unreal_gbp") is not None:
                    s["unreal_gbp"] = round(float(wm["unreal_gbp"]), 2)
                if wm.get("unreal_pct") is not None:
                    s["unreal_pct"] = round(float(wm["unreal_pct"]), 3)
                if wm.get("fx_impact_gbp") is not None:
                    s["wallet_fx_impact_gbp"] = round(float(wm["fx_impact_gbp"]), 2)
            if tk and s.get("entry") is not None and str(s.get("state") or "").upper() == "ACTIVE":
                live = px_map.get(tku)
                if live is not None and live > 0:
                    s["last_price"] = float(live)
                if s.get("unreal_pct") is None:
                    try:
                        ent = float(s["entry"])
                        if ent > 0 and live is not None and live > 0:
                            s["unreal_pct"] = round((float(live) - ent) / ent * 100.0, 3)
                    except (TypeError, ValueError):
                        pass
            if (
                str(s.get("state") or "").upper() == "ACTIVE"
                and s.get("unreal_gbp") is None
                and s.get("unreal_pct") is not None
            ):
                try:
                    cg = float(s.get("capital_gbp") or 0)
                    if cg > 0:
                        s["unreal_gbp"] = round(cg * float(s["unreal_pct"]) / 100.0, 2)
                except (TypeError, ValueError):
                    pass
        _ai_merge_broker_into_slots(snap, broker_rows)
    except Exception as exc:
        _log.debug("ai slot enrich failed: %s", exc)
    snap["broker_positions"] = broker_rows
    return jsonify(ok=True, **snap)


@app.get("/api/ai/feed")
def api_ai_feed():
    """Scanner feed read from SQLite so it survives restarts.

    Joins alerts with their latest score (if any) so the row shows the final
    decision tag without a second round trip.

    One row per ticker (newest alert only): repeated WHALE pings for the same
    symbol are collapsed. The DB scan uses a larger cap so we still return up to
    ``limit`` distinct tickers when one name dominates recent traffic.
    """
    db = _ai_db()
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except ValueError:
        limit = 50
    fetch_cap = min(3000, max(300, limit * 40))
    rows = db.fetchall(
        """
        SELECT a.id AS alert_id, a.ts, a.ticker, a.type, a.raw, a.news_class, a.parsed_json,
               s.score, s.decision, s.reason
        FROM alerts a
        LEFT JOIN (
            SELECT alert_id, score, decision, reason,
                   ROW_NUMBER() OVER (PARTITION BY alert_id ORDER BY ts DESC) AS rn
            FROM scores
        ) s ON s.alert_id = a.id AND s.rn = 1
        ORDER BY a.ts DESC
        LIMIT ?
        """,
        (fetch_cap,),
    )
    import json as _json

    latest_rows = db.fetchall(
        """
        SELECT ticker, score, decision FROM (
            SELECT ticker, score, decision,
                   ROW_NUMBER() OVER (PARTITION BY UPPER(ticker) ORDER BY ts DESC, id DESC) AS rn
            FROM scores
            WHERE ticker IS NOT NULL AND TRIM(ticker) != ''
        ) x WHERE rn = 1
        """
    )
    ticker_latest = {}
    for lr in latest_rows:
        tk = (lr["ticker"] or "").strip().upper()
        if tk:
            ticker_latest[tk] = (lr["score"], lr["decision"])

    items = []
    for r in rows:
        d = dict(r)
        parsed = {}
        if d.get("parsed_json"):
            try:
                parsed = _json.loads(d["parsed_json"])
            except Exception:
                parsed = {}
        alert_score = d.get("score")
        alert_decision = d.get("decision")
        tk = (d.get("ticker") or "").strip().upper()
        eff_score, eff_decision = alert_score, alert_decision
        if tk and tk in ticker_latest:
            eff_score, eff_decision = ticker_latest[tk]
        items.append(
            {
                "alert_id": d["alert_id"],
                "ts": d["ts"],
                "ticker": d["ticker"],
                "type": d["type"],
                "raw": d["raw"],
                "news_class": d["news_class"],
                "score": eff_score,
                "decision": eff_decision,
                "alert_score": alert_score,
                "alert_decision": alert_decision,
                "filter_reason": None if d["score"] is not None else _filter_hint(parsed),
                "parsed": {
                    "price": parsed.get("price"),
                    "pct": parsed.get("pct"),
                    "rv": parsed.get("rv"),
                    "float": parsed.get("float"),
                    "market_cap": parsed.get("market_cap"),
                    "rank": parsed.get("rank"),
                    "tags": parsed.get("tags") or [],
                    "news_headline": parsed.get("news_headline"),
                },
            }
        )

    # Newest-first: first row wins per ticker; scan until we have ``limit`` rows.
    deduped: list = []
    seen: set[str] = set()
    for item in items:
        tk = (item.get("ticker") or "").strip().upper()
        key = tk if tk else f"\x00{int(item.get('alert_id') or 0)}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break

    return jsonify(ok=True, items=deduped)


def _filter_hint(parsed: dict) -> str | None:
    """Best-effort label for why an alert was filtered before Claude.

    Mirrors ai_sandbox.alert_filter rules in priority order; this is purely a
    display hint — the engine is the source of truth.
    """
    t = parsed.get("type")
    if t == "HALT":
        return "halt"
    if t == "OFFERING":
        return "offering"
    if t in (None, "UNKNOWN", "NEWS"):
        return f"type:{t}" if t else None
    if t == "WHALE" or t == "FIRE":
        return None
    rv = parsed.get("rv")
    if rv is None:
        return "no_rv"
    if rv < 10:
        return f"rv<10"
    flt = parsed.get("float")
    if flt is not None and flt > 30_000_000:
        return "float_too_large"
    pct = parsed.get("pct")
    # Baseline scanner cap matches ai_sandbox alert_filter default (30%); elevated tier (60%) needs context — not available here.
    if pct is not None and pct > 30:
        return "first_pct_too_late"
    return None


@app.get("/api/ai/trades")
def api_ai_trades():
    db = _ai_db()
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        limit = 100
    rows = db.fetchall(
        "SELECT * FROM trades ORDER BY open_ts DESC LIMIT ?",
        (limit,),
    )
    items = [dict(r) for r in rows]
    px_map = _ai_live_price_usd_by_ticker()
    wallet_map = _ai_positions_wallet_map()
    cfg = _ai_config()
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        for it in items:
            tk = (it.get("ticker") or "").strip()
            tku = tk.upper() if tk else ""
            if tk:
                it["display_ticker"] = _t212_ai_disp.display_raw_for(tk)
            wm = wallet_map.get(tku) if tku else None
            if it.get("status") == "OPEN" and wm:
                if wm.get("total_cost_gbp") is not None:
                    it["wallet_total_cost_gbp"] = round(float(wm["total_cost_gbp"]), 2)
                if wm.get("current_value_gbp") is not None:
                    it["wallet_current_value_gbp"] = round(float(wm["current_value_gbp"]), 2)
                if wm.get("unreal_gbp") is not None:
                    it["live_pnl_gbp"] = round(float(wm["unreal_gbp"]), 2)
                if wm.get("unreal_pct") is not None:
                    it["live_unreal_pct"] = round(float(wm["unreal_pct"]), 4)
            if it.get("status") == "OPEN" and it.get("live_unreal_pct") is None:
                px = px_map.get(tku) if tku else None
                if px is not None and px > 0:
                    it["live_price_usd"] = px
                    try:
                        entry = float(it.get("entry_price") or 0)
                        qty = float(it.get("quantity") or 0)
                        if entry > 0:
                            it["live_unreal_pct"] = round((px - entry) / entry * 100.0, 4)
                        if qty > 0 and it.get("live_pnl_gbp") is None:
                            it["live_pnl_gbp"] = round(cfg.usd_notionals_to_gbp(qty * (px - entry)), 2)
                    except (TypeError, ValueError):
                        pass
    except Exception as exc:
        _log.debug("ai trades enrich failed: %s", exc)

    for it in items:
        it["broker_confirmed"] = _ai_trade_broker_confirmed(it)
    items = [
        it
        for it in items
        if str(it.get("status") or "").upper() != "CLOSED" or it.get("broker_confirmed")
    ]

    return jsonify(ok=True, items=items)


@app.get("/api/ai/monitor/<int:trade_id>")
def api_ai_monitor(trade_id: int):
    db = _ai_db()
    rows = db.fetchall(
        "SELECT ts, price, unreal_pct, ai_decision, raw_response FROM monitor_log WHERE trade_id=? ORDER BY ts",
        (trade_id,),
    )
    return jsonify(ok=True, items=[dict(r) for r in rows])


@app.get("/api/ai/alert/<int:alert_id>")
def api_ai_alert(alert_id: int):
    """Full breakdown for one scanner alert: parsed payload, filter, news class,
    score (with reasoning + risk flags) and monitor log if it became a trade."""
    db = _ai_db()
    alert = db.fetchone(
        "SELECT id, ts, ticker, type, raw, parsed_json, news_class FROM alerts WHERE id=?",
        (alert_id,),
    )
    if not alert:
        return jsonify(ok=False, error="alert_not_found"), 404
    a = dict(alert)
    score_row = db.fetchone(
        "SELECT id, ts, score, decision, entry, tp, stop, reason, risk_flags, thinking_used, raw_json "
        "FROM scores WHERE alert_id=? ORDER BY ts DESC LIMIT 1",
        (alert_id,),
    )
    score = dict(score_row) if score_row else None
    trade_row = None
    monitor: list = []
    if score:
        trade_row = db.fetchone(
            "SELECT * FROM trades WHERE ticker=? AND open_ts>=? ORDER BY open_ts DESC LIMIT 1",
            (a["ticker"], a["ts"] - 60),
        )
        if trade_row:
            tr = dict(trade_row)
            try:
                from ai_sandbox import t212_ai as _t212_ai_disp

                tk = (tr.get("ticker") or "").strip()
                if tk:
                    tr["display_ticker"] = _t212_ai_disp.display_raw_for(tk)
            except Exception:
                pass
            trade_row = tr
            monitor = [
                dict(r)
                for r in db.fetchall(
                    "SELECT ts, price, unreal_pct, ai_decision, raw_response FROM monitor_log WHERE trade_id=? ORDER BY ts",
                    (trade_row["id"],),
                )
            ]
    return jsonify(
        ok=True,
        alert=a,
        score=score,
        trade=trade_row if trade_row else None,
        monitor=monitor,
    )


@app.get("/api/ai/watch/<ticker>")
def api_ai_watch_detail(ticker: str):
    """Return the watch-queue entry for a ticker plus its full re-evaluation
    log (initial score + every periodic review) so the dashboard can show
    how Claude's view of the setup is evolving while it sits on watch.
    """
    import json as _json

    db = _ai_db()
    t = (ticker or "").upper().strip()
    if not t:
        return jsonify(ok=False, error="missing_ticker"), 400

    row = db.fetchone(
        """SELECT ticker, score, alert_id, decision_json, alert_json,
                  added_ts, last_reviewed_ts, reviews, last_decision
             FROM watch_queue WHERE ticker=?""",
        (t,),
    )
    if not row:
        return jsonify(ok=False, error="not_on_watch"), 404
    d = dict(row)
    try:
        d["decision"] = _json.loads(d.pop("decision_json") or "{}")
    except Exception:
        d["decision"] = {}
    try:
        d["alert"] = _json.loads(d.pop("alert_json") or "{}")
    except Exception:
        d["alert"] = {}

    score_rows = db.fetchall(
        """SELECT id, ts, score, decision, entry, tp, stop, reason, raw_json
             FROM scores
            WHERE ticker=? AND ts >= ?
            ORDER BY ts ASC""",
        (t, float(d.get("added_ts") or 0.0) - 5.0),
    )
    history: list[dict] = []
    for r in score_rows:
        rj: dict = {}
        if r["raw_json"]:
            try:
                rj = _json.loads(r["raw_json"])
            except Exception:
                rj = {}
        history.append({
            "id": int(r["id"]),
            "ts": float(r["ts"]),
            "score": int(r["score"] or 0),
            "decision": r["decision"],
            "entry": r["entry"],
            "tp": r["tp"],
            "stop": r["stop"],
            "reason": r["reason"],
            "is_review": bool(rj.get("review")),
            "entry_pattern": rj.get("entry_pattern"),
            "risk_flags": rj.get("risk_flags") or [],
            "max_entry": rj.get("max_entry"),
        })

    live_price = None
    try:
        from ai_sandbox import price_data as _pd
        q = _pd.quote(t) or {}
        live = q.get("p") or q.get("price") or q.get("last")
        if live:
            live_price = float(live)
    except Exception as exc:
        _log.debug("watch live quote enrich failed for %s: %s", t, exc)

    cfg = _ai_config()
    return jsonify(
        ok=True,
        watch=d,
        history=history,
        live_price=live_price,
        review_interval_s=int(cfg.WATCH_REVIEW_INTERVAL_SECONDS),
        max_reviews=int(cfg.WATCH_MAX_REVIEWS),
        drop_score=int(cfg.WATCH_DROP_SCORE),
    )


@app.route("/api/ai/watch_history", methods=["GET"])
@app.route("/api/ai/ai_history", methods=["GET"])
def api_ai_watch_history():
    """AI History: one row per watch outcome, and one row per traded position.

    Rows with ``UPGRADE_TRADE`` are hidden when a ``TRADE`` audit row exists for
    the same ``trade_id`` so queue-upgrades merge into the single trade audit.
    """
    db = _ai_db()
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        limit = 100
    rows = db.fetchall(
        """SELECT h.id, h.ticker, h.added_ts, h.ended_ts,
                  COALESCE(h.updated_ts, h.ended_ts) AS updated_ts_ord,
                  h.reason, h.reviews,
                  h.initial_score, h.peak_score, h.final_score, h.final_decision,
                  h.final_reason, h.trade_id, h.alert_id, h.episode_type,
                  t.status            AS trade_status,
                  t.ticker            AS trade_instrument,
                  t.t212_error        AS trade_t212_error,
                  t.entry_price       AS trade_entry,
                  t.exit_price        AS trade_exit,
                  t.exit_reason       AS trade_exit_reason,
                  t.pnl_pct           AS trade_pnl_pct,
                  t.pnl_gbp           AS trade_pnl_gbp
             FROM watch_history h
        LEFT JOIN trades t ON t.id = h.trade_id
            WHERE NOT (
                      h.reason = 'UPGRADE_TRADE'
                      AND h.trade_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM watch_history x
                           WHERE x.trade_id = h.trade_id
                             AND COALESCE(x.episode_type, 'WATCH') = 'TRADE'
                      )
                  )
            ORDER BY updated_ts_ord DESC
            LIMIT ?""",
        (limit,),
    )
    items = [dict(r) for r in rows]
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        for it in items:
            ti = (it.get("trade_instrument") or "").strip()
            if ti:
                it["trade_display_ticker"] = _t212_ai_disp.display_raw_for(ti)
    except Exception as exc:
        _log.debug("watch_history display ticker enrich failed: %s", exc)
    return jsonify(ok=True, items=items)


@app.route("/api/ai/watch_history/<int:hist_id>", methods=["GET"])
@app.route("/api/ai/ai_history/<int:hist_id>", methods=["GET"])
def api_ai_watch_history_detail(hist_id: int):
    """Full review chain or trade audit blob for one AI history episode."""
    import json as _json
    import time as _time

    db = _ai_db()
    h = db.fetchone(
        """SELECT h.*,
                  t.status            AS trade_status,
                  t.ticker            AS trade_instrument,
                  t.t212_error        AS trade_t212_error,
                  t.entry_price       AS trade_entry,
                  t.exit_price        AS trade_exit,
                  t.exit_reason       AS trade_exit_reason,
                  t.pnl_pct           AS trade_pnl_pct,
                  t.pnl_gbp           AS trade_pnl_gbp
             FROM watch_history h
        LEFT JOIN trades t ON t.id = h.trade_id
            WHERE h.id=?""",
        (hist_id,),
    )
    if not h:
        return jsonify(ok=False, error="not_found"), 404
    h = dict(h)
    raw_audit = h.pop("audit_json", None)
    h["audit"] = None
    if raw_audit:
        try:
            h["audit"] = _json.loads(raw_audit)
        except Exception:
            h["audit"] = None

    sf: list = []
    if h.get("alert_id"):
        try:
            sf = db.scores_for_alert(int(h["alert_id"]))
        except Exception as exc:
            _log.debug("scores_for_alert failed id=%s: %s", h.get("alert_id"), exc)

    watch_period: dict | None = None
    wp_scores: list = []
    if h.get("trade_id") and (h.get("episode_type") or "WATCH") == "TRADE":
        wp = db.fetchone(
            """SELECT id, ticker, added_ts, ended_ts, reason, reviews,
                      initial_score, peak_score, final_score, final_decision,
                      final_reason, alert_id
                 FROM watch_history
                WHERE trade_id=? AND reason='UPGRADE_TRADE'
                ORDER BY id ASC LIMIT 1""",
            (int(h["trade_id"]),),
        )
        if wp:
            watch_period = dict(wp)
            wid = watch_period.get("alert_id")
            if wid and int(wid) != int(h.get("alert_id") or 0):
                try:
                    wp_scores = db.scores_for_alert(int(wid))
                except Exception:
                    wp_scores = []
            elif wid:
                wp_scores = sf

    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        ti = (h.get("trade_instrument") or "").strip()
        if ti:
            h["trade_display_ticker"] = _t212_ai_disp.display_raw_for(ti)
    except Exception as exc:
        _log.debug("watch_history detail display ticker enrich failed: %s", exc)
    score_rows = db.fetchall(
        """SELECT id, ts, score, decision, entry, tp, stop, reason, raw_json
             FROM scores
            WHERE ticker=? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC""",
        (
            h["ticker"],
            float(h["added_ts"]) - 5.0,
            _time.time() if (
                h.get("reason") == "WATCH_ACTIVE"
                or (h.get("ended_ts") is not None and float(h["ended_ts"]) <= 0)
            ) else float(h["ended_ts"] or _time.time()) + 5.0,
        ),
    )
    history: list[dict] = []
    for r in score_rows:
        rj: dict = {}
        if r["raw_json"]:
            try:
                rj = _json.loads(r["raw_json"])
            except Exception:
                rj = {}
        history.append({
            "id": int(r["id"]),
            "ts": float(r["ts"]),
            "score": int(r["score"] or 0),
            "decision": r["decision"],
            "entry": r["entry"],
            "tp": r["tp"],
            "stop": r["stop"],
            "reason": r["reason"],
            "is_review": bool(rj.get("review")),
            "entry_pattern": rj.get("entry_pattern"),
            "risk_flags": rj.get("risk_flags") or [],
        })
    is_live = h.get("reason") == "WATCH_ACTIVE" or (
        h.get("ended_ts") is not None and float(h["ended_ts"]) <= 0
    )
    return jsonify(
        ok=True,
        watch=h,
        history=history,
        scores_for_alert=sf,
        watch_period=watch_period,
        watch_period_scores=wp_scores,
        is_live=is_live,
    )


@app.get("/api/ai/news_history")
def api_ai_news_history():
    """All #news-scanner pipeline outcomes (including Flash/price skips) for the dashboard log."""
    db = _ai_db()
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 500)
    except ValueError:
        limit = 200
    items = db.news_scanner_log_list(limit)
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        for it in items:
            tk = (it.get("ticker") or "").strip()
            if tk:
                it["display_ticker"] = _t212_ai_disp.display_raw_for(tk)
    except Exception as exc:
        _log.debug("news_history display ticker enrich failed: %s", exc)
    return jsonify(ok=True, items=items)


@app.get("/api/ai/news_history/<int:nid>")
def api_ai_news_history_detail(nid: int):
    import json as _json

    db = _ai_db()
    row = db.news_scanner_log_get(nid)
    if not row:
        return jsonify(ok=False, error="not_found"), 404
    h = dict(row)
    raw_flash = h.pop("flash_json", None)
    h["flash"] = None
    if raw_flash:
        try:
            h["flash"] = _json.loads(raw_flash)
        except Exception:
            h["flash"] = None
    raw_audit = h.pop("audit_json", None)
    h["audit"] = None
    if raw_audit:
        try:
            h["audit"] = _json.loads(raw_audit)
        except Exception:
            h["audit"] = None
    sf: list = []
    if h.get("alert_id"):
        try:
            sf = db.scores_for_alert(int(h["alert_id"]))
        except Exception as exc:
            _log.debug("news_history scores_for_alert failed: %s", exc)
    try:
        from ai_sandbox import t212_ai as _t212_ai_disp

        tk = (h.get("ticker") or "").strip()
        if tk:
            h["display_ticker"] = _t212_ai_disp.display_raw_for(tk)
    except Exception as exc:
        _log.debug("news_history detail display ticker enrich failed: %s", exc)
    return jsonify(ok=True, log=h, scores_for_alert=sf)


@app.post("/api/ai/toggle")
def api_ai_toggle():
    from ai_sandbox import config as _ai_cfg

    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    _ai_cfg.persist_ai_trading_enabled(enabled)
    return jsonify(ok=True, enabled=enabled)


@app.post("/api/reasoning-test/stream")
def api_reasoning_test_stream():
    """SSE: synthetic price tape + streamed Gemini chat turns (development lab only).

    Not gated on ``AI_TRADING_ENABLED`` — this path does not open trades or run the engine.
    """
    payload = request.get_json(silent=True) or {}

    def generate():
        try:
            from ai_sandbox.reasoning_stream_lab import iter_sse_lines

            yield from iter_sse_lines(payload)
        except Exception as exc:
            _log.exception("reasoning-test stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=headers,
    )


@app.post("/api/ai/test/inject_alert")
def api_ai_test_inject():
    """Drop a synthetic TrendVision-format message into the scanner feed so the
    full engine pipeline runs against it.

    Body JSON: { "content": "**AAPL** :flag_us: ... `RV` 50x ...", "ticker": "AAPL" }
    """
    import datetime as _dt

    payload = request.get_json(silent=True) or {}
    content = (payload.get("content") or "").strip()
    if not content:
        return jsonify(ok=False, error="missing content"), 400
    from ai_sandbox import config as _aicfg

    feed = _aicfg.SCANNER_FEED_PATH
    feed.parent.mkdir(parents=True, exist_ok=True)
    msg = {
        "timestamp": _dt.datetime.utcnow().isoformat() + "+00:00",
        "author": "TEST_INJECT",
        "author_id": "0",
        "content": content,
        "channel_id": "test",
        "guild_id": None,
        "embeds": 0,
        "attachments": 0,
    }
    with open(feed, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    return jsonify(ok=True, injected=msg)


@app.post("/api/ai/test/order")
def api_ai_test_order():
    """End-to-end broker plumbing test: place a small T212 BUY on the AI account.

    Body JSON: {
      "ticker": "AAPL_US_EQ",   // T212 instrument code (or raw symbol if known to the map)
      "quantity": 1,
      "tp_pct": 5,              // optional, default 5
      "stop_pct": 5             // optional, default 5
    }

    Returns the new trade_id. Slot is reserved + monitor is started. Use
    /api/ai/test/close/<trade_id> to force-close.
    """
    import asyncio as _asyncio
    import time as _time

    payload = request.get_json(silent=True) or {}
    raw_tkr = (payload.get("ticker") or "").strip().upper()
    if not raw_tkr:
        return jsonify(ok=False, error="missing ticker"), 400
    try:
        qty = float(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        qty = 1.0
    tp_pct = float(payload.get("tp_pct") or 5)
    stop_pct = float(payload.get("stop_pct") or 5)

    from ai_sandbox import service as _ai_svc, db as _ai_db_mod, t212_ai
    from ai_sandbox.position_monitor import run_slot

    eng = _ai_svc.get_engine()
    if eng is None:
        return jsonify(ok=False, error="ai_engine_not_running"), 503
    loop = _ai_svc._loop
    if loop is None:
        return jsonify(ok=False, error="ai_engine_loop_not_ready"), 503

    # Resolve T212 instrument code via ai_sandbox cache / map (standalone; no Trading_AI).
    t212_ticker = t212_ai.resolve_ticker(raw_tkr) or raw_tkr

    # Market order with extendedHours=True works in T212 demo AH (slow fill).
    fut = _asyncio.run_coroutine_threadsafe(t212_ai.place_market(t212_ticker, qty), loop)
    try:
        order = fut.result(timeout=20)
    except Exception as exc:
        return jsonify(ok=False, error=f"place_market failed: {exc}"), 502
    if order.get("error"):
        return jsonify(ok=False, error=order["error"], order=order), 502

    # Poll positions for up to 120s waiting for the fill (AH market orders can take ~90s).
    order_id = order.get("id")
    fill = None
    deadline = _time.time() + 120
    while _time.time() < deadline:
        _time.sleep(2.0)
        fut2 = _asyncio.run_coroutine_threadsafe(t212_ai.get_positions(), loop)
        positions = fut2.result(timeout=10)
        for p in positions:
            inst = p.get("instrument") if isinstance(p, dict) else None
            tkr = (inst or {}).get("ticker") if inst else p.get("ticker")
            if tkr == t212_ticker:
                fill = p
                break
        if fill:
            break
    if not fill:
        return jsonify(
            ok=False,
            error="order_not_filled_within_120s",
            order=order,
            order_id=order_id,
            hint="Order may still be queued. Check /api/ai/account and use cancel_order if needed.",
        ), 502
    entry_price = float(fill.get("averagePricePaid") or fill.get("averagePrice") or fill.get("currentPrice") or 0.0)
    if entry_price <= 0:
        return jsonify(ok=False, error="no_entry_price", order=order, fill=fill), 502

    tp = entry_price * (1 + tp_pct / 100.0)
    stop = entry_price * (1 - stop_pct / 100.0)

    from ai_sandbox import config as _ai_cfg_deploy

    deployed_gbp = _ai_cfg_deploy.usd_notionals_to_gbp(qty * entry_price)

    # Reserve an open slot, log the trade, kick off the monitor task on the engine loop.
    async def _setup():
        slot = await eng.mgr.find_open_slot()
        if not slot:
            return None, "no_open_slot"
        trade_id = _ai_db_mod.insert(
            """INSERT INTO trades(slot, ticker, score_id, alert_id, entry_price, tp, stop, capital_gbp,
                                  quantity, open_ts, status, t212_open_order_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slot.index,
                t212_ticker,
                None,
                None,
                entry_price,
                tp,
                stop,
                deployed_gbp,
                qty,
                _time.time(),
                "OPEN",
                str(order.get("id") or ""),
            ),
        )
        await eng.mgr.assign(
            slot,
            ticker=t212_ticker,
            trade_id=trade_id,
            entry=entry_price,
            tp=tp,
            stop=stop,
            capital_gbp=deployed_gbp,
        )
        setup = {
            "ticker": t212_ticker, "entry": entry_price, "tp": tp, "stop": stop,
            "capital_gbp": deployed_gbp, "reason": "manual e2e test order",
            "risk_flags": ["manual_test"], "alert": {"type": "TEST"},
        }
        import asyncio as __asyncio
        task = __asyncio.create_task(run_slot(slot, eng.mgr, setup), name=f"ai-slot-{slot.index}-TEST")
        eng._monitor_tasks[slot.index] = task
        return trade_id, None

    fut3 = _asyncio.run_coroutine_threadsafe(_setup(), loop)
    trade_id, err = fut3.result(timeout=10)
    if err:
        return jsonify(ok=False, error=err, order=order), 503
    return jsonify(
        ok=True,
        trade_id=trade_id,
        ticker=t212_ticker,
        entry_price=entry_price,
        tp=tp,
        stop=stop,
        quantity=qty,
        order=order,
        message=f"Trade opened. Monitor running. POST /api/ai/test/close/{trade_id} to force-close.",
    )


@app.post("/api/ai/test/close/<int:trade_id>")
def api_ai_test_close(trade_id: int):
    """Force-close a test trade with a market sell. Returns the exit fill if available."""
    import asyncio as _asyncio
    import time as _time
    from ai_sandbox import service as _ai_svc, db as _ai_db_mod, t212_ai

    loop = _ai_svc._loop
    if loop is None:
        return jsonify(ok=False, error="ai_engine_loop_not_ready"), 503

    row = _ai_db_mod.fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not row:
        return jsonify(ok=False, error="trade_not_found"), 404
    if row["status"] == "CLOSED":
        return jsonify(ok=True, message="already closed", trade=dict(row))
    ticker = row["ticker"]
    qty = float(row["quantity"] or 0)
    if qty <= 0:
        return jsonify(ok=False, error="no_quantity"), 400

    # Market sell with extendedHours=True. Negative quantity = sell.
    fut = _asyncio.run_coroutine_threadsafe(t212_ai.place_market(ticker, -qty), loop)
    try:
        order = fut.result(timeout=20)
    except Exception as exc:
        return jsonify(ok=False, error=f"sell failed: {exc}"), 502
    if order.get("error"):
        return jsonify(ok=False, error=order["error"], order=order), 502

    # Reference price from Yahoo for P&L if we lose visibility after the fill.
    ref_price = float(row["entry_price"] or 0.0)
    try:
        from ai_sandbox import price_data
        q = price_data.quote(ticker.split("_")[0]) or {}
        rp = float(q.get("p") or q.get("price") or q.get("last") or q.get("regularMarketPrice") or 0.0)
        if rp > 0:
            ref_price = rp
    except Exception:
        pass

    exit_price = ref_price
    deadline = _time.time() + 120
    closed = False
    while _time.time() < deadline:
        _time.sleep(2.0)
        fut2 = _asyncio.run_coroutine_threadsafe(t212_ai.get_positions(), loop)
        positions = fut2.result(timeout=10)
        still_open = False
        for p in positions:
            inst = p.get("instrument") if isinstance(p, dict) else None
            tkr = (inst or {}).get("ticker") if inst else p.get("ticker")
            if tkr == ticker:
                still_open = True
                if p.get("currentPrice"):
                    exit_price = float(p["currentPrice"])
                break
        if not still_open:
            closed = True
            break
    if not closed:
        return jsonify(ok=False, error="sell_not_filled_within_120s", order=order), 502
    exit_ts_wall = _time.time()
    _ai_db_mod.execute(
        """UPDATE trades SET exit_price=?, exit_ts=?, exit_reason=?, status='CLOSED',
                              pnl_pct=ROUND((?-entry_price)/entry_price*100, 4),
                              pnl_gbp=ROUND((?-entry_price)*quantity, 4),
                              t212_close_order_id=?
           WHERE id=?""",
        (exit_price, exit_ts_wall, "manual_test_close", exit_price, exit_price, str(order.get("id") or ""), trade_id),
    )
    try:
        _ai_db_mod.trade_audit_finalize(
            trade_id,
            exit_ts=exit_ts_wall,
            exit_reason="manual_test_close",
            risk_at_exit={"manual_test_close": True},
            close_order_id=str(order.get("id") or ""),
        )
    except Exception:
        _log.exception("trade_audit_finalize (manual_test_close) failed trade_id=%s", trade_id)
    # Free the slot.
    eng = _ai_svc.get_engine()
    if eng:
        for s in eng.mgr.state.slots:
            if s.trade_id == trade_id:
                async def _free(slot=s):
                    await eng.mgr.close(slot, exit_price=exit_price, reason="manual_test_close")
                _asyncio.run_coroutine_threadsafe(_free(), loop).result(timeout=5)
                break
    return jsonify(ok=True, trade_id=trade_id, exit_price=exit_price, order=order)


@app.post("/api/ai/test/attach/<int:trade_id>")
def api_ai_test_attach(trade_id: int):
    """Attach an existing DB trade row to the live engine slot manager + start its monitor.

    Useful when a trade was inserted into the DB out-of-band (e.g. a T212 order
    that filled after our buy-endpoint timed out) and we need the UI/monitor to
    pick it up without a service restart.
    """
    import asyncio as _asyncio
    from ai_sandbox import service as _ai_svc, db as _ai_db_mod
    from ai_sandbox.position_monitor import run_slot

    eng = _ai_svc.get_engine()
    loop = _ai_svc._loop
    if eng is None or loop is None:
        return jsonify(ok=False, error="ai_engine_not_running"), 503

    row = _ai_db_mod.fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not row:
        return jsonify(ok=False, error="trade_not_found"), 404
    if row["status"] == "CLOSED":
        return jsonify(ok=False, error="trade_already_closed"), 400

    async def _attach():
        slot = await eng.mgr.find_open_slot()
        if not slot:
            return None, "no_open_slot"
        await eng.mgr.assign(
            slot,
            ticker=row["ticker"],
            trade_id=row["id"],
            entry=float(row["entry_price"]),
            tp=float(row["tp"]),
            stop=float(row["stop"]),
            capital_gbp=float(row["capital_gbp"] or 0),
        )
        setup = {
            "ticker": row["ticker"], "entry": float(row["entry_price"]),
            "tp": float(row["tp"]), "stop": float(row["stop"]),
            "capital_gbp": float(row["capital_gbp"] or 0),
            "reason": "manual attach (test)",
            "risk_flags": ["manual_attach"],
            "alert": {"type": "TEST"},
        }
        import asyncio as __asyncio
        task = __asyncio.create_task(run_slot(slot, eng.mgr, setup), name=f"ai-slot-{slot.index}-ATTACH")
        eng._monitor_tasks[slot.index] = task
        return slot.index, None

    fut = _asyncio.run_coroutine_threadsafe(_attach(), loop)
    slot_idx, err = fut.result(timeout=10)
    if err:
        return jsonify(ok=False, error=err), 503
    return jsonify(ok=True, trade_id=trade_id, slot=slot_idx, message="Attached. Monitor running.")


@app.post("/api/ai/test/cancel_order/<int:order_id>")
def api_ai_test_cancel(order_id: int):
    import asyncio as _asyncio
    from ai_sandbox import service as _ai_svc, t212_ai
    loop = _ai_svc._loop
    if loop is None:
        return jsonify(ok=False, error="ai_engine_loop_not_ready"), 503
    fut = _asyncio.run_coroutine_threadsafe(t212_ai.cancel_order(order_id), loop)
    try:
        result = fut.result(timeout=10)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502
    return jsonify(ok=True, result=result)


@app.get("/api/ai/account")
def api_ai_account():
    """Return AI T212 account cash / balance from the shared poller cache."""
    cash = _ai_cash_cached()
    if not cash:
        return jsonify(ok=False, error="account_cache_empty"), 503
    if cash.get("error"):
        return jsonify(ok=False, error=str(cash.get("error"))), 503
    return jsonify(ok=True, cash=cash)

