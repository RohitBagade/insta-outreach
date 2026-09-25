from __future__ import annotations

from collections import Counter
from datetime import timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import select

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Channel,
    ConversationOwner,
    LaneState,
    LeadStatus,
    OperatingMode,
    SenderKind,
)
from insta_outreach.orchestrator.control import ControlError
from insta_outreach.storage.models import Action, Conversation, Lead
from tests.conftest import run_ticks


def outbound(app: Any) -> list[dict[str, Any]]:
    return [a for a in app.control.list_actions(limit=500) if a["type"].startswith("SEND")]


async def test_observe_discovers_and_analyzes_but_never_prepares_outreach(make_app, clock) -> None:
    app = make_app()
    await run_ticks(app, clock, 40)
    status = app.control.status()
    assert status["leads"].get("QUALIFIED", 0) > 3
    assert outbound(app) == [] and app.world.sent_log == []


async def test_draft_prepares_but_never_sends(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.DRAFT, "test")
    await run_ticks(app, clock, 40)
    drafts = outbound(app)
    assert drafts and {a["status"] for a in drafts} == {"DRAFTED"}
    assert app.world.sent_log == []


async def test_approval_flow_with_edit_and_redraft(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 40)
    pending = app.control.list_actions([ActionStatus.PENDING_APPROVAL])
    assert pending and app.world.sent_log == []
    first, second = pending[0], pending[1]
    edited = first["message"].replace("Hi there!", "Hello!")
    try:
        app.control.approve(first["id"], by="rohit", edited_text=first["message"] + " Prices from ₹999!")
        raise AssertionError("invalid edit must be rejected")
    except ControlError as exc:
        assert "prices" in str(exc)
    app.control.approve(first["id"], by="rohit", edited_text=edited)
    app.control.reject(second["id"], by="rohit", reason="tone", redraft=True)
    await run_ticks(app, clock, 6)
    assert [m["text"] for m in app.world.sent_log] == [edited]
    with app.db.session() as s:
        lead = s.scalars(select(Lead).where(Lead.username == second["target_username"])).one()
        redrafts = s.scalars(
            select(Action).where(Action.lead_id == lead.id, Action.type == ActionType.SEND_OUTREACH)
        ).all()
        assert len(redrafts) == 2  # a fresh draft was prepared


async def test_switching_to_approval_demotes_auto_approved_work(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.runtime.set_limit_overrides({"outreach_per_hour": 1}, "test")
    await run_ticks(app, clock, 30)
    sent_before = len(app.world.sent_log)
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 30)
    assert len(app.world.sent_log) == sent_before
    statuses = Counter(a["status"] for a in outbound(app))
    assert statuses["PENDING_APPROVAL"] > 0 and statuses["APPROVED"] == 0


