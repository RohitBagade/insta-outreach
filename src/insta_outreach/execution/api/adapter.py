"""Tier 1 adapter: official Instagram Platform API operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from insta_outreach.config import ApiSettings
from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus, MessageDirection
from insta_outreach.domain.models import (
    ExecutionResult,
    InboxEntry,
    OperationRequest,
    PostObservation,
    ProfileObservation,
    ThreadMessage,
    ThreadSnapshot,
    text_sha256,
)
from insta_outreach.execution.api.client import (
    GraphApiClient,
    GraphApiError,
    GraphTransportError,
    map_error,
)
from insta_outreach.execution.base import OperationContext, private_reply_open, result
from insta_outreach.policy.usage import BudgetExhausted
from insta_outreach.util.clock import Clock

# Stay safely inside the 24h standard messaging window.
REPLY_WINDOW = timedelta(hours=23, minutes=30)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))


class GraphApiAdapter:
    channel = Channel.API
    simulated = False

    def __init__(self, client: GraphApiClient, settings: ApiSettings, clock: Clock) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def capabilities(self) -> frozenset[Capability]:
        caps = {
            Capability.SEND_DM_REPLY,
            Capability.PRIVATE_REPLY,
            Capability.REPLY_COMMENT,
            Capability.READ_THREAD,
            Capability.READ_INBOX,
        }
        if self._settings.business_discovery_available:
            caps.add(Capability.INSPECT_PROFILE)
        return frozenset(caps)

    def supports(self, request: OperationRequest) -> bool:
        cap = request.capability
        if cap not in self.capabilities():
            return False
        conv = request.conversation
        if cap is Capability.SEND_DM_REPLY:
            return bool(
                conv
                and conv.peer_igsid
                and conv.last_inbound_at
                and self._clock.now() - conv.last_inbound_at < REPLY_WINDOW
            )
        if cap is Capability.READ_THREAD:
            return bool(conv and (conv.api_thread_id or conv.peer_igsid))
        if cap is Capability.INSPECT_PROFILE:
            return bool(request.target_username)
        if cap is Capability.PRIVATE_REPLY:
            return private_reply_open(request.params, self._clock.now())
        if cap is Capability.REPLY_COMMENT:
            return bool(request.params.get("comment_id"))
        return True

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        try:
            handler = {
                Capability.INSPECT_PROFILE: self._inspect,
                Capability.SEND_DM_REPLY: self._send_reply,
                Capability.PRIVATE_REPLY: self._private_reply,
                Capability.REPLY_COMMENT: self._reply_comment,
                Capability.READ_THREAD: self._read_thread,
                Capability.READ_INBOX: self._read_inbox,
            }[request.capability]
        except KeyError:
            return result(
                request, self.channel, ExecutionStatus.CAPABILITY_UNAVAILABLE, ctx=ctx, code="unsupported_capability"
            )
        try:
            return await handler(request, ctx)
        except BudgetExhausted as exc:
            return result(
                request,
                self.channel,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx=ctx,
                code="budget_exhausted",
                detail=str(exc),
            )
        except GraphApiError as exc:
            status, code = map_error(exc)
            return result(
                request,
                self.channel,
                status,
                ctx=ctx,
                code=code,
                detail=str(exc)[:500],
                retry_after=exc.retry_after_seconds,
                data={"fbtrace_id": exc.fbtrace_id, "graph_code": exc.code, "graph_subcode": exc.subcode},
            )
        except GraphTransportError as exc:
            # For sends the outcome is unknown: RETRYABLE, and the retry path
            # re-reads the thread before sending again (idempotency).
            return result(
                request,
                self.channel,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx=ctx,
                code="transport_error",
                detail=str(exc)[:500],
            )

    async def _unit(self, ctx: OperationContext) -> None:
        if ctx.pacer is not None:
            await ctx.pacer.unit()

    # -- operations -------------------------------------------------------------------
    async def _inspect(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        await self._unit(ctx)
        username = request.target_username or ""
        data = await self._client.business_discovery(username)
        if not data:
            return result(
                request, self.channel, ExecutionStatus.TARGET_NOT_FOUND, ctx=ctx, code="not_found_or_not_professional"
            )
        media = (data.get("media") or {}).get("data", [])
        obs = ProfileObservation(
            username=(data.get("username") or username).lower(),
            ig_user_id=data.get("id"),
            full_name=data.get("name"),
            biography=data.get("biography"),
            is_business=True,  # Business Discovery only returns professional accounts
            is_private=False,
            website=data.get("website"),
            followers=data.get("followers_count"),
            following=data.get("follows_count"),
            posts_count=data.get("media_count"),
            recent_posts=[
                PostObservation(
                    url=m.get("permalink"),
                    posted_at=_parse_time(m.get("timestamp")),
                    caption=m.get("caption"),
                    media_type=m.get("media_product_type") or m.get("media_type"),
                    like_count=m.get("like_count"),
                    comment_count=m.get("comments_count"),
                )
                for m in media
            ],
            observed_via=self.channel,
            observed_at=self._clock.now(),
        )
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            data={"profile": obs.model_dump(mode="json")},
            confirmed=True,
        )

    async def _thread_messages(self, igsid: str, thread_id: str | None) -> tuple[str | None, list[ThreadMessage]]:
        if not thread_id:
            conversations = await self._client.list_conversations(user_igsid=igsid, limit=1)
            thread_id = conversations[0]["id"] if conversations else None
        if not thread_id:
            return None, []
        raw = await self._client.get_conversation_messages(thread_id)
        messages = []
        for item in reversed(raw):  # API returns newest first
            sender = (item.get("from") or {}).get("id")
            messages.append(
                ThreadMessage(
                    direction=MessageDirection.OUTBOUND
                    if sender == self._client.ig_user_id
                    else MessageDirection.INBOUND,
                    text=item.get("message") or "",
                    sent_at=_parse_time(item.get("created_time")),
                    platform_message_id=item.get("id"),
                )
            )
        return thread_id, messages

    async def _send_reply(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        message, conv = request.message, request.conversation
        if message is None or not message.is_intact() or conv is None or not conv.peer_igsid:
            return result(
                request,
                self.channel,
                ExecutionStatus.PERMANENT_FAILURE,
                ctx=ctx,
                code="invalid_request",
                detail="missing/tampered approved message or recipient",
            )
        delivered = await self._delivered_on_earlier_attempt(request, ctx)
        if delivered is not None:
            return delivered
        if not await ctx.guard():
            return result(
                request, self.channel, ExecutionStatus.OWNERSHIP_CONFLICT, ctx=ctx, code="lease_lost_before_send"
            )
        await self._unit(ctx)
        response = await self._client.send_text(conv.peer_igsid, message.text)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"message_id": response.get("message_id"), "recipient_id": response.get("recipient_id")},
        )

    async def _delivered_on_earlier_attempt(
        self, request: OperationRequest, ctx: OperationContext
    ) -> ExecutionResult | None:
        """Retry after an ambiguous failure: was the message delivered after all?"""
        message, conv = request.message, request.conversation
        repeat = request.params.get("attempt", 1) > 1 or request.params.get("verify_before_send")
        if not repeat or message is None or conv is None or not conv.peer_igsid:
            return None
        await self._unit(ctx)
        thread_id, messages = await self._thread_messages(conv.peer_igsid, conv.api_thread_id)
        if any(m.direction is MessageDirection.OUTBOUND and text_sha256(m.text) == message.sha256 for m in messages):
            return result(
                request,
                self.channel,
                ExecutionStatus.SUCCESS,
                ctx=ctx,
                confirmed=True,
                code="already_delivered",
                data={"thread_id": thread_id},
            )
        return None

    async def _private_reply(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        message = request.message
        if message is None or not message.is_intact():
            return result(request, self.channel, ExecutionStatus.PERMANENT_FAILURE, ctx=ctx, code="invalid_request")
        # A second private reply to the same comment is refused by the platform,
        # so an unknown first outcome must be resolved by reading the thread.
        delivered = await self._delivered_on_earlier_attempt(request, ctx)
        if delivered is not None:
            return delivered
        if not await ctx.guard():
            return result(
                request, self.channel, ExecutionStatus.OWNERSHIP_CONFLICT, ctx=ctx, code="lease_lost_before_send"
            )
        await self._unit(ctx)
        response = await self._client.send_private_reply(str(request.params["comment_id"]), message.text)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"message_id": response.get("message_id"), "recipient_id": response.get("recipient_id")},
        )

    async def _reply_comment(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        message = request.message
        if message is None or not message.is_intact():
            return result(request, self.channel, ExecutionStatus.PERMANENT_FAILURE, ctx=ctx, code="invalid_request")
        await self._unit(ctx)
        response = await self._client.reply_to_comment(str(request.params["comment_id"]), message.text)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"comment_reply_id": response.get("id")},
        )

    async def _read_thread(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        conv = request.conversation
        if conv is None or not (conv.peer_igsid or conv.api_thread_id):
            return result(
                request,
                self.channel,
                ExecutionStatus.PERMANENT_FAILURE,
                ctx=ctx,
                code="invalid_request",
                detail="no API conversation reference",
            )
        await self._unit(ctx)
        thread_id, messages = await self._thread_messages(conv.peer_igsid or "", conv.api_thread_id)
        snapshot = ThreadSnapshot(
            peer_username=conv.peer_username or "", thread_id=thread_id, exists=bool(messages), messages=messages
        )
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"thread": snapshot.model_dump(mode="json"), "api_thread_id": thread_id},
        )

    async def _read_inbox(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        await self._unit(ctx)
        conversations = await self._client.list_conversations(limit=int(request.params.get("limit", 20)))
        entries: list[dict[str, Any]] = []
        for conv in conversations:
            participants = (conv.get("participants") or {}).get("data", [])
            peer = next((p for p in participants if p.get("id") != self._client.ig_user_id), None)
            if peer and peer.get("username"):
                updated = _parse_time(conv.get("updated_time"))
                entries.append(
                    InboxEntry(peer_username=peer["username"].lower(), thread_id=conv.get("id")).model_dump(mode="json")
                    | {"peer_igsid": peer.get("id"), "updated_time": updated.isoformat() if updated else None}
                )
        return result(request, self.channel, ExecutionStatus.SUCCESS, ctx=ctx, confirmed=True, data={"inbox": entries})

    async def close(self) -> None:
        await self._client.close()
