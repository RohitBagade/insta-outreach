"""The research lane: a browser adapter logged in as a separate Instagram account.

It may only search and read public profiles. Outbound capabilities and the
brand account's own inbox are refused structurally, so a misrouted request can
never send from, or read the chats of, the research account. With a research
adapter configured, research work is routed *only* to it: when its lane stops
(e.g. a checkpoint), finding leads pauses instead of falling back to the brand
account.
"""

from __future__ import annotations

from typing import Any

from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus
from insta_outreach.domain.models import ExecutionResult, OperationRequest
from insta_outreach.execution.base import InstagramAdapter, OperationContext, result

RESEARCH_CAPABILITIES = frozenset(
    {
        Capability.SEARCH_ACCOUNTS,
        Capability.HASHTAG_POSTS,
        Capability.LOCATION_POSTS,
        Capability.LIST_FOLLOWERS,
        Capability.LIST_FOLLOWING,
        Capability.SUGGESTED_ACCOUNTS,
        Capability.POST_ENGAGERS,
        Capability.INSPECT_PROFILE,
    }
)


class ResearchAdapter:
    """Wraps a browser adapter (real or simulated) as the read-only RESEARCH lane."""

    channel = Channel.RESEARCH

    def __init__(self, inner: InstagramAdapter) -> None:
        inner.channel = Channel.RESEARCH  # every result, lane transition and incident names this lane
        self._inner = inner
        self.simulated = inner.simulated

    def capabilities(self) -> frozenset[Capability]:
        return self._inner.capabilities() & RESEARCH_CAPABILITIES

    def supports(self, request: OperationRequest) -> bool:
        return request.capability in RESEARCH_CAPABILITIES and self._inner.supports(request)

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        if request.capability not in RESEARCH_CAPABILITIES:
            return result(
                request,
                Channel.RESEARCH,
                ExecutionStatus.NOT_PERMITTED,
                ctx=ctx,
                code="research_is_read_only",
                detail="the research account only searches and reads public profiles",
            )
        return await self._inner.execute(request, ctx)

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:  # e.g. the browser session, for diagnostics
        return getattr(self._inner, name)


def research_routes(
    routes: dict[Capability, tuple[Channel, ...]],
) -> dict[Capability, tuple[Channel, ...]]:
    """Move research capabilities from the brand account's browser to the research lane."""
    return {
        capability: tuple(
            Channel.RESEARCH if channel is Channel.BROWSER and capability in RESEARCH_CAPABILITIES else channel
            for channel in channels
        )
        for capability, channels in routes.items()
    }
