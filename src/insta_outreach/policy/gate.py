"""Policy / rate / duplicate gate.

``evaluate`` is a pure function over :class:`GateFacts` so every rule is unit
testable. :class:`EligibilityGate` assembles the facts from the database.

The gate runs twice for outbound actions: when the action is proposed (so
junk never reaches the approval queue) and again immediately before
execution (caps, ownership, lanes and mode may have changed since).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from insta_outreach.config import LimitsSettings, ScheduleSettings, Settings
from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Channel,
    ConversationOwner,
    GateOutcome,
    LeadStatus,
    MessageDirection,
    OperatingMode,
)
from insta_outreach.policy import usage as u
from insta_outreach.policy.lanes import LaneService, LaneSnapshot
from insta_outreach.policy.suppression import suppression_reason
from insta_outreach.storage.models import Action, Conversation, Lead, Message
from insta_outreach.util.clock import Clock, in_window, local_day_start, next_window_start

AUTO_APPROVER = "auto"

_CONTACTED_STATES = (LeadStatus.CONTACTED, LeadStatus.REPLIED, LeadStatus.HANDED_OFF, LeadStatus.CLOSED)


@dataclass
class GateFacts:
    now: datetime
    mode: OperatingMode
    paused: bool
    limits: LimitsSettings
    schedule: ScheduleSettings
    action_type: ActionType
    approved_by: str | None = None
    # lead
    has_lead: bool = False
    lead_status: LeadStatus | None = None
    lead_score: int | None = None
    min_score: int = 60
    require_business: bool = True
    business_like: bool | None = None
    suppressed: str | None = None
    contacted_before: bool = False
    replied: bool = False
    followups_sent: int = 0
    followups_blocked: str | None = None
    followup_number: int = 0
    last_outbound_at: datetime | None = None
    # rollout sandbox (rollout.allowed_targets)
    target_allowed: bool = True
    # conversation
    conversation_paused: bool = False
    conversation_owner: ConversationOwner = ConversationOwner.NONE
    # lanes able to serve the capability (router order)
    lanes: list[LaneSnapshot] = field(default_factory=list)
    # counters
    outreach_today: int = 0
    outreach_last_hour: int = 0
    oldest_outreach_in_hour: datetime | None = None
    followups_today: int = 0
    replies_last_hour: int = 0
    oldest_reply_in_hour: datetime | None = None
    last_send_at: datetime | None = None
    browser_units_last_hour: int = 0
    oldest_browser_unit_in_hour: datetime | None = None
    inspections_today: int = 0
    discovery_runs_today: int = 0
    send_jitter_seconds: float = 0.0


@dataclass
class GateDecision:
    outcome: GateOutcome
    reasons: list[str] = field(default_factory=list)
    not_before: datetime | None = None
    lane_parked: bool = False  # waits for a human to reopen a lane / unpause
    requires_approval: bool = False  # auto-approved but mode now demands a human

    @property
    def allowed(self) -> bool:
        return self.outcome is GateOutcome.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reasons": self.reasons,
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "lane_parked": self.lane_parked,
            "requires_approval": self.requires_approval,
        }


def _deny(*reasons: str) -> GateDecision:
    return GateDecision(GateOutcome.DENY, list(reasons))


def evaluate(f: GateFacts) -> GateDecision:
    if f.paused:
        return GateDecision(GateOutcome.DEFER, ["global pause is on"], lane_parked=True)

    limits, tz = f.limits, f.schedule.timezone
    defer: list[tuple[datetime, str]] = []
    next_day = local_day_start(f.now, tz) + timedelta(days=1)

    if f.action_type.is_outbound:
        # -- autonomy mode -------------------------------------------------
        if not f.mode.may_send:
            return GateDecision(GateOutcome.DEFER, [f"mode {f.mode.value} never sends"], lane_parked=True)
        if f.approved_by is None:
            return GateDecision(GateOutcome.DEFER, ["awaiting approval"], lane_parked=True)
        if f.mode is OperatingMode.APPROVAL and f.approved_by == AUTO_APPROVER:
            return GateDecision(GateOutcome.DEFER, ["mode APPROVAL requires a human approval"], requires_approval=True)

        # -- permanent denials ---------------------------------------------
        if not f.target_allowed:
            return _deny("sandbox: target is not listed in rollout.allowed_targets")
        if f.suppressed:
            return _deny(f.suppressed)
        if f.conversation_paused or f.conversation_owner is ConversationOwner.HUMAN:
            return _deny("conversation is owned by the human; automation paused")
        if f.action_type is ActionType.SEND_OUTREACH:
            if not f.has_lead:
                return _deny("outreach requires a lead")
            if f.contacted_before:
                return _deny("repeated-contact prevention: lead was already contacted")
            if f.lead_status not in (LeadStatus.QUALIFIED, LeadStatus.OUTREACH_PENDING):
                return _deny(f"lead status is {f.lead_status}")
            if f.lead_score is None or f.lead_score < f.min_score:
                return _deny(f"score {f.lead_score} below minimum {f.min_score}")
            if f.require_business and f.business_like is False:
                return _deny("not a business profile")
        elif f.action_type is ActionType.SEND_FOLLOW_UP:
            if f.replied:
                return _deny("lead replied; no automated follow-up")
            if f.followups_blocked:
                return _deny(f"follow-ups blocked: {f.followups_blocked}")
            if f.lead_status is not LeadStatus.CONTACTED:
                return _deny(f"follow-up requires CONTACTED lead, got {f.lead_status}")
            if f.followups_sent >= limits.max_followups_per_lead:
                return _deny(f"follow-up limit reached ({limits.max_followups_per_lead})")
            if f.followup_number != f.followups_sent + 1:
                return _deny(f"follow-up #{f.followup_number} out of sequence")
            days = limits.followup_after_days
            gap_days = days[min(f.followup_number - 1, len(days) - 1)] if days else 3
            if f.last_outbound_at is None:
                return _deny("no previous outbound message to follow up on")
            due = f.last_outbound_at + timedelta(days=gap_days)
            if f.now < due:
                defer.append((due, f"follow-up not due until {due.isoformat()}"))

        # -- temporary deferrals ---------------------------------------------
        if not in_window(f.now, tz, f.schedule.send_hours):
            defer.append((next_window_start(f.now, tz, f.schedule.send_hours), "outside send hours"))
        if f.action_type is ActionType.SEND_OUTREACH:
            if f.outreach_today >= limits.outreach_per_day:
                defer.append((next_day, f"daily outreach cap {limits.outreach_per_day} reached"))
            if f.outreach_last_hour >= limits.outreach_per_hour:
                base = f.oldest_outreach_in_hour or f.now
                defer.append((base + timedelta(hours=1), f"hourly outreach cap {limits.outreach_per_hour} reached"))
        elif f.action_type is ActionType.SEND_FOLLOW_UP:
            if f.followups_today >= limits.followups_per_day:
                defer.append((next_day, f"daily follow-up cap {limits.followups_per_day} reached"))
        elif f.action_type is ActionType.SEND_REPLY and f.replies_last_hour >= limits.replies_per_hour:
            base = f.oldest_reply_in_hour or f.now
            defer.append((base + timedelta(hours=1), f"hourly reply cap {limits.replies_per_hour} reached"))
        if f.last_send_at is not None:
            earliest = f.last_send_at + timedelta(seconds=limits.min_seconds_between_sends + f.send_jitter_seconds)
            if f.now < earliest:
                defer.append((earliest, "pacing: minimum spacing between sends"))
    else:
        if f.action_type is ActionType.INSPECT_PROFILE and f.inspections_today >= limits.profile_inspections_per_day:
            defer.append((next_day, "daily profile-inspection cap reached"))
        if f.action_type is ActionType.DISCOVER and f.discovery_runs_today >= limits.discovery_runs_per_day:
            defer.append((next_day, "daily discovery-run cap reached"))

    # -- lanes --------------------------------------------------------------
    open_lanes = [lane for lane in f.lanes if lane.is_open(f.now)]
    if not f.lanes:
        return _deny("no execution lane can serve this capability")
    if not open_lanes:
        cooldowns = [lane.until for lane in f.lanes if not lane.halted and lane.until is not None]
        if cooldowns:
            defer.append((min(cooldowns), "all lanes cooling down"))
        else:
            reasons = [f"lane {lane.channel.value} halted: {lane.reason}" for lane in f.lanes]
            return GateDecision(GateOutcome.DEFER, reasons, lane_parked=True)
    elif all(lane.channel is Channel.BROWSER for lane in open_lanes):
        if not in_window(f.now, tz, f.schedule.browser_active_hours):
            defer.append(
                (next_window_start(f.now, tz, f.schedule.browser_active_hours), "outside browser active hours")
            )
        if f.browser_units_last_hour >= limits.browser_units_per_hour:
            base = f.oldest_browser_unit_in_hour or f.now
            defer.append((base + timedelta(hours=1), "hourly browser action cap reached"))

    if defer:
        until = max(when for when, _ in defer)
        return GateDecision(GateOutcome.DEFER, [reason for _, reason in defer], not_before=until)
    return GateDecision(GateOutcome.ALLOW, ["all checks passed"])


def sandbox_targets(settings: Settings) -> set[str]:
    """Handles outbound messages are restricted to (empty: no restriction)."""
    return {t.strip().lstrip("@").lower() for t in settings.rollout.allowed_targets if t.strip()}


class EligibilityGate:
    """Builds :class:`GateFacts` from the database and evaluates them."""

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        ledger: u.UsageLedger,
        lanes: LaneService,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._ledger = ledger
        self._lanes = lanes
        self._rng = rng or random.Random()

    def min_score_for(self, campaign_id: str | None) -> int:
        for campaign in self._settings.campaigns:
            if campaign.id == campaign_id and campaign.min_score is not None:
                return campaign.min_score
        return self._settings.scoring.min_score_to_contact

    def facts_for(
        self,
        session: Session,
        action: Action,
        channels: list[Channel],
        mode: OperatingMode,
        paused: bool,
        limits: LimitsSettings,
    ) -> GateFacts:
        now = self._clock.now()
        account = action.account_id
        tz = self._settings.schedule.timezone
        day_start = local_day_start(now, tz)
        hour_ago = now - timedelta(hours=1)
        ledger = self._ledger

        # Persist the pacing jitter once per action so re-evaluation is stable.
        params = dict(action.params or {})
        if action.type.is_outbound and "_send_jitter" not in params:
            params["_send_jitter"] = round(self._rng.uniform(0, limits.send_jitter_seconds), 1)
            action.params = params

        in_flight = self._in_flight(session, account, action)
        facts = GateFacts(
            now=now,
            mode=mode,
            paused=paused,
            limits=limits,
            schedule=self._settings.schedule,
            action_type=action.type,
            approved_by=action.approved_by,
            min_score=self.min_score_for(action.campaign_id),
            require_business=self._settings.scoring.require_business_signals,
            followup_number=action.followup_number,
            lanes=[self._lanes.snapshot(session, account, ch) for ch in channels],
            outreach_today=ledger.total(session, account, [u.SEND_OUTREACH], day_start)
            + in_flight.get(ActionType.SEND_OUTREACH, 0),
            outreach_last_hour=ledger.total(session, account, [u.SEND_OUTREACH], hour_ago)
            + in_flight.get(ActionType.SEND_OUTREACH, 0),
            oldest_outreach_in_hour=ledger.oldest_since(session, account, [u.SEND_OUTREACH], hour_ago),
            followups_today=ledger.total(session, account, [u.SEND_FOLLOWUP], day_start)
            + in_flight.get(ActionType.SEND_FOLLOW_UP, 0),
            replies_last_hour=ledger.total(session, account, [u.SEND_REPLY], hour_ago)
            + in_flight.get(ActionType.SEND_REPLY, 0),
            oldest_reply_in_hour=ledger.oldest_since(session, account, [u.SEND_REPLY], hour_ago),
            last_send_at=ledger.last_at(session, account, u.SEND_KINDS),
            browser_units_last_hour=ledger.total(session, account, [u.PAGE_VIEW], hour_ago, Channel.BROWSER),
            oldest_browser_unit_in_hour=ledger.oldest_since(session, account, [u.PAGE_VIEW], hour_ago, Channel.BROWSER),
            inspections_today=ledger.total(session, account, [u.INSPECTION], day_start),
            discovery_runs_today=ledger.total(session, account, [u.DISCOVERY_RUN], day_start),
            send_jitter_seconds=float(params.get("_send_jitter", 0.0)),
        )

        allowed = sandbox_targets(self._settings)
        if allowed and action.type.is_outbound:
            facts.target_allowed = (action.target_username or "").lower() in allowed
        lead = session.get(Lead, action.lead_id) if action.lead_id else None
        conversation = session.get(Conversation, action.conversation_id) if action.conversation_id else None
        if lead is not None:
            facts.has_lead = True
            facts.lead_status = lead.status
            facts.lead_score = lead.score
            facts.business_like = (lead.signals or {}).get("business_like")
            facts.followups_sent = lead.followups_sent
            facts.followups_blocked = lead.followups_blocked_reason
            facts.replied = lead.replied_at is not None or lead.status is LeadStatus.REPLIED
            facts.last_outbound_at = lead.last_outbound_at
            facts.contacted_before = (
                lead.contacted_at is not None
                or lead.status in _CONTACTED_STATES
                or self._has_outbound_history(session, account, lead, exclude_action_id=action.id)
            )
            contact = lead.contact or {}
            facts.suppressed = suppression_reason(
                session,
                username=lead.username,
                igsid=lead.igsid,
                domain=lead.website_domain,
                emails=contact.get("emails", []),
                phones=contact.get("phones", []),
            )
        elif action.target_username:
            facts.suppressed = suppression_reason(session, username=action.target_username)
        if conversation is not None:
            facts.conversation_paused = conversation.automation_paused
            facts.conversation_owner = conversation.owner
            if not facts.suppressed and conversation.peer_igsid:
                facts.suppressed = suppression_reason(session, igsid=conversation.peer_igsid)
        return facts

    def check(
        self,
        session: Session,
        action: Action,
        channels: list[Channel],
        mode: OperatingMode,
        paused: bool,
        limits: LimitsSettings,
    ) -> GateDecision:
        return evaluate(self.facts_for(session, action, channels, mode, paused, limits))

    def preview(
        self, session: Session, action: Action, channels: list[Channel], limits: LimitsSettings
    ) -> GateDecision:
        """Proposal-time check, independent of the current mode.

        Evaluates as if the action were approved in AUTONOMOUS mode so that
        permanent denials (suppressed, already contacted, human-owned ...)
        stop an item before it ever reaches the draft/approval queue.
        """
        facts = self.facts_for(session, action, channels, OperatingMode.AUTONOMOUS, False, limits)
        facts.approved_by = AUTO_APPROVER
        return evaluate(facts)

    @staticmethod
    def _in_flight(session: Session, account: str, action: Action) -> dict[ActionType, int]:
        rows = session.execute(
            select(Action.type, func.count())
            .where(
                Action.account_id == account,
                Action.status == ActionStatus.EXECUTING,
                Action.id != action.id,
            )
            .group_by(Action.type)
        ).all()
        return {row[0]: int(row[1]) for row in rows}

    @staticmethod
    def _has_outbound_history(session: Session, account: str, lead: Lead, exclude_action_id: str) -> bool:
        """Any outbound message to this lead other than this action's own.

        An interrupted attempt of *this* action (PENDING message after a crash)
        must not count: whether it went out is resolved by the executor's
        idempotent thread check, not by cancelling the action here.
        """
        conv_ids = select(Conversation.id).where(
            Conversation.account_id == account,
            or_(Conversation.lead_id == lead.id, Conversation.peer_username == lead.username),
        )
        count = session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id.in_(conv_ids),
                Message.direction == MessageDirection.OUTBOUND,
                Message.delivery_state != "FAILED",
                or_(Message.action_id.is_(None), Message.action_id != exclude_action_id),
            )
        )
        return bool(count)
