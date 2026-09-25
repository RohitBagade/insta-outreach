"""Comment -> private reply, privacy of unrelated DMs, retention, live-mode token."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from insta_outreach.api.server import create_api
from insta_outreach.config import LLMSettings, OfferSettings
from insta_outreach.domain.enums import (
    ActionStatus,
    Capability,
    Channel,
    Environment,
    IncidentSeverity,
    IncidentStatus,
    LeadStatus,
    OperatingMode,
)
from insta_outreach.execution.base import private_reply_open
from insta_outreach.llm import NullLLM
from insta_outreach.personalization.composer import MessageComposer
from insta_outreach.personalization.validator import MessageValidator
from insta_outreach.storage.models import Conversation, Incident, Lead, Message, WebhookEvent
from tests.conftest import run_ticks

LEAD = "sim.smileline.dental"


def outreach_actions(app: Any, username: str) -> list[dict[str, Any]]:
    actions = [a for a in app.control.list_actions(limit=500) if a["target_username"] == username]
    return sorted((a for a in actions if a["type"] == "SEND_OUTREACH"), key=lambda a: a["created_at"])


def comment_only(settings: Any) -> Any:
    settings.campaigns = []  # no discovery: the comment is the only way in
    settings.simulation.prospects_reply = False
    return settings


async def test_comment_on_our_post_gets_one_private_reply_via_api(make_app, settings, clock) -> None:
    app = make_app(settings=comment_only(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    comment_id = app.world.comment_on_our_post(LEAD, "Love this idea! 😍")
    await run_ticks(app, clock, 8)

    sent = [m for m in app.world.sent_log if m["to"] == LEAD]
    assert len(sent) == 1 and sent[0]["channel"] == Channel.API.value
    assert "commenting on our recent post" in sent[0]["text"]
    assert app.world.comments[comment_id].private_replied
    (action,) = outreach_actions(app, LEAD)
    assert action["capability"] == Capability.PRIVATE_REPLY.value and action["status"] == "SUCCEEDED"
    assert action["params"]["comment_id"] == comment_id
    assert "their_comment" in action["facts_used"]

    lead = app.control.lead_detail(LEAD)
    assert lead["status"] == LeadStatus.CONTACTED.value
    assert lead["sources"][0]["strategy"] == "own_post_comment"
    igsid = app.world.accounts[LEAD].igsid
    with app.db.session() as s:
        conv = s.scalars(select(Conversation).where(Conversation.peer_username == LEAD)).one()
        assert conv.peer_igsid == igsid and not conv.automation_paused  # our own echo, not a takeover

    # The platform allows further messages only after they respond: no follow-ups.
    assert lead["followups_blocked_reason"]
    clock.advance(days=4)
    await run_ticks(app, clock, 12)
    assert [m for m in app.world.sent_log if m["to"] == LEAD] == sent


async def test_refused_private_reply_falls_back_to_a_normal_dm(make_app, settings, clock) -> None:
    app = make_app(settings=comment_only(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    comment_id = app.world.comment_on_our_post(LEAD, "Nice work")
    del app.world.comments[comment_id]  # they deleted the comment before we answered
    await run_ticks(app, clock, 12)

    first, second = outreach_actions(app, LEAD)
    assert first["capability"] == Capability.PRIVATE_REPLY.value and first["status"] == "FAILED"
    assert first["last_result_code"] == "comment_invalid_for_private_reply"
    assert second["capability"] == Capability.SEND_NEW_DM.value and second["status"] == "SUCCEEDED"
    sent = [m for m in app.world.sent_log if m["to"] == LEAD]
    assert len(sent) == 1 and sent[0]["channel"] == Channel.BROWSER.value
    assert app.control.lead_detail(LEAD)["followups_blocked_reason"] is None  # normal DM: follow-ups allowed


async def test_private_reply_window_closing_before_approval_falls_back(make_app, settings, clock) -> None:
    app = make_app(settings=comment_only(settings))
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    app.runtime.set_limit_overrides({"approval_ttl_hours": 400}, "test")
    commented_at = clock.now()  # 10:30 IST
    app.world.comment_on_our_post(LEAD, "Great post")
    await run_ticks(app, clock, 6)
    (draft,) = outreach_actions(app, LEAD)
    assert draft["capability"] == Capability.PRIVATE_REPLY.value and draft["status"] == "PENDING_APPROVAL"

    # Approved at 10:05 IST six days later: inside send hours, but too close to the 7-day limit.
    clock.set(commented_at + timedelta(days=6, hours=23, minutes=35))
    app.control.approve(draft["id"], by="rohit")
    await run_ticks(app, clock, 3)
    first, *rest = outreach_actions(app, LEAD)
    assert first["status"] == "FAILED" and app.world.sent_log == []
    assert rest and rest[0]["capability"] == Capability.SEND_NEW_DM.value
    assert rest[0]["status"] == ActionStatus.PENDING_APPROVAL.value  # a fresh draft, still needs Rohit


def test_private_reply_window_helper() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    assert not private_reply_open({}, now)
    assert private_reply_open({"comment_id": "c1"}, now)  # age unknown: the platform decides
    assert private_reply_open({"comment_id": "c1", "comment_at": (now - timedelta(days=6)).isoformat()}, now)
    assert not private_reply_open({"comment_id": "c1", "comment_at": (now - timedelta(days=7)).isoformat()}, now)
    assert not private_reply_open({"comment_id": "c1", "comment_at": "yesterday"}, now)


async def test_negative_comments_never_start_outreach(make_app, settings, clock) -> None:
    app = make_app(settings=comment_only(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.world.comment_on_our_post(LEAD, "Stop spamming everyone")
    await run_ticks(app, clock, 8)
    with app.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Lead)) == 0
    assert app.world.sent_log == []


def test_commenter_template_thanks_them_without_claiming_a_cold_find() -> None:
    offer = OfferSettings()
    composer = MessageComposer(offer, NullLLM(), MessageValidator(offer), LLMSettings())
    facts = {"business_name": "Smileline Dental Studio", "username": "@sim.smileline.dental", "location": "Mumbai"}
    candidates = composer._template_candidates("initial", facts | {"their_comment": "Love this"}, None, 0)
    text, used = candidates[0]
    assert "Thanks for commenting on our recent post. I had a look at your page in Mumbai." in text
    assert used[0] == "their_comment"
    plain, _ = composer._template_candidates("initial", facts, None, 0)[0]
    assert "commenting" not in plain and "I came across your page" in plain


async def test_unrelated_direct_messages_are_never_stored(make_app, settings, clock) -> None:
    settings.api.app_secret = SecretStr("appsecret")
    settings.control_api.token = SecretStr("s3cret")
    app = make_app(settings=settings)
    app.world.prospect_reply("sim.friend.rahul", "dinner tonight?")  # a personal chat, not a lead
    payload = json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": "17840000000000001",
                    "time": 1,
                    "messaging": [
                        {
                            "sender": {"id": "5550001"},
                            "recipient": {"id": "17840000000000001"},
                            "timestamp": 1758430800000,
                            "message": {"mid": "m_private_1", "text": "are we still on for Sunday?"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"appsecret", payload, hashlib.sha256).hexdigest()
    client = TestClient(create_api(app, run_orchestrator=False))
    assert client.post("/webhooks/instagram", content=payload, headers={"x-hub-signature-256": signature}).is_success
    report = await app.orchestrator.tick()
    assert report.events["ignored"] >= 2 and report.events["inbound"] == 0
    with app.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        assert s.scalar(select(func.count()).select_from(Conversation)) == 0


def test_live_control_plane_requires_a_token(make_app, settings) -> None:
    settings.environment = Environment.LIVE
    app = make_app(settings=settings, adapters={})
    client = TestClient(create_api(app, run_orchestrator=False))
    response = client.get("/api/status")
    assert response.status_code == 503 and "CONTROL_API_TOKEN" in response.json()["detail"]
    settings.control_api.token = SecretStr("s3cret")
    client = TestClient(create_api(make_app(settings=settings, adapters={}), run_orchestrator=False))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"authorization": "Bearer s3cret"}).status_code == 200


def test_retention_prunes_old_payloads_and_evidence(make_app, settings, clock) -> None:
    app = make_app()
    now = clock.now()
    evidence = settings.browser.evidence_dir
    for name in ("2026-08-01", "2026-08-02", "2026-09-20", "notes"):
        (evidence / name).mkdir(parents=True)
        (evidence / name / "shot.png").write_bytes(b"png")
    with app.db.session() as s:
        s.add_all(
            [
                WebhookEvent(received_at=now - timedelta(days=9), payload={}, processed_at=now - timedelta(days=9)),
                WebhookEvent(received_at=now - timedelta(days=2), payload={}, processed_at=now - timedelta(days=2)),
                WebhookEvent(received_at=now - timedelta(days=9), payload={}, processed_at=None),
                Incident(
                    account_id="lemmedeliver",
                    severity=IncidentSeverity.CRITICAL,
                    status=IncidentStatus.OPEN,
                    kind="CHECKPOINT_REQUIRED",
                    title="checkpoint",
                    evidence=[{"kind": "screenshot", "path": str(evidence / "2026-08-02" / "shot.png")}],
                ),
            ]
        )
    first = app.orchestrator.maintenance()
    assert first["webhooks_pruned"] == 1 and first["evidence_folders_pruned"] == 1
    assert sorted(p.name for p in evidence.iterdir()) == ["2026-08-02", "2026-09-20", "notes"]
    with app.db.session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookEvent)) == 2  # recent + unprocessed kept
    assert "webhooks_pruned" not in app.orchestrator.maintenance()  # at most once a day


async def test_negative_reply_suppresses_the_business_not_just_the_handle(make_app, settings, clock) -> None:
    settings.campaigns = []
    app = make_app(settings=settings)
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    app.world.comment_on_our_post("sim.aroma.kitchen.mulund", "Yum")  # replies "Not interested" after 20h
    await run_ticks(app, clock, 8)
    clock.advance(hours=21)
    await run_ticks(app, clock, 3)
    suppressed = {(s["kind"], s["value"]) for s in app.control.suppressions()}
    assert ("USERNAME", "sim.aroma.kitchen.mulund") in suppressed
    assert ("PHONE", "9000000016") in suppressed  # a branch account with the same number is covered too
    assert app.control.lead_detail("sim.aroma.kitchen.mulund")["status"] == LeadStatus.CLOSED.value
