"""Rolling-window usage counters and in-operation pacing."""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import Channel
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import UsageEvent
from insta_outreach.util.clock import Clock

# Usage kinds
PAGE_VIEW = "page_view"  # one browser unit (navigation / scroll batch / dialog step)
API_CALL = "api_call"
SEND_OUTREACH = "send_outreach"
SEND_FOLLOWUP = "send_followup"
SEND_REPLY = "send_reply"
INSPECTION = "inspection"
DISCOVERY_RUN = "discovery_run"
SEND_KINDS = (SEND_OUTREACH, SEND_FOLLOWUP, SEND_REPLY)


class UsageLedger:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def record(
        self,
        session: Session,
        account_id: str,
        channel: Channel,
        kind: str,
        units: int = 1,
        action_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        session.add(
            UsageEvent(
                account_id=account_id,
                channel=channel,
                kind=kind,
                units=units,
                action_id=action_id,
                at=at or self._clock.now(),
            )
        )

    def total(
        self,
        session: Session,
        account_id: str,
        kinds: Iterable[str],
        since: datetime,
        channel: Channel | None = None,
    ) -> int:
        query = select(func.coalesce(func.sum(UsageEvent.units), 0)).where(
            UsageEvent.account_id == account_id,
            UsageEvent.kind.in_(list(kinds)),
            UsageEvent.at >= since,
        )
        if channel is not None:
            query = query.where(UsageEvent.channel == channel)
        return int(session.scalar(query) or 0)

    def oldest_since(
        self,
        session: Session,
        account_id: str,
        kinds: Iterable[str],
        since: datetime,
        channel: Channel | None = None,
    ) -> datetime | None:
        query = select(func.min(UsageEvent.at)).where(
            UsageEvent.account_id == account_id,
            UsageEvent.kind.in_(list(kinds)),
            UsageEvent.at >= since,
        )
        if channel is not None:
            query = query.where(UsageEvent.channel == channel)
        return session.scalar(query)

    def last_at(
        self,
        session: Session,
        account_id: str,
        kinds: Iterable[str],
        channel: Channel | None = None,
    ) -> datetime | None:
        query = select(func.max(UsageEvent.at)).where(
            UsageEvent.account_id == account_id, UsageEvent.kind.in_(list(kinds))
        )
        if channel is not None:
            query = query.where(UsageEvent.channel == channel)
        return session.scalar(query)


class BudgetExhausted(Exception):
    """The operation used every unit it was granted; return partial results."""


class Pacer:
    """Spaces browser units inside one operation and records them as usage.

    The *business* limits (how many units per hour, spacing) come from policy;
    the adapter only calls :meth:`unit` before each page-level step.
    """

    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        ledger: UsageLedger,
        account_id: str,
        channel: Channel,
        action_id: str | None,
        budget: int,
        min_interval: float,
        jitter: float,
        rng: random.Random,
        last_unit_at: datetime | None,
        kind: str = PAGE_VIEW,
    ) -> None:
        self._db = db
        self._clock = clock
        self._ledger = ledger
        self._account_id = account_id
        self._channel = channel
        self._action_id = action_id
        self.budget = budget
        self._min_interval = min_interval
        self._jitter = jitter
        self._rng = rng
        self.last_unit_at = last_unit_at
        self._kind = kind
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    async def unit(self) -> None:
        if self.used >= self.budget:
            raise BudgetExhausted(f"operation budget of {self.budget} units exhausted")
        if self.last_unit_at is not None:
            gap = self._min_interval + self._rng.uniform(0, self._jitter)
            wait = (self.last_unit_at - self._clock.now()).total_seconds() + gap
            await self._clock.sleep(max(0.0, wait))
        now = self._clock.now()
        with self._db.session() as session:
            self._ledger.record(session, self._account_id, self._channel, self._kind, action_id=self._action_id, at=now)
        self.last_unit_at = now
        self.used += 1
