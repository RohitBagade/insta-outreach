"""Executor contracts shared by the API adapter, the browser agent and the simulator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus
from insta_outreach.domain.models import Evidence, ExecutionResult, OperationRequest
from insta_outreach.policy.usage import Pacer
from insta_outreach.util.clock import Clock, utc

Guard = Callable[[], Awaitable[bool]]

# Instagram allows one private reply per comment, within 7 days of the comment.
PRIVATE_REPLY_MAX_AGE = timedelta(days=7)
_PRIVATE_REPLY_MARGIN = timedelta(hours=6)


def private_reply_open(params: dict[str, Any], now: datetime) -> bool:
    """Whether a private reply to ``params['comment_id']`` is still allowed."""
    if not params.get("comment_id"):
        return False
    raw = params.get("comment_at")
    if not raw:
        return True  # age unknown: let the platform decide
    try:
        commented_at = utc(datetime.fromisoformat(str(raw)))
    except ValueError:
        return False
    return commented_at is not None and now - commented_at < PRIVATE_REPLY_MAX_AGE - _PRIVATE_REPLY_MARGIN


async def _always_true() -> bool:
    return True


@dataclass
class OperationContext:
    """What an adapter gets besides the request. No business logic in here:
    pacing numbers and the ownership guard are decided by the orchestrator."""

    clock: Clock
    pacer: Pacer | None = None
    # Re-validates the conversation lease; adapters MUST await it immediately
    # before any irreversible step (pressing send).
    guard: Guard = _always_true
    evidence_dir: Path = Path("data/evidence")
    started_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


class InstagramAdapter(Protocol):
    channel: Channel
    simulated: bool

    def capabilities(self) -> frozenset[Capability]:
        """Capabilities this adapter can perform at all (static)."""
        ...

    def supports(self, request: OperationRequest) -> bool:
        """Whether it can perform this particular request now (contextual)."""
        ...

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult: ...

    async def close(self) -> None: ...


def result(
    request: OperationRequest,
    channel: Channel,
    status: ExecutionStatus,
    *,
    ctx: OperationContext | None = None,
    detail: str = "",
    code: str | None = None,
    data: dict[str, Any] | None = None,
    evidence: list[Evidence] | None = None,
    page_url: str | None = None,
    retry_after: float | None = None,
    confirmed: bool = False,
    simulated: bool = False,
) -> ExecutionResult:
    """Convenience constructor so every adapter fills results the same way."""
    now = ctx.clock.now() if ctx else None
    return ExecutionResult(
        status=status,
        capability=request.capability,
        channel=channel,
        code=code,
        detail=detail,
        data=data or {},
        evidence=evidence or [],
        page_url=page_url,
        retry_after_seconds=retry_after,
        confirmed=confirmed,
        simulated=simulated,
        units_used=ctx.pacer.used if ctx and ctx.pacer else 0,
        started_at=ctx.started_at if ctx else None,
        finished_at=now,
    )