async def test_autonomous_respects_caps_and_pacing(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.runtime.set_limit_overrides({"outreach_per_day": 5, "outreach_per_hour": 2}, "test")
    await run_ticks(app, clock, 66)
    sends = [m["at"] for m in app.world.sent_log]
    assert len(sends) == 5
    for a, b in pairwise(sends):
        assert (b - a).total_seconds() >= 240
    for i, t in enumerate(sends):
        assert sum(1 for x in sends if t <= x < t + timedelta(hours=1)) <= 2, i


async def test_global_pause_stops_all_execution(make_app, clock) -> None:
    app = make_app()
    app.control.set_paused(True, by="rohit")
    await run_ticks(app, clock, 10)
    assert app.world.browser_ops == 0 and app.control.list_actions() == []


async def test_human_takeover_via_echo_pauses_conversation(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 40)
    contacted = [a["target_username"] for a in outbound(app) if a["status"] == "SUCCEEDED"]
    target = contacted[-1]
    app.world.human_sends(target, "Rohit here - calling you in 5!")
    await run_ticks(app, clock, 2)
    conv = next(c for c in app.control.conversations() if c["peer_username"] == target)
    assert conv["owner"] == ConversationOwner.HUMAN.value and conv["automation_paused"]
    clock.advance(days=4)
    await run_ticks(app, clock, 40)
    followups = [a for a in outbound(app) if a["type"] == "SEND_FOLLOW_UP" and a["target_username"] == target]
    assert followups == []  # never automated again while Rohit owns it
    app.control.release_conversation(target, by="rohit")
    assert not next(c for c in app.control.conversations() if c["peer_username"] == target)["automation_paused"]


async def test_opt_out_and_negative_replies_are_suppressed(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 66)
    clock.advance(hours=13)
    await run_ticks(app, clock, 30)
    suppressed = {s["value"]: s["source"] for s in app.control.suppressions()}
    assert suppressed.get("sim.glowup.salon.powai") == "OPT_OUT"
    lead = app.control.lead_detail("sim.glowup.salon.powai")
    assert lead["status"] == LeadStatus.CLOSED.value


async def test_checkpoint_halts_browser_lane_and_resume_releases_work(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.world.faults.checkpoint_after_browser_ops = 3
    await run_ticks(app, clock, 6)
    status = app.control.status()
    lanes = {lane["channel"]: lane["state"] for lane in status["lanes"]}
    assert lanes == {"API": LaneState.ACTIVE.value, "BROWSER": LaneState.HALTED.value}
    incidents = app.control.incidents()
    assert incidents and incidents[0]["kind"] == "CHECKPOINT_REQUIRED" and "/challenge/" in incidents[0]["page_url"]
    ops = app.world.browser_ops
    await run_ticks(app, clock, 6)
    assert app.world.browser_ops == ops  # nothing touches the browser while halted
    parked = app.control.list_actions([ActionStatus.NEEDS_HUMAN])
    assert parked
    app.world.faults.checkpoint_after_browser_ops = None
    assert app.control.resume_lane(Channel.BROWSER, by="rohit", note="confirmed on phone") == len(parked)
    await run_ticks(app, clock, 6)
    assert app.world.browser_ops > ops and app.control.incidents() == []


async def test_crash_recovery_never_double_sends(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.runtime.set_limit_overrides({"outreach_per_hour": 1}, "test")  # keep outreach queued
    queued = None
    for _ in range(40):
        await run_ticks(app, clock, 1)
        with app.db.session() as s:
            queued = s.scalars(
                select(Action.id).where(Action.type == ActionType.SEND_OUTREACH, Action.status == ActionStatus.APPROVED)
            ).first()
        if queued:
            break
    assert queued is not None, "expected queued outreach"
    # Reproduce the real sequence: the worker records its intent, the message goes
    # out, then the process dies before the result is recorded (lease left EXECUTING).
    with app.db.session() as s:
        action = s.get(Action, queued)
        assert action is not None
        conversation = s.get(Conversation, action.conversation_id)
        assert conversation is not None
        app.services.ownership.record_automation_intent(
            s, conversation, text=action.message_text or "", sender=SenderKind.BROWSER_AGENT, action_id=action.id
        )
        action.status, action.lease_expires_at = ActionStatus.EXECUTING, clock.now() - timedelta(minutes=1)
        target, text, action_id = action.target_username, action.message_text, action.id
    app.world.deliver(target, text, Channel.BROWSER)
    await run_ticks(app, clock, 12)
    detail = app.control.action_detail(action_id)
    assert detail["status"] == "SUCCEEDED" and detail["last_result_code"] == "already_sent_idempotent"
    assert [m["text"] for m in app.world.sent_log if m["to"] == target] == [text]


async def test_branch_accounts_sharing_a_domain_are_contacted_once(make_app, clock) -> None:
    app = make_app()
    await run_ticks(app, clock, 60)
    bandra, khar = app.control.lead_detail("sim.pearl.dental.bandra"), app.control.lead_detail("sim.pearl.dental.khar")
    assert LeadStatus.DUPLICATE.value in (bandra["status"], khar["status"])


async def test_existing_human_thread_is_never_cold_messaged(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 66)
    assert all(m["to"] != "sim.chai.and.chapters" for m in app.world.sent_log)
    lead = app.control.lead_detail("sim.chai.and.chapters")
    assert lead["status"] in (
        LeadStatus.HANDED_OFF.value,
        LeadStatus.CONTACTED.value,
        LeadStatus.QUALIFIED.value,
        LeadStatus.ANALYZED.value,
        LeadStatus.DISCOVERED.value,
    )
