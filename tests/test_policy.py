from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from insta_outreach.config import LimitsSettings, OwnershipSettings, ScheduleSettings
from insta_outreach.conversations.ownership import OwnershipService
from insta_outreach.domain.enums import (
    ActionType,
    Capability,
    Channel,
    ConversationOwner,
    ExecutionStatus,
    GateOutcome,
    LaneState,
    LeadStatus,
    MessageDirection,
    OperatingMode,
    SenderKind,
)
from insta_outreach.domain.models import ExecutionResult, ThreadMessage, ThreadSnapshot, text_sha256
from insta_outreach.notify import FanoutNotifier
from insta_outreach.policy.gate import GateFacts, evaluate
from insta_outreach.policy.incidents import IncidentService
from insta_outreach.policy.lanes import LaneService, LaneSnapshot
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import Conversation, Message
from insta_outreach.util.clock import FakeClock

# 10:30 IST on a Monday: inside send hours and browser hours.
NOW = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
ACTIVE = [LaneSnapshot("acct", Channel.BROWSER, LaneState.ACTIVE, None, None)]


def facts(**kw: object) -> GateFacts:
    base: dict[str, object] = dict(
        now=NOW,
        mode=OperatingMode.AUTONOMOUS,
        paused=False,
        limits=LimitsSettings(),
        schedule=ScheduleSettings(),
        action_type=ActionType.SEND_OUTREACH,
        approved_by="auto",
        has_lead=True,
        lead_status=LeadStatus.QUALIFIED,
        lead_score=80,
        business_like=True,
        lanes=list(ACTIVE),
    )
    base.update(kw)
    return GateFacts(**base)  # type: ignore[arg-type]


def test_allows_eligible_outreach() -> None:
    assert evaluate(facts()).outcome is GateOutcome.ALLOW


@pytest.mark.parametrize("mode", [OperatingMode.OBSERVE, OperatingMode.DRAFT])
def test_modes_that_never_send_park_outbound(mode: OperatingMode) -> None:
    decision = evaluate(facts(mode=mode))
    assert decision.outcome is GateOutcome.DEFER and decision.lane_parked


def test_approval_mode_requires_human_approval() -> None:
    decision = evaluate(facts(mode=OperatingMode.APPROVAL, approved_by="auto"))
    assert decision.requires_approval
    assert evaluate(facts(mode=OperatingMode.APPROVAL, approved_by="human:rohit")).outcome is GateOutcome.ALLOW


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"suppressed": "suppressed (USERNAME=x): opted out"}, "suppressed"),
        ({"contacted_before": True}, "repeated-contact"),
        ({"conversation_owner": ConversationOwner.HUMAN}, "owned by the human"),
        ({"conversation_paused": True}, "owned by the human"),
        ({"lead_score": 40}, "below minimum"),
        ({"business_like": False}, "not a business"),
        ({"lead_status": LeadStatus.CONTACTED}, "lead status"),
    ],
)
def test_permanent_denials(kw: dict[str, object], reason: str) -> None:
    decision = evaluate(facts(**kw))
    assert decision.outcome is GateOutcome.DENY
    assert any(reason in r for r in decision.reasons)


def test_daily_cap_defers_to_next_day() -> None:
    decision = evaluate(facts(outreach_today=15))
    assert decision.outcome is GateOutcome.DEFER
    assert decision.not_before is not None and decision.not_before > NOW + timedelta(hours=12)


def test_hourly_cap_defers_until_window_frees() -> None:
    oldest = NOW - timedelta(minutes=40)
    decision = evaluate(facts(outreach_last_hour=4, oldest_outreach_in_hour=oldest))
    assert decision.not_before == oldest + timedelta(hours=1)


def test_pacing_uses_min_gap_plus_jitter() -> None:
    last = NOW - timedelta(seconds=100)
    decision = evaluate(facts(last_send_at=last, send_jitter_seconds=30))
    assert decision.not_before == last + timedelta(seconds=240 + 30)


def test_send_hours_respected() -> None:
    night = datetime(2026, 9, 21, 17, 0, tzinfo=UTC)  # 22:30 IST
    decision = evaluate(facts(now=night))
    assert decision.outcome is GateOutcome.DEFER and "outside send hours" in decision.reasons


def test_halted_lane_parks_and_cooldown_defers() -> None:
    halted = [LaneSnapshot("acct", Channel.BROWSER, LaneState.HALTED, None, "CHECKPOINT_REQUIRED")]
    assert evaluate(facts(lanes=halted)).lane_parked
    until = NOW + timedelta(hours=3)
    cooling = [LaneSnapshot("acct", Channel.BROWSER, LaneState.COOLDOWN, until, "rate limited")]
    assert evaluate(facts(lanes=cooling)).not_before == until


