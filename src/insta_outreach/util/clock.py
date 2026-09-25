"""Injectable clock so caps, cooldowns and schedules are testable."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock: ``sleep`` advances time instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 21, 5, 0, tzinfo=UTC)  # 10:30 IST, a Monday
        self.slept: float = 0.0

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self._now += timedelta(seconds=seconds, **kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)
            self.slept += seconds
        await asyncio.sleep(0)


def utc(dt: datetime | None) -> datetime | None:
    """Coerce to aware UTC (naive values are assumed to already be UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def local_day_start(now: datetime, tz: str) -> datetime:
    """Start of the calendar day in ``tz`` that contains ``now``, as UTC."""
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def in_window(now: datetime, tz: str, window: tuple[str, str]) -> bool:
    start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    current = now.astimezone(ZoneInfo(tz)).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # window crosses midnight


def next_window_start(now: datetime, tz: str, window: tuple[str, str]) -> datetime:
    """Earliest instant >= now that falls inside ``window`` (UTC)."""
    if in_window(now, tz, window):
        return now
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    start = parse_hhmm(window[0])
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
