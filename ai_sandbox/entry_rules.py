"""Deterministic entry rules from trading_model_rules_and_prompting.md (§1, §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import time

from . import config, db
from .grader.hard_rules import FLOAT_LIMIT, MC_LIMIT, PRICE_MIN, RV_MIN

SETUP_PRIORITY: tuple[str, ...] = (
    "alert2_extreme_tape_confirmed",
    "clean_catalyst_trade",
    "squeeze_momentum_trade",
    "extreme_momentum_trade",  # Setup F — high-RV momentum, no structure required
    "pure_tape_trade",
    "extreme_tape_trade",
)

CONTINUATION_LABELS = frozenset({"MOMENTUM", "BREAKOUT", "NBREAK", "HUGE", "HUGE S"})


@dataclass
class EntryDecision:
    decision: str  # TRADE_NOW | WATCH_ONLY | SKIP
    matched_setup: str | None = None
    reject_reason: str | None = None
    size_grade: str = "FAIL"
    structure_grade: str = "FAIL"
    tape_confirmed: bool = False
    notes: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "matched_setup": self.matched_setup,
            "reject_reason": self.reject_reason,
            "size_grade": self.size_grade,
            "structure_grade": self.structure_grade,
            "tape_confirmed": self.tape_confirmed,
            "notes": self.notes,
            "context": self.context,
        }


def _norm_label(label: str | None) -> str | None:
    if not label:
        return None
    u = label.strip().upper()
    if u.startswith("MOMENTUM"):
        return "MOMENTUM"
    if u.startswith("BREAKOUT"):
        return "BREAKOUT"
    if u.startswith("NBREAK"):
        return "NBREAK"
    if u.startswith("HUGE S"):
        return "HUGE S"
    if u.startswith("HUGE"):
        return "HUGE"
    if u.startswith("REV"):
        return "REV V"
    if u.startswith("BTT"):
        return "BTT V"
    return label.strip()


def _is_continuation_label(label: str | None) -> bool:
    n = _norm_label(label)
    if not n:
        return False
    if n in CONTINUATION_LABELS:
        return True
    return n.upper().startswith("MOMENTUM")


def _flags_from_row(row: dict[str, Any], *, news_class: str | None = None) -> dict[str, bool]:
    indicators = set(row.get("indicators") or [])
    tags = set(row.get("tags") or [])
    raw = str(row.get("raw") or "")
    has_news = bool(
        row.get("news_headline")
        or (news_class and str(news_class).upper() not in ("", "NONE"))
        or "`NEWS`" in raw
        or "NEWS:" in raw.upper()
    )
    zero_borrow = "0 Borrow" in indicators or "0Borrow" in tags or row.get("zero_borrow")
    reg_sho = "Reg SHO" in indicators or "RegSHO" in tags
    pot_squeeze = "Potential Squeeze" in indicators or "PotSqueeze" in tags
    known_runner = "Known Runner" in indicators or "KnownRunner" in tags
    has_ipo = bool(row.get("ipo")) or "IPO" in raw.upper()
    has_rs = bool(row.get("reverse_split")) or "R/S" in raw.upper()
    return {
        "has_news": bool(has_news),
        "has_ipo": has_ipo,
        "has_reverse_split": has_rs,
        "zero_borrow": bool(zero_borrow),
        "reg_sho": bool(reg_sho),
        "potential_squeeze": bool(pot_squeeze),
        "known_runner": bool(known_runner),
    }


def _size_grade(ft: float | None, mc: float | None) -> str:
    if ft is not None and ft < 5_000_000:
        return "PASS_STRONG"
    if mc is not None and mc < 10_000_000:
        return "PASS_STRONG"
    if ft is not None and ft <= 20_000_000:
        return "PASS"
    if mc is not None and mc <= 30_000_000:
        return "PASS"
    if ft is not None and ft <= FLOAT_LIMIT:
        return "PARTIAL"
    if mc is not None and mc <= MC_LIMIT:
        return "PARTIAL"
    return "FAIL"


def _structure_grade(flags: dict[str, bool]) -> str:
    squeeze_count = sum(
        1 for k in ("zero_borrow", "reg_sho", "potential_squeeze") if flags.get(k)
    )
    if squeeze_count >= 2:
        return "PASS_STRONG"
    if squeeze_count == 1:
        return "PASS"
    if flags.get("known_runner"):
        return "PARTIAL"
    return "FAIL"


def _tape_confirmed(
    *,
    price: float | None,
    prior_price: float | None,
    label: str | None,
    rv: float | None,
) -> bool:
    if price is None or prior_price is None:
        return False
    if float(price) <= float(prior_price):
        return False
    if _is_continuation_label(label):
        return True
    if rv is not None and float(rv) >= 50:
        return True
    return False


def _hard_reject(ctx: dict[str, Any]) -> str | None:
    alert_no = int(ctx.get("alert_number") or 0)
    if alert_no <= 1:
        return "alert_number_1"
    if ctx.get("prior_price") is None:
        return "no_prior_same_day_alert"
    price = ctx.get("price")
    prior_price = ctx.get("prior_price")
    if price is not None and prior_price is not None and float(price) <= float(prior_price):
        return "price_not_above_prior"
    ft = ctx.get("float")
    if ft is not None and float(ft) > FLOAT_LIMIT:
        return "float_too_large"
    mc = ctx.get("market_cap")
    if mc is not None and float(mc) > MC_LIMIT:
        return "mc_too_large"
    if price is not None and float(price) < PRICE_MIN:
        return "price_too_low"
    rv = ctx.get("rv")
    if rv is None:
        return "rv_missing"
    if float(rv) < RV_MIN:
        return "rv_too_low"
    if ctx.get("has_reverse_split"):
        return "reverse_split_present"
    label = _norm_label(ctx.get("label"))
    if label == "REV V":
        return "label_rev_v"
    if label == "BTT V":
        return "label_btt_v"
    pct = ctx.get("change_pct")
    rv = ctx.get("rv")
    # Allow high-change MOMENTUM alerts when RV is very strong (≥ 200x).
    # A stock up 80%+ on 200x RV is exactly the extreme momentum event we want.
    # Only block when RV is modest (< 200) to filter late/exhausted moves.
    if label == "MOMENTUM" and pct is not None and float(pct) > 80:
        if rv is None or float(rv) < 200:
            return "momentum_change_over_80"
    return None


def _match_setups(
    ctx: dict[str, Any],
    *,
    size_grade: str,
    structure_grade: str,
    tape_ok: bool,
) -> list[str]:
    alert_no = int(ctx.get("alert_number") or 0)
    rv = float(ctx.get("rv") or 0)
    price = float(ctx.get("price") or 0)
    prior_price = ctx.get("prior_price")
    prior_f = float(prior_price) if prior_price is not None else 0.0
    pct = ctx.get("change_pct")
    pct_ok = pct is None or float(pct) <= 80
    matched: list[str] = []

    if alert_no == 2 and rv >= 500 and price > prior_f > 0:
        if ctx.get("has_news") or ctx.get("zero_borrow") or ctx.get("reg_sho"):
            if pct_ok:
                matched.append("alert2_extreme_tape_confirmed")

    if alert_no >= 3:
        # Setup B — clean_catalyst_trade: raised RV threshold 50→100 (2026-06-16)
        if size_grade in ("PASS_STRONG", "PASS") and ctx.get("has_news") and tape_ok and rv >= 100:
            matched.append("clean_catalyst_trade")
        # Setup C — squeeze_momentum_trade: raised RV threshold 50→100 (2026-06-16)
        if (
            size_grade in ("PASS_STRONG", "PASS")
            and structure_grade in ("PASS_STRONG", "PASS")
            and tape_ok
            and rv >= 100
        ):
            matched.append("squeeze_momentum_trade")
        # Setup F — extreme_momentum_trade: high RV momentum, no news/structure needed.
        # Catches stocks like PRFX, CRVO, WCT that run on pure tape with no squeeze flags.
        # Requirements: RV ≥ 150, continuation label, price rising, size PASS or better.
        if (
            size_grade in ("PASS_STRONG", "PASS")
            and tape_ok
            and rv >= 150
            and _is_continuation_label(ctx.get("label"))
        ):
            matched.append("extreme_momentum_trade")
        # Setup D — pure_tape_trade: unchanged at RV ≥ 200, PASS_STRONG size only
        if size_grade == "PASS_STRONG" and tape_ok and rv >= 200:
            matched.append("pure_tape_trade")
        if rv >= 500 and price > prior_f > 0:
            matched.append("extreme_tape_trade")

    return matched


def _pick_setup(matches: list[str]) -> str | None:
    for name in SETUP_PRIORITY:
        if name in matches:
            return name
    return None


def build_context(
    ticker: str,
    alert_id: int,
    alert: dict[str, Any],
    *,
    news_class: str | None = None,
) -> dict[str, Any]:
    """Build alert context from same-day episode + live alert fields."""
    since = config.uk_day_start_ts()
    episode = db.movement_alerts_for_ticker_day(ticker, since_ts=since)
    current: dict[str, Any] | None = None
    prior: dict[str, Any] | None = None
    for i, row in enumerate(episode):
        if int(row.get("alert_id") or 0) == int(alert_id):
            current = row
            if i > 0:
                prior = episode[i - 1]
            break
    if current is None and episode:
        current = episode[-1]
        if len(episode) >= 2:
            prior = episode[-2]

    base = current or {}
    flags = _flags_from_row({**base, **alert, "raw": alert.get("raw") or base.get("raw")}, news_class=news_class)
    alert_no = int(base.get("rank") or base.get("sequence") or alert.get("rank") or 1)
    price = base.get("price") if base.get("price") is not None else alert.get("price")
    prior_price = prior.get("price") if prior else None

    return {
        "ticker": ticker.upper(),
        "alert_id": alert_id,
        "alert_number": alert_no,
        "label": base.get("label") or alert.get("label"),
        "price": price,
        "prior_price": prior_price,
        "change_pct": base.get("pct") or alert.get("pct"),
        "float": base.get("float") or alert.get("float"),
        "market_cap": base.get("market_cap") or alert.get("market_cap"),
        "rv": base.get("rv") if base.get("rv") is not None else alert.get("rv"),
        "volume_1v": base.get("volume_1v") or alert.get("volume_1v"),
        **flags,
    }


def evaluate(
    ticker: str,
    alert_id: int,
    alert: dict[str, Any],
    *,
    news_class: str | None = None,
) -> EntryDecision:
    """Return TRADE_NOW, WATCH_ONLY, or SKIP per §1.8 / §3.1."""
    ctx = build_context(ticker, alert_id, alert, news_class=news_class)
    reject = _hard_reject(ctx)
    size_grade = _size_grade(
        float(ctx["float"]) if ctx.get("float") is not None else None,
        float(ctx["market_cap"]) if ctx.get("market_cap") is not None else None,
    )
    structure_grade = _structure_grade(ctx)
    tape_ok = _tape_confirmed(
        price=float(ctx["price"]) if ctx.get("price") is not None else None,
        prior_price=float(ctx["prior_price"]) if ctx.get("prior_price") is not None else None,
        label=ctx.get("label"),
        rv=float(ctx["rv"]) if ctx.get("rv") is not None else None,
    )

    if reject:
        return EntryDecision(
            decision="SKIP",
            reject_reason=reject,
            size_grade=size_grade,
            structure_grade=structure_grade,
            tape_confirmed=tape_ok,
            notes=f"Hard reject: {reject}",
            context=ctx,
        )

    matches = _match_setups(ctx, size_grade=size_grade, structure_grade=structure_grade, tape_ok=tape_ok)
    setup = _pick_setup(matches)
    if setup:
        return EntryDecision(
            decision="TRADE_NOW",
            matched_setup=setup,
            size_grade=size_grade,
            structure_grade=structure_grade,
            tape_confirmed=tape_ok,
            notes=f"Approved setup: {setup}",
            context=ctx,
        )

    return EntryDecision(
        decision="WATCH_ONLY",
        size_grade=size_grade,
        structure_grade=structure_grade,
        tape_confirmed=tape_ok,
        notes="No hard reject but no approved setup matched",
        context=ctx,
    )


def _has_squeeze_structure(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("zero_borrow") or ctx.get("reg_sho") or ctx.get("potential_squeeze"))


def apply_loss_filters(result: EntryDecision) -> EntryDecision:
    """Loss-analysis entry blocks (known runner without news catalyst)."""
    if not config.candle_loss_filters_enabled():
        return result
    if result.decision != "TRADE_NOW":
        return result
    ctx = result.context
    if not ctx.get("known_runner") or ctx.get("has_news"):
        return result
    if _has_squeeze_structure(ctx):
        return result
    return EntryDecision(
        decision="SKIP",
        matched_setup=result.matched_setup,
        reject_reason="known_runner_no_news",
        size_grade=result.size_grade,
        structure_grade=result.structure_grade,
        tape_confirmed=result.tape_confirmed,
        notes="Blocked — Known Runner without news or squeeze structure",
        context=ctx,
    )


def _last_entry_setup_for_alert(alert_id: int | None) -> str | None:
    if not alert_id:
        return None
    row = db.fetchone(
        "SELECT raw_json FROM scores WHERE alert_id=? ORDER BY id DESC LIMIT 1",
        (int(alert_id),),
    )
    if not row or not row["raw_json"]:
        return None
    try:
        import json

        payload = json.loads(str(row["raw_json"]))
    except Exception:
        return None
    return payload.get("matched_setup") or payload.get("reason")


def apply_reentry_guards(result: EntryDecision, ticker: str) -> EntryDecision:
    """Post-loss cooldown, same-day cap, and stricter squeeze re-entry bar."""
    if result.decision != "TRADE_NOW":
        return result
    ctx = result.context

    max_day = config.candle_max_trades_per_ticker_day()
    if max_day > 0:
        closed_today = db.closed_trades_for_ticker_day(
            ticker, since_ts=config.uk_day_start_ts()
        )
        if len(closed_today) >= max_day:
            return EntryDecision(
                decision="SKIP",
                matched_setup=result.matched_setup,
                reject_reason="ticker_daily_trade_cap",
                size_grade=result.size_grade,
                structure_grade=result.structure_grade,
                tape_confirmed=result.tape_confirmed,
                notes=(
                    f"Blocked — {len(closed_today)} trades on {ticker} today "
                    f"(cap {max_day})"
                ),
                context=ctx,
            )

    cooldown_min = config.candle_ticker_loss_cooldown_minutes()
    if cooldown_min > 0:
        last = db.last_closed_trade_for_ticker(ticker)
        if last:
            pnl = float(last.get("pnl_gbp") or 0.0)
            exit_ts = float(last.get("exit_ts") or 0.0)
            age_min = (time.time() - exit_ts) / 60.0 if exit_ts > 0 else 9999.0
            if pnl < 0 and age_min < cooldown_min:
                return EntryDecision(
                    decision="SKIP",
                    matched_setup=result.matched_setup,
                    reject_reason="ticker_loss_cooldown",
                    size_grade=result.size_grade,
                    structure_grade=result.structure_grade,
                    tape_confirmed=result.tape_confirmed,
                    notes=(
                        f"Blocked — lost £{abs(pnl):.0f} on {ticker} "
                        f"{age_min:.0f}m ago (cooldown {cooldown_min:.0f}m)"
                    ),
                    context=ctx,
                )

    any_cd = config.candle_ticker_any_exit_cooldown_minutes()
    if any_cd > 0:
        last = db.last_closed_trade_for_ticker(ticker)
        if last:
            exit_ts = float(last.get("exit_ts") or 0.0)
            age_min = (time.time() - exit_ts) / 60.0 if exit_ts > 0 else 9999.0
            if age_min < any_cd:
                return EntryDecision(
                    decision="SKIP",
                    matched_setup=result.matched_setup,
                    reject_reason="ticker_reentry_cooldown",
                    size_grade=result.size_grade,
                    structure_grade=result.structure_grade,
                    tape_confirmed=result.tape_confirmed,
                    notes=(
                        f"Blocked — last {ticker} exit {age_min:.0f}m ago "
                        f"(re-entry cooldown {any_cd:.0f}m)"
                    ),
                    context=ctx,
                )

    if result.matched_setup == "squeeze_momentum_trade":
        last = db.last_closed_trade_for_ticker(ticker)
        if last and float(last.get("pnl_gbp") or 0.0) < 0:
            prev_setup = _last_entry_setup_for_alert(last.get("alert_id"))
            prev_reason = str(last.get("exit_reason") or "")
            if prev_setup == "squeeze_momentum_trade" and "stop" in prev_reason:
                alert_n = int(ctx.get("alert_number") or 99)
                min_alert = config.candle_squeeze_reentry_min_alert()
                if alert_n < min_alert:
                    return EntryDecision(
                        decision="SKIP",
                        matched_setup=result.matched_setup,
                        reject_reason="squeeze_reentry_blocked",
                        size_grade=result.size_grade,
                        structure_grade=result.structure_grade,
                        tape_confirmed=result.tape_confirmed,
                        notes=(
                            f"Blocked — prior squeeze stop on {ticker}; "
                            f"need alert #{min_alert}+ (got #{alert_n})"
                        ),
                        context=ctx,
                    )
    return result


async def evaluate_entry(
    ticker: str,
    alert_id: int,
    alert: dict[str, Any],
    *,
    news_class: str | None = None,
) -> EntryDecision:
    """Deterministic rules + loss filters + optional Gemini §3.1 validation."""
    result = apply_reentry_guards(
        apply_loss_filters(evaluate(ticker, alert_id, alert, news_class=news_class)),
        ticker,
    )
    if not config.entry_ai_enabled():
        return result
    from . import entry_grader

    merged, _ai = await entry_grader.grade_entry(
        ticker, alert_id, alert, result, news_class=news_class
    )
    return apply_reentry_guards(apply_loss_filters(merged), ticker)


def log_decision(ticker: str, alert_id: int, result: EntryDecision) -> None:
    """Persist entry decision for feed / audit."""
    feed_decision = "TRADE" if result.decision == "TRADE_NOW" else result.decision
    reason = result.matched_setup or result.reject_reason or result.notes
    payload = result.to_dict()
    payload["decision"] = feed_decision
    payload["reason"] = reason
    payload["score"] = 100 if result.decision == "TRADE_NOW" else 0
    payload["entry"] = None
    payload["tp"] = None
    payload["stop"] = None
    payload["risk_flags"] = []
    payload["source"] = "entry_rules"
    db.log_score(alert_id, ticker, payload, thinking_used=False)
