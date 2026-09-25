"""Execution worker: the only place where approved actions meet the executor.

Per action: execution-time gate -> atomic claim -> conversation lease (for
sends) -> intent record -> executor -> structured result -> state updates.
The executor never decides what happens next; this module maps each
ExecutionStatus to retries, parking, cancellation, lead updates and lanes.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from insta_outreach.config import LimitsSettings
from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    Channel,
    ConversationOwner,
    ExecutionStatus,
    GateOutcome,
    LeadStatus,
    OperatingMode,
    SenderKind,
)
from insta_outreach.domain.models import (
    ApprovedMessage,
    ConversationRef,
    ExecutionResult,
    OperationRequest,
    ThreadSnapshot,
)
from insta_outreach.execution.base import Guard
from insta_outreach.orchestrator.pipeline import Pipeline, Services
from insta_outreach.policy import usage as u
from insta_outreach.policy.gate import GateDecision
from insta_outreach.policy.lanes import LaneService
from insta_outreach.storage.models import Action, Conversation, Lead, Message, UsageEvent

log = logging.getLogger(__name__)

_NOT_SENT = frozenset(
    {
        ExecutionStatus.ALREADY_CONTACTED,
        ExecutionStatus.OWNERSHIP_CONFLICT,
        ExecutionStatus.TARGET_NOT_FOUND,
        ExecutionStatus.NOT_PERMITTED,
        ExecutionStatus.PERMANENT_FAILURE,
        ExecutionStatus.CAPABILITY_UNAVAILABLE,
        ExecutionStatus.UI_CHANGED,  # adapters only report UI_CHANGED for failures *before* pressing send
    }
)
_SEND_USAGE = {
    ActionType.SEND_OUTREACH: u.SEND_OUTREACH,
    ActionType.SEND_FOLLOW_UP: u.SEND_FOLLOWUP,
    ActionType.SEND_REPLY: u.SEND_REPLY,
}
LEASE_SECONDS = 900
# A private reply that cannot be made now never becomes possible later (one
# per comment, 7-day window); the lead falls back to a normal first DM.
_PRIVATE_REPLY_FALLBACK = frozenset(
    {
        ExecutionStatus.NOT_PERMITTED,
        ExecutionStatus.CAPABILITY_UNAVAILABLE,
        ExecutionStatus.TARGET_NOT_FOUND,
        ExecutionStatus.PERMANENT_FAILURE,
    }
)


@dataclass
class Prepared:
    action_id: str
    type: ActionType
    request: OperationRequest
    conversation_id: int | None
    message_text: str | None
    budgets: dict[Channel, int] = field(default_factory=dict)


class ExecutionWorker:
    def __init__(
        self,
        services: Services,
        pipeline: Pipeline,
        ledger: u.UsageLedger,
        lanes: LaneService,
        worker_id: str | None = None,
    ) -> None:
        self.s = services
        self.pipeline = pipeline
        self._ledger = ledger
        self._lanes = lanes
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run_once(self, mode: OperatingMode, max_actions: int) -> list[ExecutionResult]:
        paused = self.s.runtime.paused()
        limits = self.s.runtime.limits()
        now = self.s.clock.now()
        with self.s.db.session() as session:
            candidate_ids = list(
                session.scalars(
                    select(Action.id)
                    .where(
                        Action.status == ActionStatus.APPROVED,
                        (Action.not_before.is_(None)) | (Action.not_before <= now),
                    )
                    .order_by(Action.priority, Action.created_at)
                    .limit(max(1, max_actions) * 5)
                )
            )
        results: list[ExecutionResult] = []
        for action_id in candidate_ids:
            if len(results) >= max_actions:
                break
            prepared = self._prepare(action_id, mode, paused, limits)
            if prepared is not None:
                results.append(await self._execute(prepared))
        return results

    # ---------------------------------------------------------------- prepare
    def _prepare(self, action_id: str, mode: OperatingMode, paused: bool, limits: LimitsSettings) -> Prepared | None:
        now = self.s.clock.now()
        with self.s.db.session() as session:
            action = session.get(Action, action_id)
            if action is None or action.status is not ActionStatus.APPROVED:
                return None
            if action.expires_at is not None and action.expires_at < now:
                action.status, action.status_reason = ActionStatus.EXPIRED, "expired before execution"
                return None
            channels = self.s.executor.candidate_channels(action.capability)
            decision = self.s.gate.check(session, action, channels, mode, paused, limits)
            action.gate = dict(action.gate or {}) | {"execution": decision.as_dict()}
            if decision.outcome is GateOutcome.DENY:
                action.status, action.status_reason = ActionStatus.CANCELLED, "; ".join(decision.reasons)[:1000]
                self._lead_on_denied(session, action, decision)
                return None
            if decision.outcome is GateOutcome.DEFER:
                if decision.requires_approval:
                    action.status, action.approved_by, action.approved_at = ActionStatus.PENDING_APPROVAL, None, None
                elif decision.not_before is not None:
                    action.not_before = decision.not_before
                action.status_reason = "; ".join(decision.reasons)[:1000]
                return None
            browser_left = limits.browser_units_per_hour - self._ledger.total(
                session, action.account_id, [u.PAGE_VIEW], now - timedelta(hours=1), Channel.BROWSER
            )
            budgets = {
                Channel.BROWSER: max(1, min(limits.max_units_per_operation, browser_left)),
                Channel.API: limits.max_units_per_operation,
            }
            request = self._build_request(session, action, limits)
            prepared = Prepared(action.id, action.type, request, action.conversation_id, action.message_text, budgets)
            if not self.s.actions.claim(session, action.id, self.worker_id, LEASE_SECONDS):
                return None
            return prepared

    def _build_request(self, session: Session, action: Action, limits: LimitsSettings) -> OperationRequest:
        params = dict(action.params or {}) | {"attempt": action.attempts + 1}
        intent = session.scalars(select(Message.id).where(Message.action_id == action.id)).first()
        if action.type.is_outbound and intent is not None:
            # An earlier execution got as far as recording its intent (e.g. crashed
            # mid-send): the adapter must check the thread before sending again.
            params["verify_before_send"] = True
        conversation = session.get(Conversation, action.conversation_id) if action.conversation_id else None
        conv_ref = None
        if conversation is not None:
            conv_ref = ConversationRef(
                conversation_id=conversation.id,
                peer_username=conversation.peer_username or action.target_username or "",
                peer_igsid=conversation.peer_igsid,
                api_thread_id=conversation.api_thread_id,
                browser_thread_id=conversation.browser_thread_id,
                last_inbound_at=conversation.last_inbound_at,
            )
        message = None
        if action.type.is_outbound and action.message_text:
            message = ApprovedMessage(
                action_id=action.id,
                text=action.message_text,
                sha256=action.message_sha256 or "",
                kind=action.message_kind or "initial",  # type: ignore[arg-type]
                followup_number=action.followup_number,
                known_outbound_hashes=self.s.ownership.automation_hashes(session, conversation) if conversation else [],
            )
        return OperationRequest(
            action_id=action.id,
            account_id=action.account_id,
            capability=action.capability,
            target_username=action.target_username,
            params=params,
            message=message,
            conversation=conv_ref,
            max_units=int(params.get("max_units", limits.max_units_per_operation)),
        )

    # ---------------------------------------------------------------- execute
    async def _execute(self, prepared: Prepared) -> ExecutionResult:
        lease: dict[str, str | None] = {"token": None}
        conv_id = prepared.conversation_id
        needs_lease = prepared.type.is_outbound and conv_id is not None

        async def on_channel(channel: Channel) -> Guard | None:
            assert conv_id is not None
            owner = ConversationOwner.for_channel(channel)
            with self.s.db.session() as session:
                token = lease["token"]
                token = (
                    self.s.ownership.transfer(session, conv_id, token, owner)
                    if token
                    else self.s.ownership.acquire(session, conv_id, owner)
                )
                if token is None:
                    return None
                lease["token"] = token
                conversation = session.get(Conversation, conv_id)
                assert conversation is not None
                # Written BEFORE sending so the webhook echo is attributed to us.
                self.s.ownership.record_automation_intent(
                    session,
                    conversation,
                    text=prepared.message_text or "",
                    sender=SenderKind(owner.value),
                    action_id=prepared.action_id,
                )

            async def guard() -> bool:
                with self.s.db.session() as session:
                    return self.s.ownership.still_owner(session, conv_id, token)

            return guard

        try:
            outcome = await self.s.executor.execute(
                prepared.request, on_channel=on_channel if needs_lease else None, budgets=prepared.budgets
            )
        finally:
            if lease["token"] and conv_id is not None:
                with self.s.db.session() as session:
                    self.s.ownership.release(session, conv_id, lease["token"])
        await self._record(prepared, outcome)
        return outcome

    # ----------------------------------------------------------------- record
    def _next_state(
        self, action: Action, outcome: ExecutionResult, limits: LimitsSettings
    ) -> tuple[ActionStatus, datetime | None, bool]:
        """(new status, not_before, final?)"""
        now = self.s.clock.now()
        status = outcome.status
        if status is ExecutionStatus.SUCCESS:
            return ActionStatus.SUCCEEDED, None, True
        if status.is_barrier:
            return ActionStatus.NEEDS_HUMAN, None, False
        if status is ExecutionStatus.RATE_LIMITED:
            action.attempts -= 1  # being throttled is not the action's fault
            wait = max(outcome.retry_after_seconds or 0, 3600)
            return ActionStatus.APPROVED, now + timedelta(seconds=wait), False
        if status is ExecutionStatus.CAPABILITY_UNAVAILABLE and action.capability is Capability.PRIVATE_REPLY:
            return ActionStatus.FAILED, None, True
        if status in (
            ExecutionStatus.RETRYABLE_FAILURE,
            ExecutionStatus.UI_CHANGED,
            ExecutionStatus.CAPABILITY_UNAVAILABLE,
        ):
            if action.attempts < action.max_attempts:
                backoff = limits.retry_base_seconds * (2 ** max(0, action.attempts - 1))
                return ActionStatus.APPROVED, now + timedelta(seconds=backoff), False
            return ActionStatus.FAILED, None, True
        if status in (ExecutionStatus.ALREADY_CONTACTED, ExecutionStatus.OWNERSHIP_CONFLICT):
            return ActionStatus.CANCELLED, None, True
        return ActionStatus.FAILED, None, True

    async def _record(self, prepared: Prepared, outcome: ExecutionResult) -> None:
        limits = self.s.runtime.limits()
        now = self.s.clock.now()
        lead_to_analyze: int | None = None
        with self.s.db.session() as session:
            action = session.get(Action, prepared.action_id)
            if action is None:
                return
            action.attempts += 1
            self.s.actions.record_attempt(session, action, outcome)
            action.executed_channel = outcome.channel
            action.last_result_status, action.last_result_code = outcome.status, outcome.code
            action.last_result_detail = (outcome.detail or "")[:2000]
            action.result_data = {
                k: v for k, v in outcome.data.items() if k not in ("candidates", "profile", "thread", "inbox")
            }
            action.lease_owner = action.lease_expires_at = None
            transition = self._lanes.record_result(session, action.account_id, outcome, limits, action.id)
            if transition is not None:
                log.warning(
                    "lane %s/%s -> %s: %s",
                    transition.account_id,
                    transition.channel.value,
                    transition.new_state.value,
                    transition.reason,
                )
            new_status, not_before, final = self._next_state(action, outcome, limits)
            action.status, action.not_before = new_status, not_before
            action.status_reason = f"{outcome.status.value}: {outcome.code or ''} {outcome.detail or ''}".strip()[:1000]
            if final:
                action.completed_at = now

            conversation = session.get(Conversation, action.conversation_id) if action.conversation_id else None
            thread = outcome.data.get("thread")
            if conversation is not None and thread:
                self.pipeline.reconcile_thread(
                    session,
                    conversation,
                    ThreadSnapshot.model_validate(thread),
                    via=f"{outcome.channel.value.lower()}_read",
                )
            if outcome.ok and conversation is not None and outcome.data.get("api_thread_id"):
                conversation.api_thread_id = outcome.data["api_thread_id"]

            if action.type is ActionType.DISCOVER:
                if outcome.ok:
                    self._ledger.record(
                        session, action.account_id, outcome.channel, u.DISCOVERY_RUN, action_id=action.id
                    )
                self.pipeline.handle_discovery_result(session, action, outcome, final)
            elif action.type is ActionType.INSPECT_PROFILE:
                if outcome.ok:
                    self._ledger.record(session, action.account_id, outcome.channel, u.INSPECTION, action_id=action.id)
                lead_to_analyze = self.pipeline.handle_inspection_result(session, action, outcome, final)
            elif action.type is ActionType.SYNC_INBOX:
                self.pipeline.handle_inbox_result(session, action, outcome)
            elif action.type.is_outbound:
                self._record_send(session, action, conversation, outcome, final)
        await self.s.incidents.flush()
        if lead_to_analyze is not None:
            await self.pipeline.analyze_lead(lead_to_analyze)

    def _record_send(
        self, session: Session, action: Action, conversation: Conversation | None, outcome: ExecutionResult, final: bool
    ) -> None:
        now = self.s.clock.now()
        intent = session.scalars(select(Message).where(Message.action_id == action.id)).first()
        lead = session.get(Lead, action.lead_id) if action.lead_id else None
        if outcome.ok:
            if intent is not None:
                intent.delivery_state, intent.sent_at = "SENT", intent.sent_at or now
                mid = outcome.data.get("message_id")
                if (
                    mid
                    and not intent.platform_message_id
                    and not session.scalars(select(Message.id).where(Message.platform_message_id == mid)).first()
                ):
                    intent.platform_message_id = mid
            recipient = outcome.data.get("recipient_id")
            if conversation is not None and recipient and not conversation.peer_igsid:
                # e.g. a private reply: the API reveals the IGSID that later webhooks use.
                conversation = self.s.ownership.get_or_create(
                    session,
                    action.account_id,
                    peer_username=conversation.peer_username,
                    peer_igsid=str(recipient),
                    lead_id=conversation.lead_id,
                )
            if conversation is not None:
                conversation.last_outbound_at = now
                thread_id = outcome.data.get("thread_id")
                if thread_id and outcome.channel is Channel.BROWSER:
                    conversation.browser_thread_id = thread_id
            if not self._send_counted(session, action):
                # Counted exactly once per action, including a send first made by an
                # attempt that crashed before recording and confirmed on replay.
                self._ledger.record(
                    session, action.account_id, outcome.channel, _SEND_USAGE[action.type], action_id=action.id
                )
            if lead is not None:
                lead.last_outbound_at = now
                if recipient and not lead.igsid:
                    lead.igsid = str(recipient)
                if action.type is ActionType.SEND_OUTREACH:
                    lead.status, lead.contacted_at = LeadStatus.CONTACTED, lead.contacted_at or now
                    lead.status_reason = f"outreach sent via {outcome.channel.value}"
                    if action.capability is Capability.PRIVATE_REPLY:
                        lead.status_reason = "private reply to their comment sent via API"
                        # The platform allows further messages only once they respond.
                        lead.followups_blocked_reason = "first contact was a private reply; waiting for them to respond"
                elif action.type is ActionType.SEND_FOLLOW_UP:
                    lead.followups_sent = max(lead.followups_sent, action.followup_number)
            return
        if intent is not None and outcome.status in _NOT_SENT:
            intent.delivery_state = "FAILED"
        if lead is None or not final:
            return
        if (
            action.type is ActionType.SEND_OUTREACH
            and action.capability is Capability.PRIVATE_REPLY
            and outcome.status in _PRIVATE_REPLY_FALLBACK
        ):
            if lead.status is LeadStatus.OUTREACH_PENDING:
                lead.status = LeadStatus.QUALIFIED
                lead.status_reason = f"private reply not possible ({outcome.code}); a normal DM will be prepared"
            return
        if outcome.status is ExecutionStatus.TARGET_NOT_FOUND:
            lead.status, lead.status_reason = LeadStatus.UNREACHABLE, f"not found when sending ({outcome.code})"
        elif outcome.status is ExecutionStatus.NOT_PERMITTED:
            if outcome.code == "message_request_pending":
                lead.followups_blocked_reason = "Instagram allows no further messages until the request is accepted"
            elif action.type is ActionType.SEND_OUTREACH:
                lead.status, lead.status_reason = LeadStatus.UNREACHABLE, f"cannot be messaged ({outcome.code})"
        elif outcome.status is ExecutionStatus.ALREADY_CONTACTED:
            if lead.status is not LeadStatus.HANDED_OFF:
                lead.status, lead.status_reason = LeadStatus.CONTACTED, "existing conversation found; nothing sent"
                lead.contacted_at = lead.contacted_at or now
        elif outcome.status is ExecutionStatus.OWNERSHIP_CONFLICT:
            if lead.status not in (LeadStatus.HANDED_OFF, LeadStatus.CLOSED):
                lead.status, lead.status_reason = LeadStatus.HANDED_OFF, "the human owns this conversation"
        elif action.type is ActionType.SEND_OUTREACH:
            lead.status, lead.status_reason = LeadStatus.ANALYZED, f"outreach failed: {outcome.status.value}"

    @staticmethod
    def _send_counted(session: Session, action: Action) -> bool:
        return bool(
            session.scalars(
                select(UsageEvent.id).where(UsageEvent.action_id == action.id, UsageEvent.kind.in_(u.SEND_KINDS))
            ).first()
        )

    @staticmethod
    def _lead_on_denied(session: Session, action: Action, decision: GateDecision) -> None:
        if action.type is not ActionType.SEND_OUTREACH or action.lead_id is None:
            return
        lead = session.get(Lead, action.lead_id)
        if lead is None or lead.status is not LeadStatus.OUTREACH_PENDING:
            return
        Pipeline._lead_after_block(lead, decision.reasons)


def summarize(results: list[ExecutionResult]) -> list[dict[str, Any]]:
    return [
        {"capability": r.capability.value, "channel": r.channel.value, "status": r.status.value, "code": r.code}
        for r in results
    ]
