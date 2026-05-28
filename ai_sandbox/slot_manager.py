"""5-slot manager: tracks state, queues alerts, enforces concentration rules."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import config, db

_log = logging.getLogger("ai_sandbox.slot_manager")


@dataclass
class Slot:
    index: int
    state: str = "OPEN"  # OPEN | ACTIVE | COOLING
    ticker: str | None = None
    trade_id: int | None = None
    entry: float | None = None
    tp: float | None = None
    stop: float | None = None
    capital_gbp: float = 0.0  # deployed stake for ACTIVE slot; set on assign
    opened_ts: float | None = None
    cooling_until: float | None = None
    last_decision: str | None = None
    last_price: float | None = None
    unreal_pct: float | None = None
    # Resting DAY limit TP sell (regular session); cleared on fill cancel or slot reset.
    take_profit_order_id: str | None = None


@dataclass
class QueuedAlert:
    ts: float
    score: int
    ticker: str
    alert: dict[str, Any]
    decision_payload: dict[str, Any]
    alert_id: int | None = None


@dataclass
class SlotManagerState:
    slots: list[Slot] = field(default_factory=list)
    queue: list[QueuedAlert] = field(default_factory=list)


class SlotManager:
    def __init__(self) -> None:
        self.state = SlotManagerState(slots=[Slot(index=i) for i in range(config.SLOT_COUNT)])
        self._lock = asyncio.Lock()

    # ── inspection ─────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "slots": [s.__dict__ for s in self.state.slots],
            "queue": [q.__dict__ for q in self.state.queue],
        }

    def ticker_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.state.slots:
            if s.state == "ACTIVE" and s.ticker:
                out[s.ticker] = out.get(s.ticker, 0) + 1
        return out

    def negative_slot_count(self) -> int:
        return sum(1 for s in self.state.slots if s.state == "ACTIVE" and (s.unreal_pct or 0) < 0)

    # ── slot lifecycle ─────────────────────────────────────────────────────
    def sweep_expired_cooling(self) -> None:
        """Clear cooling timers once elapsed.

        ``find_open_slot`` already resets expired cooling when reserving a slot;
        without this sweep, idle dashboards would stay amber until the next enqueue.
        """
        now = time.time()
        for s in self.state.slots:
            if s.state == "COOLING" and s.cooling_until is not None and now >= s.cooling_until:
                self._reset_slot(s)

    async def find_open_slot(self) -> Slot | None:
        async with self._lock:
            now = time.time()
            for s in self.state.slots:
                if s.state == "COOLING" and s.cooling_until and now >= s.cooling_until:
                    self._reset_slot(s)
                if s.state == "OPEN":
                    return s
            return None

    def _reset_slot(self, s: Slot) -> None:
        s.state = "OPEN"
        s.ticker = None
        s.trade_id = None
        s.entry = s.tp = s.stop = None
        s.opened_ts = None
        s.cooling_until = None
        s.last_decision = None
        s.last_price = None
        s.unreal_pct = None
        s.take_profit_order_id = None
        s.capital_gbp = 0.0

    async def assign(
        self,
        slot: Slot,
        *,
        ticker: str,
        trade_id: int,
        entry: float,
        tp: float,
        stop: float,
        capital_gbp: float | None = None,
    ) -> None:
        async with self._lock:
            slot.state = "ACTIVE"
            slot.ticker = ticker
            slot.trade_id = trade_id
            slot.entry = entry
            slot.tp = tp
            slot.stop = stop
            slot.opened_ts = time.time()
            if capital_gbp is not None:
                slot.capital_gbp = float(capital_gbp)
            else:
                slot.capital_gbp = 0.0
            slot.take_profit_order_id = None

    async def close(self, slot: Slot, *, exit_price: float | None, reason: str) -> None:
        async with self._lock:
            slot.state = "COOLING"
            slot.cooling_until = time.time() + config.COOLING_SECONDS
            slot.last_decision = f"closed:{reason}"
            slot.last_price = exit_price

    async def force_reset_slot(self, slot: Slot) -> None:
        """Immediately return a slot to OPEN (broker reconciler / orphan cleanup)."""
        async with self._lock:
            self._reset_slot(slot)

    # ── queue (persistent watch list, mirrored to DB) ──────────────────────
    def hydrate_from_db(self) -> None:
        """Rebuild the in-memory queue from the persisted watch_queue table.

        Called once at engine startup so a service restart does not lose
        WATCH-rated tickers. Drops anything older than QUEUE_TTL_SECONDS.
        """
        try:
            rows = db.watch_all()
        except Exception:
            return
        now = time.time()
        kept: list[QueuedAlert] = []
        for r in rows:
            tkr = (r.get("ticker") or "").upper()
            bl_row = db.t212_blacklist_get(tkr) if tkr else None
            if bl_row:
                added_ts = float(r.get("added_ts") or now)
                reason_tag = ""
                try:
                    reason_tag = str(bl_row["reason"] or "")
                except Exception:
                    pass
                try:
                    db.watch_episode_finalize(
                        ticker=tkr,
                        added_ts_fallback=added_ts,
                        ended_ts=now,
                        reason="DROP_BLACKLIST",
                        reviews=int(r.get("reviews") or 0),
                        initial_score=None,
                        peak_score=int(r.get("score") or 0),
                        final_score=int(r.get("score") or 0),
                        final_decision=r.get("last_decision"),
                        final_reason=f"T212 blacklist ({reason_tag})"[:500],
                        trade_id=None,
                        alert_id=r.get("alert_id"),
                        audit_tail={
                            "kind": "hydrate_blacklist",
                            "ts": now,
                        },
                    )
                except Exception:
                    pass
                try:
                    db.watch_remove(tkr)
                except Exception:
                    pass
                continue
            added_ts = float(r.get("added_ts") or now)
            if (now - added_ts) > config.QUEUE_TTL_SECONDS:
                try:
                    db.watch_episode_finalize(
                        ticker=r["ticker"],
                        added_ts_fallback=added_ts,
                        ended_ts=now,
                        reason="EXPIRED",
                        reviews=int(r.get("reviews") or 0),
                        initial_score=None,
                        peak_score=int(r.get("score") or 0),
                        final_score=int(r.get("score") or 0),
                        final_decision=r.get("last_decision"),
                        final_reason="queue TTL exceeded",
                        trade_id=None,
                        alert_id=r.get("alert_id"),
                        audit_tail={"kind": "hydrate_expired", "ts": now},
                    )
                except Exception:
                    pass
                try:
                    db.watch_remove(r["ticker"])
                except Exception:
                    pass
                continue
            kept.append(QueuedAlert(
                ts=added_ts,
                score=int(r.get("score") or 0),
                ticker=r["ticker"],
                alert=r.get("alert") or {},
                decision_payload=r.get("decision") or {},
                alert_id=r.get("alert_id"),
            ))
        kept.sort(key=lambda q: q.score, reverse=True)
        self.state.queue = kept
        _log.info("hydrated %d watch rows from db", len(kept))

    async def enqueue(self, qa: QueuedAlert) -> None:
        br = db.t212_blacklist_get(qa.ticker) if qa.ticker else None
        if br:
            tag = ""
            try:
                tag = str(br["reason"]) if br else ""
            except Exception:
                pass
            _log.info("watch enqueue skipped %s — T212 blacklist (%s)", qa.ticker, tag)
            return
        async with self._lock:
            self.state.queue = [q for q in self.state.queue if q.ticker != qa.ticker]
            self.state.queue.append(qa)
            self.state.queue.sort(key=lambda q: q.score, reverse=True)
        try:
            db.watch_upsert(
                qa.ticker,
                score=qa.score,
                alert_id=qa.alert_id,
                decision=qa.decision_payload,
                alert=qa.alert,
            )
        except Exception:
            _log.exception("watch_upsert failed for %s", qa.ticker)

    async def pop_best(self) -> QueuedAlert | None:
        async with self._lock:
            now = time.time()
            expired = [q for q in self.state.queue if (now - q.ts) > config.QUEUE_TTL_SECONDS]
            self.state.queue = [
                q for q in self.state.queue if (now - q.ts) <= config.QUEUE_TTL_SECONDS
            ]
            qa = self.state.queue.pop(0) if self.state.queue else None
        for q in expired:
            try:
                db.watch_remove(q.ticker)
            except Exception:
                pass
        if qa:
            try:
                db.watch_remove(qa.ticker)
            except Exception:
                pass
        return qa

    async def remove_from_queue(self, ticker: str) -> None:
        """Drop a ticker from both the in-memory queue and the DB."""
        t = ticker.upper()
        async with self._lock:
            self.state.queue = [q for q in self.state.queue if q.ticker != t]
        try:
            db.watch_remove(t)
        except Exception:
            pass

    async def update_queue_score(
        self,
        ticker: str,
        *,
        score: int,
        decision: dict[str, Any],
    ) -> None:
        """Re-rank a watched ticker after a periodic re-evaluation."""
        t = ticker.upper()
        async with self._lock:
            for q in self.state.queue:
                if q.ticker == t:
                    q.score = int(score)
                    q.decision_payload = decision
                    break
            self.state.queue.sort(key=lambda q: q.score, reverse=True)
        try:
            db.watch_mark_reviewed(t, score=score, decision=decision)
        except Exception:
            _log.exception("watch_mark_reviewed failed for %s", t)

    # ── pause logic ────────────────────────────────────────────────────────
    def entries_paused(self) -> tuple[bool, str]:
        if self.negative_slot_count() >= 3:
            return True, "3+ slots negative"
        return False, ""

    async def update_slot_pnl(self, slot: Slot, price: float, unreal_pct: float, decision: str | None) -> None:
        async with self._lock:
            slot.last_price = price
            slot.unreal_pct = unreal_pct
            if decision:
                slot.last_decision = decision
