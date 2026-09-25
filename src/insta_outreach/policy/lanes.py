"""Execution lanes and stop-on-warning.

A lane is (account, channel). Barriers (login/checkpoint/restriction/human
action) HALT a lane until a human resumes it; rate limits put it in a
time-boxed COOLDOWN (escalating to HALT when they repeat); repeated UI drift
halts it for review. Nothing here ever tries to get *past* a barrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from insta_outreach.config import LimitsSettings
from insta_outreach.domain.enums import (
    ActionStatus,
    Channel,
    ExecutionStatus,
    IncidentSeverity,
    LaneState,
)
from insta_outreach.domain.models import ExecutionResult
from insta_outreach.policy.incidents import IncidentService
from insta_outreach.storage.models import Action, Lane
from insta_outreach.util.clock import Clock, utc


@dataclass(frozen=True)
class LaneSnapshot:
    account_id: str
    channel: Channel
    state: LaneState
    until: datetime | None
    reason: str | None

    def is_open(self, now: datetime) -> bool:
        if self.state is LaneState.ACTIVE:
            return True
        if self.state is LaneState.COOLDOWN:
            return self.until is not None and self.until <= now
        return False

    @property
    def halted(self) -> bool:
        return self.state is LaneState.HALTED


@dataclass(frozen=True)
class LaneTransition:
    account_id: str
    channel: Channel
    new_state: LaneState
    reason: str
    incident_id: int | None


_HALT_MESSAGES = {
    ExecutionStatus.LOGIN_REQUIRED: "Instagram session is logged out / token invalid",
    ExecutionStatus.CHECKPOINT_REQUIRED: "Instagram security checkpoint requires the account owner",
    ExecutionStatus.ACCOUNT_RESTRICTED: "Instagram reports the account is restricted or blocked",
    ExecutionStatus.HUMAN_ACTION_REQUIRED: "Instagram is asking for something only a human should do",
}


class LaneService:
    def __init__(self, clock: Clock, incidents: IncidentService) -> None:
        self._clock = clock
        self._incidents = incidents

    def _row(self, session: Session, account_id: str, channel: Channel) -> Lane:
        lane = session.scalars(select(Lane).where(Lane.account_id == account_id, Lane.channel == channel)).first()
        if lane is None:
            lane = Lane(
                account_id=account_id,
                channel=channel,
                state=LaneState.ACTIVE,
                rate_limit_events=[],
                consecutive_ui_changed=0,
                updated_at=self._clock.now(),
            )
            session.add(lane)
            session.flush()
        return lane

    def snapshot(self, session: Session, account_id: str, channel: Channel) -> LaneSnapshot:
        lane = self._row(session, account_id, channel)
        now = self._clock.now()
        if lane.state is LaneState.COOLDOWN and lane.until is not None and lane.until <= now:
            lane.state = LaneState.ACTIVE
            lane.until = None
            lane.reason = None
        return LaneSnapshot(account_id, channel, lane.state, lane.until, lane.reason)

    def all_snapshots(self, session: Session, account_id: str) -> list[LaneSnapshot]:
        return [self.snapshot(session, account_id, channel) for channel in Channel]

    # -- result handling -----------------------------------------------------
    def record_result(
        self,
        session: Session,
        account_id: str,
        result: ExecutionResult,
        limits: LimitsSettings,
        action_id: str | None = None,
    ) -> LaneTransition | None:
        lane = self._row(session, account_id, result.channel)
        now = self._clock.now()
        status = result.status

        if status is ExecutionStatus.SUCCESS:
            lane.consecutive_ui_changed = 0
            lane.last_success_at = now
            return None

        if status is ExecutionStatus.ACCOUNT_RESTRICTED:
            # An account-level restriction stops every lane of the account.
            transition = None
            for channel in Channel:
                transition = (
                    self._halt(
                        session,
                        self._row(session, account_id, channel),
                        result,
                        _HALT_MESSAGES[status],
                        IncidentSeverity.CRITICAL,
                        action_id,
                    )
                    or transition
                )
            return transition

        if status.is_barrier:
            return self._halt(session, lane, result, _HALT_MESSAGES[status], IncidentSeverity.CRITICAL, action_id)

        if status is ExecutionStatus.RATE_LIMITED:
            window_start = now - timedelta(hours=24)
            events = [
                e for e in (lane.rate_limit_events or []) if (utc(datetime.fromisoformat(e)) or now) >= window_start
            ]
            events.append(now.isoformat())
            lane.rate_limit_events = events
            if len(events) >= limits.rate_limit_events_before_halt:
                return self._halt(
                    session,
                    lane,
                    result,
                    f"Rate limited {len(events)}x within 24h — stopping instead of pushing on",
                    IncidentSeverity.CRITICAL,
                    action_id,
                )
            cooldown = max(
                timedelta(minutes=limits.rate_limit_cooldown_minutes),
                timedelta(seconds=result.retry_after_seconds or 0),
            )
            lane.state = LaneState.COOLDOWN
            lane.until = now + cooldown
            lane.reason = f"rate limited: {result.code or result.detail}"[:500]
            incident = self._incidents.open(
                session,
                account_id=account_id,
                channel=result.channel,
                severity=IncidentSeverity.WARNING,
                kind=ExecutionStatus.RATE_LIMITED.value,
                title=f"{result.channel.value} lane cooling down until {lane.until:%Y-%m-%d %H:%M} UTC",
                detail=result.detail,
                action_id=action_id,
                page_url=result.page_url,
                evidence=[e.model_dump(mode="json") for e in result.evidence],
            )
            lane.incident_id = incident.id
            return LaneTransition(account_id, result.channel, LaneState.COOLDOWN, lane.reason, incident.id)

        # A profile without a message option is normal once; on several profiles in a
        # row it more likely means the UI changed. Fail loud rather than silently
        # marking every lead unreachable.
        drift_like = status is ExecutionStatus.NOT_PERMITTED and result.code == "no_message_button"
        if status is ExecutionStatus.UI_CHANGED or drift_like:
            lane.consecutive_ui_changed = (lane.consecutive_ui_changed or 0) + 1
            threshold = limits.ui_changed_events_before_halt + (1 if drift_like else 0)
            if lane.consecutive_ui_changed >= threshold:
                return self._halt(
                    session,
                    lane,
                    result,
                    f"Instagram UI no longer matches expectations ({result.code or 'unknown'})",
                    IncidentSeverity.WARNING,
                    action_id,
                )
        return None

    def _halt(
        self,
        session: Session,
        lane: Lane,
        result: ExecutionResult,
        title: str,
        severity: IncidentSeverity,
        action_id: str | None,
    ) -> LaneTransition | None:
        incident = self._incidents.open(
            session,
            account_id=lane.account_id,
            channel=lane.channel,
            severity=severity,
            kind=result.status.value,
            title=f"{lane.channel.value} lane halted: {title}",
            detail=f"{result.code or ''} {result.detail}".strip(),
            action_id=action_id,
            page_url=result.page_url,
            evidence=[e.model_dump(mode="json") for e in result.evidence],
        )
        already = lane.state is LaneState.HALTED
        lane.state = LaneState.HALTED
        lane.until = None
        lane.reason = f"{result.status.value}: {result.code or result.detail}"[:500]
        lane.incident_id = incident.id
        if already:
            return None
        return LaneTransition(lane.account_id, lane.channel, LaneState.HALTED, lane.reason, incident.id)

    # -- human controls --------------------------------------------------------
    def halt_manually(self, session: Session, account_id: str, channel: Channel, reason: str) -> None:
        lane = self._row(session, account_id, channel)
        lane.state = LaneState.HALTED
        lane.until = None
        lane.reason = f"manual: {reason}"[:500]

    def resume(self, session: Session, account_id: str, channel: Channel, by: str, note: str = "") -> int:
        """Reopen a lane after a human dealt with the barrier.

        Returns how many parked actions were released back to APPROVED. They
        re-run through the full gate, and sends re-check the thread first, so
        an action interrupted mid-send can never be sent twice.
        """
        lane = self._row(session, account_id, channel)
        lane.state = LaneState.ACTIVE
        lane.until = None
        lane.reason = None
        lane.rate_limit_events = []
        lane.consecutive_ui_changed = 0
        lane.incident_id = None
        self._incidents.resolve_for_lane(session, account_id, channel, by, note or "lane resumed")
        released = session.execute(
            update(Action)
            .where(
                Action.account_id == account_id,
                Action.status == ActionStatus.NEEDS_HUMAN,
                (Action.executed_channel == channel) | (Action.executed_channel.is_(None)),
            )
            .values(status=ActionStatus.APPROVED, not_before=None, status_reason=f"released after lane resume by {by}")
            .execution_options(synchronize_session=False)
        )
        return int(released.rowcount or 0)  # type: ignore[attr-defined]
