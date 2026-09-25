"""Action queue: idempotent creation, atomic claims, attempts, recovery."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    OperatingMode,
)
from insta_outreach.domain.models import ExecutionResult, text_sha256
from insta_outreach.storage.models import Action, ActionAttempt
from insta_outreach.util.clock import Clock

PRIORITY = {
    ActionType.SEND_REPLY: 10,
    ActionType.SEND_OUTREACH: 30,
    ActionType.SEND_FOLLOW_UP: 35,
    ActionType.SYNC_THREAD: 40,
    ActionType.SYNC_INBOX: 45,
    ActionType.INSPECT_PROFILE: 50,
    ActionType.DISCOVER: 70,
}


def new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:24]}"


class ActionService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def get_by_key(self, session: Session, key: str) -> Action | None:
        return session.scalars(select(Action).where(Action.idempotency_key == key)).first()

    def create(
        self,
        session: Session,
        *,
        key: str,
        account_id: str,
        type: ActionType,
        capability: Capability,
        status: ActionStatus,
        mode: OperatingMode,
        lead_id: int | None = None,
        conversation_id: int | None = None,
        target_username: str | None = None,
        campaign_id: str | None = None,
        params: dict[str, Any] | None = None,
        message_text: str | None = None,
        message_kind: str | None = None,
        followup_number: int = 0,
        facts_used: list[str] | None = None,
        composer: str | None = None,
        approved_by: str | None = None,
        gate: dict[str, Any] | None = None,
        max_attempts: int = 3,
        not_before: datetime | None = None,
        expires_at: datetime | None = None,
        status_reason: str | None = None,
    ) -> tuple[Action, bool]:
        """Create unless an action with this idempotency key already exists."""
        existing = self.get_by_key(session, key)
        if existing is not None:
            return existing, False
        now = self._clock.now()
        action = Action(
            id=new_action_id(),
            idempotency_key=key,
            account_id=account_id,
            type=type,
            capability=capability,
            status=status,
            status_reason=status_reason,
            mode_at_creation=mode,
            lead_id=lead_id,
            conversation_id=conversation_id,
            target_username=target_username,
            campaign_id=campaign_id,
            params=params or {},
            message_text=message_text,
            message_sha256=text_sha256(message_text) if message_text else None,
            message_kind=message_kind,
            followup_number=followup_number,
            facts_used=facts_used or [],
            composer=composer,
            approved_by=approved_by,
            approved_at=now if approved_by else None,
            gate=gate or {},
            priority=PRIORITY.get(type, 50),
            max_attempts=max_attempts,
            not_before=not_before,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        try:
            with session.begin_nested():
                session.add(action)
        except IntegrityError:  # concurrent creator won the race
            existing = self.get_by_key(session, key)
            if existing is None:
                raise
            return existing, False
        return action, True

    def claim(self, session: Session, action_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = self._clock.now()
        claimed = session.execute(
            update(Action)
            .where(Action.id == action_id, Action.status == ActionStatus.APPROVED)
            .values(
                status=ActionStatus.EXECUTING,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return bool(claimed.rowcount)  # type: ignore[attr-defined]

    def record_attempt(self, session: Session, action: Action, outcome: ExecutionResult) -> ActionAttempt:
        started = outcome.started_at or self._clock.now()
        attempt = ActionAttempt(
            action_id=action.id,
            attempt_no=action.attempts,
            account_id=action.account_id,
            target_username=action.target_username,
            channel=outcome.channel,
            status=outcome.status,
            code=outcome.code,
            detail=outcome.detail[:4000] if outcome.detail else None,
            message_text=action.message_text,
            page_url=outcome.page_url,
            confirmed=outcome.confirmed,
            simulated=outcome.simulated,
            units_used=outcome.units_used,
            evidence=[e.model_dump(mode="json") for e in outcome.evidence],
            data=_attempt_data(outcome.data),
            started_at=started,
            finished_at=outcome.finished_at or self._clock.now(),
        )
        session.add(attempt)
        return attempt

    def recover_stale(self, session: Session) -> int:
        """Leases that expired mid-execution (crash). Re-queue: sends re-check
        the thread before sending, so a message that did go out is detected
        as an idempotent replay instead of being sent twice."""
        now = self._clock.now()
        result = session.execute(
            update(Action)
            .where(Action.status == ActionStatus.EXECUTING, Action.lease_expires_at < now)
            .values(
                status=ActionStatus.APPROVED,
                lease_owner=None,
                lease_expires_at=None,
                status_reason="recovered after an interrupted execution; will verify before resending",
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def expire_stale_approvals(self, session: Session, ttl_hours: int) -> int:
        cutoff = self._clock.now() - timedelta(hours=ttl_hours)
        result = session.execute(
            update(Action)
            .where(Action.status.in_([ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED]), Action.created_at < cutoff)
            .values(status=ActionStatus.EXPIRED, status_reason=f"not approved within {ttl_hours}h; facts may be stale")
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _attempt_data(data: dict[str, Any]) -> dict[str, Any]:
    """Keep attempt payloads small: drop bulky lists, keep references."""
    slim: dict[str, Any] = {}
    for key, value in data.items():
        if key == "candidates":
            slim["candidate_count"] = len(value)
        elif key == "thread":
            slim["thread_id"] = (value or {}).get("thread_id")
            slim["thread_message_count"] = len((value or {}).get("messages", []))
        elif key == "profile":
            slim["profile_username"] = (value or {}).get("username")
        else:
            slim[key] = value
    return slim
