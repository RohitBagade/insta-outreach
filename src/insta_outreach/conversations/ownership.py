"""One conversation, one current owner.

The API lane, the browser lane and Rohit (typing in the Instagram app) all
share this state. Automation must hold a lease (atomic compare-and-set with
a fencing token) before touching a conversation, and re-validates it right
before any irreversible step. Any outbound message that the automation did
not send is treated as the human taking over: automation pauses for that
conversation and its open outbound actions are cancelled.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from insta_outreach.config import OwnershipSettings
from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    ConversationOwner,
    LeadStatus,
    MessageDirection,
    SenderKind,
)
from insta_outreach.domain.models import ThreadSnapshot, text_sha256
from insta_outreach.storage.models import Action, Conversation, Lead, Message
from insta_outreach.util.clock import Clock

_AUTOMATION_KINDS = (SenderKind.API_AGENT, SenderKind.BROWSER_AGENT)
_OPEN_OUTBOUND = (
    ActionStatus.PROPOSED,
    ActionStatus.DRAFTED,
    ActionStatus.PENDING_APPROVAL,
    ActionStatus.APPROVED,
    ActionStatus.NEEDS_HUMAN,
)
_OUTBOUND_TYPES = (ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY)


@dataclass
class ReconcileOutcome:
    new_inbound: list[Message] = field(default_factory=list)
    human_outbound: list[Message] = field(default_factory=list)
    matched: int = 0

    @property
    def human_took_over(self) -> bool:
        return bool(self.human_outbound)


class OwnershipService:
    def __init__(self, clock: Clock, settings: OwnershipSettings) -> None:
        self._clock = clock
        self._settings = settings

    # -- lookup ---------------------------------------------------------------
    def find(
        self,
        session: Session,
        account_id: str,
        *,
        peer_username: str | None = None,
        peer_igsid: str | None = None,
    ) -> Conversation | None:
        if peer_igsid:
            conv = session.scalars(
                select(Conversation).where(Conversation.account_id == account_id, Conversation.peer_igsid == peer_igsid)
            ).first()
            if conv is not None:
                return conv
        if peer_username:
            return session.scalars(
                select(Conversation).where(
                    Conversation.account_id == account_id, Conversation.peer_username == peer_username
                )
            ).first()
        return None

    def get_or_create(
        self,
        session: Session,
        account_id: str,
        *,
        peer_username: str | None = None,
        peer_igsid: str | None = None,
        lead_id: int | None = None,
    ) -> Conversation:
        by_name = self.find(session, account_id, peer_username=peer_username) if peer_username else None
        by_id = self.find(session, account_id, peer_igsid=peer_igsid) if peer_igsid else None
        conv: Conversation | None
        if by_name is not None and by_id is not None and by_name.id != by_id.id:
            conv = self._merge(session, keep=by_name, drop=by_id)
        else:
            conv = by_name or by_id
        if conv is None:
            conv = Conversation(
                account_id=account_id,
                peer_username=peer_username,
                peer_igsid=peer_igsid,
                lead_id=lead_id,
                owner=ConversationOwner.NONE,
                automation_paused=False,
                created_at=self._clock.now(),
            )
            session.add(conv)
            session.flush()
        if peer_username and not conv.peer_username:
            conv.peer_username = peer_username
        if peer_igsid and not conv.peer_igsid:
            conv.peer_igsid = peer_igsid
        if lead_id and not conv.lead_id:
            conv.lead_id = lead_id
        return conv

    def _merge(self, session: Session, keep: Conversation, drop: Conversation) -> Conversation:
        """Two rows for one person (username-keyed + IGSID-keyed): fold them."""
        session.execute(update(Message).where(Message.conversation_id == drop.id).values(conversation_id=keep.id))
        session.execute(update(Action).where(Action.conversation_id == drop.id).values(conversation_id=keep.id))
        keep.peer_igsid = keep.peer_igsid or drop.peer_igsid
        keep.api_thread_id = keep.api_thread_id or drop.api_thread_id
        keep.lead_id = keep.lead_id or drop.lead_id
        if drop.automation_paused and not keep.automation_paused:
            keep.owner = drop.owner
            keep.automation_paused = True
            keep.paused_reason = drop.paused_reason
            keep.paused_at = drop.paused_at
            keep.pause_until = drop.pause_until
        for attr in ("last_inbound_at", "last_outbound_at", "last_human_activity_at"):
            values = [v for v in (getattr(keep, attr), getattr(drop, attr)) if v is not None]
            setattr(keep, attr, max(values) if values else None)
        drop.peer_igsid = None  # free the unique key before deleting
        session.flush()
        session.delete(drop)
        session.flush()
        return keep

    # -- leases ----------------------------------------------------------------
    def release_expired_holds(self, session: Session) -> int:
        now = self._clock.now()
        result = session.execute(
            update(Conversation)
            .where(
                Conversation.owner == ConversationOwner.HUMAN,
                Conversation.pause_until.is_not(None),
                Conversation.pause_until < now,
            )
            .values(
                owner=ConversationOwner.NONE,
                automation_paused=False,
                paused_reason=None,
                pause_until=None,
                lock_token=None,
                lock_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def acquire(
        self,
        session: Session,
        conversation_id: int,
        owner: ConversationOwner,
        ttl_seconds: int | None = None,
    ) -> str | None:
        """Atomically take the conversation for an automation lane.

        Returns a fencing token, or None if the human owns it, automation is
        paused, or another lane holds an unexpired lease.
        """
        if owner is ConversationOwner.HUMAN or owner is ConversationOwner.NONE:
            raise ValueError("acquire() is for automation owners only")
        self.release_expired_holds(session)
        now = self._clock.now()
        token = uuid.uuid4().hex
        expires = now + timedelta(seconds=ttl_seconds or self._settings.lock_ttl_seconds)
        result = session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.automation_paused.is_(False),
                Conversation.owner != ConversationOwner.HUMAN,
                or_(
                    Conversation.owner == ConversationOwner.NONE,
                    Conversation.lock_expires_at.is_(None),
                    Conversation.lock_expires_at < now,
                ),
            )
            .values(owner=owner, lock_token=token, lock_expires_at=expires, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            return None
        session.expire_all()
        return token

    def transfer(self, session: Session, conversation_id: int, token: str, owner: ConversationOwner) -> str | None:
        """Hand a held lease to another automation lane (API -> browser fallback)."""
        now = self._clock.now()
        result = session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.lock_token == token,
                Conversation.automation_paused.is_(False),
            )
            .values(owner=owner, lock_expires_at=now + timedelta(seconds=self._settings.lock_ttl_seconds))
            .execution_options(synchronize_session=False)
        )
        session.expire_all()
        return token if result.rowcount else None  # type: ignore[attr-defined]

    def release(self, session: Session, conversation_id: int, token: str) -> bool:
        result = session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.lock_token == token)
            .values(owner=ConversationOwner.NONE, lock_token=None, lock_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        session.expire_all()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def still_owner(self, session: Session, conversation_id: int, token: str) -> bool:
        conv = session.get(Conversation, conversation_id, populate_existing=True)
        return bool(
            conv is not None
            and conv.lock_token == token
            and not conv.automation_paused
            and conv.owner in (ConversationOwner.API_AGENT, ConversationOwner.BROWSER_AGENT)
            and conv.lock_expires_at is not None
            and conv.lock_expires_at > self._clock.now()
        )

    # -- human ownership -------------------------------------------------------
    def take_over_by_human(self, session: Session, conv: Conversation, reason: str, at: datetime | None = None) -> int:
        """Hand the conversation to the human; returns cancelled action count."""
        now = self._clock.now()
        conv.owner = ConversationOwner.HUMAN
        conv.automation_paused = True
        conv.paused_reason = reason[:500]
        conv.paused_at = now
        conv.lock_token = None
        conv.lock_expires_at = None
        conv.last_human_activity_at = at or now
        hold = self._settings.human_hold_hours
        conv.pause_until = now + timedelta(hours=hold) if hold else None
        if conv.lead_id:
            lead = session.get(Lead, conv.lead_id)
            if lead is not None and lead.status not in (LeadStatus.CLOSED,):
                lead.status = LeadStatus.HANDED_OFF
                lead.status_reason = reason[:500]
        return self.cancel_open_outbound(session, conv, f"cancelled: {reason}")

    def release_to_automation(self, session: Session, conv: Conversation, by: str) -> None:
        conv.owner = ConversationOwner.NONE
        conv.automation_paused = False
        conv.paused_reason = f"released by {by}"
        conv.pause_until = None
        conv.lock_token = None
        conv.lock_expires_at = None

    @staticmethod
    def cancel_open_outbound(session: Session, conv: Conversation, reason: str) -> int:
        scope = Action.conversation_id == conv.id
        if conv.lead_id:
            scope = or_(scope, Action.lead_id == conv.lead_id)
        result = session.execute(
            update(Action)
            .where(scope, Action.type.in_(_OUTBOUND_TYPES), Action.status.in_(_OPEN_OUTBOUND))
            .values(status=ActionStatus.CANCELLED, status_reason=reason[:500])
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # -- message bookkeeping -----------------------------------------------------
    def record_automation_intent(
        self,
        session: Session,
        conv: Conversation,
        *,
        text: str,
        sender: SenderKind,
        action_id: str,
    ) -> Message:
        """Written BEFORE an automated send so echoes can be matched to us."""
        existing = session.scalars(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.action_id == action_id,
                Message.direction == MessageDirection.OUTBOUND,
            )
        ).first()
        if existing is not None:
            existing.delivery_state = "PENDING"
            existing.sender_kind = sender
            return existing
        msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.OUTBOUND,
            sender_kind=sender,
            text=text,
            text_hash=text_sha256(text),
            action_id=action_id,
            delivery_state="PENDING",
            observed_via="executor",
            handled=True,
            created_at=self._clock.now(),
        )
        session.add(msg)
        session.flush()
        return msg

    def automation_hashes(self, session: Session, conv: Conversation) -> list[str]:
        rows = session.scalars(
            select(Message.text_hash).where(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.sender_kind.in_(_AUTOMATION_KINDS),
                Message.delivery_state != "FAILED",
            )
        ).all()
        return list(rows)

    def match_automation_echo(
        self, session: Session, account_id: str, *, text: str, at: datetime
    ) -> Conversation | None:
        """Find the conversation of a recent automation send with this text.

        Echoes of our own browser-sent cold DMs arrive keyed only by the
        recipient's IGSID, which the User Profile API refuses to resolve
        until they reply. Matching the text against recent automation sends
        attributes the echo correctly (and teaches us the IGSID) instead of
        mistaking it for the human.
        """
        window_start = at - timedelta(seconds=self._settings.echo_match_window_seconds)
        rows = session.scalars(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.account_id == account_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.sender_kind.in_(_AUTOMATION_KINDS),
                Message.text_hash == text_sha256(text),
                Message.platform_message_id.is_(None),
                Message.delivery_state != "FAILED",
                Message.created_at >= window_start,
            )
        ).all()
        conversation_ids = {m.conversation_id for m in rows}
        if len(conversation_ids) != 1:
            return None  # none, or ambiguous: never guess
        return session.get(Conversation, conversation_ids.pop())

    def record_echo(
        self,
        session: Session,
        conv: Conversation,
        *,
        text: str,
        platform_message_id: str | None,
        at: datetime,
    ) -> SenderKind:
        """An outbound message observed via webhook echo: ours or the human's?"""
        if platform_message_id:
            known = session.scalars(select(Message).where(Message.platform_message_id == platform_message_id)).first()
            if known is not None:
                return known.sender_kind
        digest = text_sha256(text)
        window_start = at - timedelta(seconds=self._settings.echo_match_window_seconds)
        ours = session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.sender_kind.in_(_AUTOMATION_KINDS),
                Message.text_hash == digest,
                Message.platform_message_id.is_(None),
                Message.delivery_state != "FAILED",
                Message.created_at >= window_start,
            )
            .order_by(Message.created_at.desc())
        ).first()
        if ours is not None:
            ours.platform_message_id = platform_message_id
            ours.delivery_state = "SENT"
            ours.sent_at = ours.sent_at or at
            return ours.sender_kind
        session.add(
            Message(
                conversation_id=conv.id,
                direction=MessageDirection.OUTBOUND,
                sender_kind=SenderKind.HUMAN,
                text=text,
                text_hash=digest,
                platform_message_id=platform_message_id,
                delivery_state="SENT",
                observed_via="webhook_echo",
                sent_at=at,
                handled=True,
                created_at=self._clock.now(),
            )
        )
        conv.last_outbound_at = max(filter(None, [conv.last_outbound_at, at]))
        self.take_over_by_human(session, conv, "human replied from the Instagram app", at)
        return SenderKind.HUMAN

    def record_inbound(
        self,
        session: Session,
        conv: Conversation,
        *,
        text: str,
        platform_message_id: str | None,
        at: datetime,
        observed_via: str,
    ) -> Message | None:
        if (
            platform_message_id
            and session.scalars(select(Message.id).where(Message.platform_message_id == platform_message_id)).first()
        ):
            return None
        msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND,
            sender_kind=SenderKind.PROSPECT,
            text=text,
            text_hash=text_sha256(text),
            platform_message_id=platform_message_id,
            delivery_state="SENT",
            observed_via=observed_via,
            sent_at=at,
            handled=False,
            created_at=self._clock.now(),
        )
        session.add(msg)
        conv.last_inbound_at = max(filter(None, [conv.last_inbound_at, at]))
        return msg

    def reconcile(
        self, session: Session, conv: Conversation, snapshot: ThreadSnapshot, observed_via: str
    ) -> ReconcileOutcome:
        """Merge a thread observed by an executor into stored history.

        Messages without platform ids are matched by (direction, text) counts,
        so repeated identical texts are handled. Any unmatched outbound
        message was not sent by automation -> the human is active.
        """
        now = self._clock.now()
        outcome = ReconcileOutcome()
        stored = session.scalars(
            select(Message).where(Message.conversation_id == conv.id, Message.delivery_state != "FAILED")
        ).all()
        known_ids = {m.platform_message_id for m in stored if m.platform_message_id}
        remaining = Counter((m.direction, m.text_hash) for m in stored)
        for observed in snapshot.messages:
            if observed.platform_message_id and observed.platform_message_id in known_ids:
                outcome.matched += 1
                continue
            key = (observed.direction, text_sha256(observed.text))
            if remaining[key] > 0:
                remaining[key] -= 1
                outcome.matched += 1
                continue
            sent_at = observed.sent_at or now
            if observed.direction is MessageDirection.INBOUND:
                msg = Message(
                    conversation_id=conv.id,
                    direction=MessageDirection.INBOUND,
                    sender_kind=SenderKind.PROSPECT,
                    text=observed.text,
                    text_hash=key[1],
                    platform_message_id=observed.platform_message_id,
                    delivery_state="SENT",
                    observed_via=observed_via,
                    sent_at=sent_at,
                    handled=False,
                    created_at=now,
                )
                outcome.new_inbound.append(msg)
                conv.last_inbound_at = max(filter(None, [conv.last_inbound_at, sent_at]))
            else:
                msg = Message(
                    conversation_id=conv.id,
                    direction=MessageDirection.OUTBOUND,
                    sender_kind=SenderKind.HUMAN,
                    text=observed.text,
                    text_hash=key[1],
                    platform_message_id=observed.platform_message_id,
                    delivery_state="SENT",
                    observed_via=observed_via,
                    sent_at=sent_at,
                    handled=True,
                    created_at=now,
                )
                outcome.human_outbound.append(msg)
                conv.last_outbound_at = max(filter(None, [conv.last_outbound_at, sent_at]))
            session.add(msg)
        if snapshot.thread_id and not conv.browser_thread_id:
            conv.browser_thread_id = snapshot.thread_id
        conv.last_synced_at = now
        if outcome.human_took_over:
            self.take_over_by_human(session, conv, "outbound message in thread was not sent by automation", now)
        return outcome
