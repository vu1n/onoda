"""Injectable clocks. Timestamps are metadata; causal order comes from event seq."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> str: ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class LogicalClock:
    """Deterministic clock for fixtures: one second per tick from a fixed epoch."""

    def __init__(self, start: str = "2026-01-01T00:00:00Z"):
        self._t = datetime.fromisoformat(start.replace("Z", "+00:00"))

    def now(self) -> str:
        self._t += timedelta(seconds=1)
        return self._t.isoformat().replace("+00:00", "Z")
