"""Instagram Executor: picks API adapter or browser agent per capability.

Routing is data (``DEFAULT_ROUTES``), not code paths. For each request the
executor walks the ordered channels, skipping adapters that cannot serve it
and lanes that are closed. Falling back to the next channel is only allowed
when the previous attempt *definitely did not* perform the operation — for
sends that means "certainly not sent", so an ambiguous API timeout can never
be followed by a browser re-send.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from insta_outreach.config import LimitsSettings
from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus
from insta_outreach.domain.models import ExecutionResult, OperationRequest
from insta_outreach.execution.base import Guard, InstagramAdapter, OperationContext
from insta_outreach.policy.lanes import LaneService
from insta_outreach.policy.usage import API_CALL, PAGE_VIEW, Pacer, UsageLedger
from insta_outreach.storage.db import Database
from insta_outreach.util.clock import Clock

log = logging.getLogger(__name__)

DEFAULT_ROUTES: dict[Capability, tuple[Channel, ...]] = {
    # No official API exists for these (research: docs/RESEARCH.md).
    Capability.SEARCH_ACCOUNTS: (Channel.BROWSER,),
    Capability.HASHTAG_POSTS: (Channel.BROWSER,),  # API hashtag search hides the poster
    Capability.LOCATION_POSTS: (Channel.BROWSER,),
    Capability.LIST_FOLLOWERS: (Channel.BROWSER,),
    Capability.LIST_FOLLOWING: (Channel.BROWSER,),
    Capability.SUGGESTED_ACCOUNTS: (Channel.BROWSER,),
    Capability.POST_ENGAGERS: (Channel.BROWSER,),
    Capability.SEND_NEW_DM: (Channel.BROWSER,),  # API cannot start conversations
    # API preferred where it exists.
    Capability.INSPECT_PROFILE: (Channel.API, Channel.BROWSER),  # Business Discovery (FB login)
    Capability.READ_THREAD: (Channel.API, Channel.BROWSER),
    Capability.READ_INBOX: (Channel.API, Channel.BROWSER),
    Capability.SEND_DM_REPLY: (Channel.API, Channel.BROWSER),  # API inside the 24h window
    Capability.PRIVATE_REPLY: (Channel.API,),
    Capability.REPLY_COMMENT: (Channel.API,),
}

# Reads may fall back on anything except barriers (which halt the lane anyway).
_READ_NO_FALLBACK = frozenset({ExecutionStatus.SUCCESS, ExecutionStatus.OWNERSHIP_CONFLICT})
# Writes may only fall back when the first channel certainly did not send.
_WRITE_FALLBACK = frozenset({ExecutionStatus.CAPABILITY_UNAVAILABLE, ExecutionStatus.NOT_PERMITTED})

ChannelHook = Callable[[Channel], Awaitable[Guard | None]]


class InstagramExecutor:
    def __init__(
        self,
        *,
        adapters: dict[Channel, InstagramAdapter],
        lanes: LaneService,
        db: Database,
        clock: Clock,
        ledger: UsageLedger,
        limits: Callable[[], LimitsSettings],
        evidence_dir: Path,
        routes: dict[Capability, tuple[Channel, ...]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.adapters = adapters
        self._lanes = lanes
        self._db = db
        self._clock = clock
        self._ledger = ledger
        self._limits = limits
        self._evidence_dir = evidence_dir
        self._routes = routes or DEFAULT_ROUTES
        self._rng = rng or random.Random()
        initial = limits()
        self._semaphores = {
            Channel.BROWSER: asyncio.Semaphore(max(1, initial.max_concurrent_browser_sessions)),
            Channel.API: asyncio.Semaphore(max(1, initial.max_concurrent_api_calls)),
        }
        self._last_unit: dict[tuple[str, Channel], datetime | None] = {}

    # -- routing --------------------------------------------------------------
    def candidate_channels(self, capability: Capability) -> list[Channel]:
        return [
            channel
            for channel in self._routes.get(capability, ())
            if channel in self.adapters and capability in self.adapters[channel].capabilities()
        ]

    def plan(self, request: OperationRequest) -> list[Channel]:
        return [c for c in self.candidate_channels(request.capability) if self.adapters[c].supports(request)]

    @staticmethod
    def may_fall_back(capability: Capability, result: ExecutionResult) -> bool:
        if result.status.is_barrier:
            return False  # barrier: stop, record, surface to a human
        if capability.is_outbound:
            return result.status in _WRITE_FALLBACK
        return result.status not in _READ_NO_FALLBACK

    # -- execution ---------------------------------------------------------------
    def _pacer(self, request: OperationRequest, channel: Channel, budget: int) -> Pacer:
        limits = self._limits()
        key = (request.account_id, channel)
        if key not in self._last_unit:
            with self._db.session() as session:
                kind = PAGE_VIEW if channel is Channel.BROWSER else API_CALL
                self._last_unit[key] = self._ledger.last_at(session, request.account_id, [kind], channel)
        if channel is Channel.BROWSER:
            interval, jitter, kind = (
                limits.min_seconds_between_browser_units,
                limits.browser_unit_jitter_seconds,
                PAGE_VIEW,
            )
        else:
            interval, jitter, kind = 0.2, 0.0, API_CALL
        return Pacer(
            db=self._db,
            clock=self._clock,
            ledger=self._ledger,
            account_id=request.account_id,
            channel=channel,
            action_id=request.action_id,
            budget=budget,
            min_interval=interval,
            jitter=jitter,
            rng=self._rng,
            last_unit_at=self._last_unit[key],
            kind=kind,
        )

    async def execute(
        self,
        request: OperationRequest,
        *,
        on_channel: ChannelHook | None = None,
        budgets: dict[Channel, int] | None = None,
    ) -> ExecutionResult:
        """Run the request on the best available channel.

        ``on_channel`` is called before each attempt (the worker acquires or
        transfers the conversation lease there) and returns the guard to use,
        or None if the conversation cannot be taken.
        """
        reasons: list[str] = []
        last: ExecutionResult | None = None
        channels = self.candidate_channels(request.capability)
        for index, channel in enumerate(channels):
            adapter = self.adapters[channel]
            if not adapter.supports(request):
                reasons.append(f"{channel.value}: cannot serve this request")
                continue
            with self._db.session() as session:
                lane = self._lanes.snapshot(session, request.account_id, channel)
            if not lane.is_open(self._clock.now()):
                reasons.append(f"{channel.value}: lane {lane.state.value} ({lane.reason})")
                continue
            guard: Guard | None = None
            if on_channel is not None:
                guard = await on_channel(channel)
                if guard is None:
                    return ExecutionResult(
                        status=ExecutionStatus.OWNERSHIP_CONFLICT,
                        capability=request.capability,
                        channel=channel,
                        code="lease_unavailable",
                        detail="conversation is owned by the human or another lane",
                        started_at=self._clock.now(),
                        finished_at=self._clock.now(),
                    )
            budget = min(request.max_units, (budgets or {}).get(channel, request.max_units))
            pacer = self._pacer(request, channel, max(1, budget))
            ctx = OperationContext(
                clock=self._clock, pacer=pacer, evidence_dir=self._evidence_dir, started_at=self._clock.now()
            )
            if guard is not None:
                ctx.guard = guard
            async with self._semaphores[channel]:
                try:
                    outcome = await adapter.execute(request, ctx)
                except Exception as exc:  # adapters should not raise; never let one kill the worker
                    log.exception("adapter %s crashed on %s", channel.value, request.capability.value)
                    outcome = ExecutionResult(
                        status=ExecutionStatus.RETRYABLE_FAILURE,
                        capability=request.capability,
                        channel=channel,
                        code="adapter_exception",
                        detail=f"{type(exc).__name__}: {exc}"[:500],
                        started_at=ctx.started_at,
                        finished_at=self._clock.now(),
                    )
            self._last_unit[(request.account_id, channel)] = pacer.last_unit_at
            outcome.units_used = pacer.used
            last = outcome
            has_next = index < len(channels) - 1
            if not (has_next and self.may_fall_back(request.capability, outcome)):
                return outcome
            reasons.append(f"{channel.value}: {outcome.status.value} {outcome.code or ''}".strip())
            log.info("falling back from %s after %s", channel.value, outcome.status.value)
        if last is not None:
            last.detail = f"{last.detail} | route: {'; '.join(reasons)}".strip(" |")
            return last
        return ExecutionResult(
            status=ExecutionStatus.CAPABILITY_UNAVAILABLE,
            capability=request.capability,
            channel=channels[0] if channels else Channel.BROWSER,
            code="no_channel",
            detail="; ".join(reasons) or "no adapter registered for this capability",
            started_at=self._clock.now(),
            finished_at=self._clock.now(),
        )

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()
