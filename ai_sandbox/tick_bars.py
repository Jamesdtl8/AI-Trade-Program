"""Build synthetic bars from broker ticks (T212 poll)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TickBar:
    """One completed bucket: close = last tick, avg = mean of ticks in window."""

    period_sec: float
    bucket_start: float
    ts: float
    close: float
    avg: float
    tick_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period_sec,
            "bucket_start": self.bucket_start,
            "ts": self.ts,
            "close": self.close,
            "avg": self.avg,
            "c": self.close,
            "n": self.tick_count,
            "source": "t212_ticks",
        }


@dataclass
class TickBarBuilder:
    """Accumulate ticks; emit a bar when the aligned bucket rolls forward."""

    period_sec: float
    _bucket_start: int | None = field(default=None, repr=False)
    _prices: list[float] = field(default_factory=list, repr=False)

    def _align(self, ts: float) -> int:
        p = max(1, int(self.period_sec))
        return int(float(ts) // p) * p

    def add(self, price: float, ts: float | None = None) -> TickBar | None:
        import time

        px = float(price)
        if px <= 0:
            return None
        now = float(ts if ts is not None else time.time())
        bucket = self._align(now)
        if self._bucket_start is None:
            self._bucket_start = bucket
            self._prices = [px]
            return None
        if bucket > self._bucket_start:
            completed = self._finalize()
            self._bucket_start = bucket
            self._prices = [px]
            return completed
        self._prices.append(px)
        return None

    def _finalize(self) -> TickBar:
        assert self._bucket_start is not None
        prices = self._prices or [0.0]
        p = float(self.period_sec)
        return TickBar(
            period_sec=p,
            bucket_start=float(self._bucket_start),
            ts=float(self._bucket_start) + p,
            close=float(prices[-1]),
            avg=sum(prices) / len(prices),
            tick_count=len(prices),
        )

    def flush(self) -> TickBar | None:
        """Force-close the open bucket (e.g. end of day)."""
        if not self._prices or self._bucket_start is None:
            return None
        bar = self._finalize()
        self._bucket_start = None
        self._prices = []
        return bar