def test_followup_rules() -> None:
    base = dict(
        action_type=ActionType.SEND_FOLLOW_UP,
        lead_status=LeadStatus.CONTACTED,
        followup_number=1,
        last_outbound_at=NOW - timedelta(days=4),
    )
    assert evaluate(facts(**base)).outcome is GateOutcome.ALLOW
    assert evaluate(facts(**base | {"replied": True})).outcome is GateOutcome.DENY
    assert evaluate(facts(**base | {"followups_sent": 2, "followup_number": 3})).outcome is GateOutcome.DENY
    assert evaluate(facts(**base | {"followups_blocked": "request pending"})).outcome is GateOutcome.DENY
    early = evaluate(facts(**base | {"last_outbound_at": NOW - timedelta(days=1)}))
    assert early.outcome is GateOutcome.DEFER and early.not_before == NOW + timedelta(days=2)


def test_read_only_actions_allowed_in_observe_but_capped() -> None:
    inspect = facts(mode=OperatingMode.OBSERVE, action_type=ActionType.INSPECT_PROFILE, approved_by=None)
    assert evaluate(inspect).outcome is GateOutcome.ALLOW
    capped = facts(mode=OperatingMode.OBSERVE, action_type=ActionType.DISCOVER, discovery_runs_today=12)
    assert evaluate(capped).outcome is GateOutcome.DEFER


def test_global_pause_parks_everything() -> None:
    assert evaluate(facts(paused=True, action_type=ActionType.DISCOVER)).lane_parked


# ------------------------------------------------------------------------------ lanes
@pytest.fixture
def db(tmp_path) -> Database:  # type: ignore[no-untyped-def]
    database = Database(f"sqlite:///{tmp_path / 'policy.db'}")
    database.create_all()
    return database


def outcome(status: ExecutionStatus, channel: Channel = Channel.BROWSER, **kw: object) -> ExecutionResult:
    return ExecutionResult(status=status, capability=Capability.SEND_NEW_DM, channel=channel, **kw)  # type: ignore[arg-type]


def test_barrier_halts_lane_and_opens_incident(db: Database) -> None:
    clock = FakeClock(NOW)
    notifier = FanoutNotifier()
    lanes = LaneService(clock, IncidentService(clock, notifier))
    with db.session() as s:
        transition = lanes.record_result(
            s,
            "acct",
            outcome(ExecutionStatus.CHECKPOINT_REQUIRED, code="challenge_url", page_url="https://x/challenge/"),
            LimitsSettings(),
        )
        assert transition is not None and transition.new_state is LaneState.HALTED
        assert lanes.snapshot(s, "acct", Channel.BROWSER).halted
        assert lanes.snapshot(s, "acct", Channel.API).state is LaneState.ACTIVE  # other lane unaffected


def test_account_restriction_halts_every_lane(db: Database) -> None:
    clock = FakeClock(NOW)
    lanes = LaneService(clock, IncidentService(clock, FanoutNotifier()))
    with db.session() as s:
        lanes.record_result(s, "acct", outcome(ExecutionStatus.ACCOUNT_RESTRICTED), LimitsSettings())
        assert all(snap.halted for snap in lanes.all_snapshots(s, "acct"))


def test_rate_limits_cool_down_then_escalate(db: Database) -> None:
    clock = FakeClock(NOW)
    lanes = LaneService(clock, IncidentService(clock, FanoutNotifier()))
    limits = LimitsSettings()
    with db.session() as s:
        first = lanes.record_result(s, "acct", outcome(ExecutionStatus.RATE_LIMITED), limits)
        assert first is not None and first.new_state is LaneState.COOLDOWN
        clock.advance(hours=25)
        assert lanes.snapshot(s, "acct", Channel.BROWSER).state is LaneState.ACTIVE  # cooldown expired
        lanes.record_result(s, "acct", outcome(ExecutionStatus.RATE_LIMITED), limits)
        clock.advance(hours=1)
        second = lanes.record_result(s, "acct", outcome(ExecutionStatus.RATE_LIMITED), limits)
        assert second is not None and second.new_state is LaneState.HALTED  # 2 within 24h


def test_ui_drift_halts_after_threshold(db: Database) -> None:
    clock = FakeClock(NOW)
    lanes = LaneService(clock, IncidentService(clock, FanoutNotifier()))
    with db.session() as s:
        assert lanes.record_result(s, "acct", outcome(ExecutionStatus.UI_CHANGED), LimitsSettings()) is None
        halted = lanes.record_result(s, "acct", outcome(ExecutionStatus.UI_CHANGED), LimitsSettings())
        assert halted is not None and halted.new_state is LaneState.HALTED


def test_missing_message_option_repeated_halts_lane(db: Database) -> None:
    clock = FakeClock(NOW)
    lanes = LaneService(clock, IncidentService(clock, FanoutNotifier()))
    missing = outcome(ExecutionStatus.NOT_PERMITTED, code="no_message_button")
    with db.session() as s:
        assert lanes.record_result(s, "acct", missing, LimitsSettings()) is None
        assert lanes.record_result(s, "acct", outcome(ExecutionStatus.SUCCESS), LimitsSettings()) is None  # resets
        assert lanes.record_result(s, "acct", missing, LimitsSettings()) is None
        assert lanes.record_result(s, "acct", missing, LimitsSettings()) is None
        halted = lanes.record_result(s, "acct", missing, LimitsSettings())
        assert halted is not None and halted.new_state is LaneState.HALTED


# -------------------------------------------------------------------------- ownership
def make_conv(db: Database, ownership: OwnershipService) -> int:
    with db.session() as s:
        return ownership.get_or_create(s, "acct", peer_username="brew.room").id


def test_single_owner_lease_and_transfer(db: Database) -> None:
    clock = FakeClock(NOW)
    ownership = OwnershipService(clock, OwnershipSettings())
    conv_id = make_conv(db, ownership)
    with db.session() as s:
        token = ownership.acquire(s, conv_id, ConversationOwner.BROWSER_AGENT)
        assert token is not None
        assert ownership.acquire(s, conv_id, ConversationOwner.API_AGENT) is None  # one owner at a time
        assert ownership.transfer(s, conv_id, token, ConversationOwner.API_AGENT) == token
        assert ownership.still_owner(s, conv_id, token)
        assert ownership.release(s, conv_id, token)
        clock.advance(seconds=1)
        assert ownership.acquire(s, conv_id, ConversationOwner.API_AGENT) is not None


def test_expired_lease_can_be_taken(db: Database) -> None:
    clock = FakeClock(NOW)
    ownership = OwnershipService(clock, OwnershipSettings(lock_ttl_seconds=60))
    conv_id = make_conv(db, ownership)
    with db.session() as s:
        old = ownership.acquire(s, conv_id, ConversationOwner.BROWSER_AGENT)
        clock.advance(seconds=61)
        assert ownership.acquire(s, conv_id, ConversationOwner.API_AGENT) is not None
        assert old is not None and not ownership.still_owner(s, conv_id, old)


def test_human_message_in_thread_pauses_automation(db: Database) -> None:
    clock = FakeClock(NOW)
    ownership = OwnershipService(clock, OwnershipSettings())
    conv_id = make_conv(db, ownership)
    with db.session() as s:
        conv = s.get(Conversation, conv_id)
        assert conv is not None
        ownership.record_automation_intent(
            s, conv, text="Hi from automation", sender=SenderKind.BROWSER_AGENT, action_id="act_1"
        )
        snap = ThreadSnapshot(
            peer_username="brew.room",
            messages=[
                ThreadMessage(direction=MessageDirection.OUTBOUND, text="Hi from automation"),
                ThreadMessage(direction=MessageDirection.INBOUND, text="Tell me more"),
                ThreadMessage(direction=MessageDirection.OUTBOUND, text="Rohit here, calling you now"),
            ],
        )
        result = ownership.reconcile(s, conv, snap, "browser_read")
        assert len(result.new_inbound) == 1 and len(result.human_outbound) == 1
        assert conv.owner is ConversationOwner.HUMAN and conv.automation_paused
        assert ownership.acquire(s, conv_id, ConversationOwner.BROWSER_AGENT) is None
        # reconciling the same snapshot again adds nothing
        again = ownership.reconcile(s, conv, snap, "browser_read")
        assert not again.new_inbound and not again.human_outbound


def test_echo_attribution(db: Database) -> None:
    clock = FakeClock(NOW)
    ownership = OwnershipService(clock, OwnershipSettings())
    conv_id = make_conv(db, ownership)
    with db.session() as s:
        conv = s.get(Conversation, conv_id)
        assert conv is not None
        ownership.record_automation_intent(
            s, conv, text="Automated hello", sender=SenderKind.BROWSER_AGENT, action_id="act_1"
        )
        # An echo keyed only by an unknown IGSID is matched back to its conversation by text.
        found = ownership.match_automation_echo(s, "acct", text="Automated hello", at=NOW)
        assert found is not None and found.id == conv_id
        assert (
            ownership.record_echo(s, conv, text="Automated hello", platform_message_id="mid.1", at=NOW)
            is SenderKind.BROWSER_AGENT
        )
        assert not conv.automation_paused
        assert ownership.match_automation_echo(s, "acct", text="Automated hello", at=NOW) is None  # attributed
        assert (
            ownership.record_echo(s, conv, text="typed by Rohit", platform_message_id="mid.2", at=NOW)
            is SenderKind.HUMAN
        )
        assert conv.automation_paused and conv.owner is ConversationOwner.HUMAN
        stored = s.query(Message).filter(Message.platform_message_id == "mid.2").one()
        assert stored.text_hash == text_sha256("typed by Rohit")


def test_human_hold_expires_when_configured(db: Database) -> None:
    clock = FakeClock(NOW)
    ownership = OwnershipService(clock, OwnershipSettings(human_hold_hours=2))
    conv_id = make_conv(db, ownership)
    with db.session() as s:
        conv = s.get(Conversation, conv_id)
        assert conv is not None
        ownership.take_over_by_human(s, conv, "claimed")
        clock.advance(hours=3)
        assert ownership.acquire(s, conv_id, ConversationOwner.BROWSER_AGENT) is not None
