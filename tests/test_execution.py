from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from insta_outreach.config import ApiSettings, LimitsSettings
from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus
from insta_outreach.domain.models import (
    ApprovedMessage,
    ConversationRef,
    ExecutionResult,
    OperationRequest,
    text_sha256,
)
from insta_outreach.execution.api.adapter import GraphApiAdapter
from insta_outreach.execution.api.client import GraphApiClient, GraphApiError, map_error
from insta_outreach.execution.base import OperationContext
from insta_outreach.execution.executor import InstagramExecutor
from insta_outreach.notify import FanoutNotifier
from insta_outreach.policy.incidents import IncidentService
from insta_outreach.policy.lanes import LaneService
from insta_outreach.policy.usage import UsageLedger
from insta_outreach.storage.db import Database
from insta_outreach.util.clock import FakeClock
from insta_outreach.webhooks import WebhookInbox, parse_payload, verify_signature

NOW = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)


class StubAdapter:
    simulated = True

    def __init__(self, channel: Channel, caps: set[Capability], statuses: list[ExecutionStatus]) -> None:
        self.channel = channel
        self._caps = frozenset(caps)
        self._statuses = list(statuses)
        self.calls = 0

    def capabilities(self) -> frozenset[Capability]:
        return self._caps

    def supports(self, request: OperationRequest) -> bool:
        return request.capability in self._caps

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(status=self._statuses.pop(0), capability=request.capability, channel=self.channel)

    async def close(self) -> None:
        return None


def executor(tmp_path: Any, adapters: dict[Channel, StubAdapter]) -> InstagramExecutor:
    db = Database(f"sqlite:///{tmp_path / 'exec.db'}")
    db.create_all()
    clock = FakeClock(NOW)
    return InstagramExecutor(
        adapters=adapters,
        lanes=LaneService(clock, IncidentService(clock, FanoutNotifier())),  # type: ignore[arg-type]
        db=db,
        clock=clock,
        ledger=UsageLedger(clock),
        limits=LimitsSettings,
        evidence_dir=tmp_path / "ev",
    )


def request(cap: Capability, **kw: Any) -> OperationRequest:
    return OperationRequest(action_id="act_1", account_id="acct", capability=cap, target_username="brew.room", **kw)


async def test_inspection_prefers_api_and_falls_back_to_browser(tmp_path: Any) -> None:
    api = StubAdapter(Channel.API, {Capability.INSPECT_PROFILE}, [ExecutionStatus.TARGET_NOT_FOUND])
    browser = StubAdapter(Channel.BROWSER, {Capability.INSPECT_PROFILE}, [ExecutionStatus.SUCCESS])
    result = await executor(tmp_path, {Channel.API: api, Channel.BROWSER: browser}).execute(
        request(Capability.INSPECT_PROFILE)
    )
    assert result.ok and result.channel is Channel.BROWSER and api.calls == 1


async def test_cold_dm_only_ever_uses_browser(tmp_path: Any) -> None:
    api = StubAdapter(Channel.API, {Capability.SEND_DM_REPLY}, [])
    browser = StubAdapter(Channel.BROWSER, {Capability.SEND_NEW_DM}, [ExecutionStatus.SUCCESS])
    ex = executor(tmp_path, {Channel.API: api, Channel.BROWSER: browser})
    assert ex.candidate_channels(Capability.SEND_NEW_DM) == [Channel.BROWSER]
    assert (await ex.execute(request(Capability.SEND_NEW_DM))).ok


async def test_ambiguous_send_failure_never_falls_back(tmp_path: Any) -> None:
    api = StubAdapter(Channel.API, {Capability.SEND_DM_REPLY}, [ExecutionStatus.RETRYABLE_FAILURE])
    browser = StubAdapter(Channel.BROWSER, {Capability.SEND_DM_REPLY}, [ExecutionStatus.SUCCESS])
    result = await executor(tmp_path, {Channel.API: api, Channel.BROWSER: browser}).execute(
        request(Capability.SEND_DM_REPLY)
    )
    assert result.status is ExecutionStatus.RETRYABLE_FAILURE and browser.calls == 0


async def test_definitely_not_sent_may_fall_back(tmp_path: Any) -> None:
    api = StubAdapter(Channel.API, {Capability.SEND_DM_REPLY}, [ExecutionStatus.NOT_PERMITTED])
    browser = StubAdapter(Channel.BROWSER, {Capability.SEND_DM_REPLY}, [ExecutionStatus.SUCCESS])
    result = await executor(tmp_path, {Channel.API: api, Channel.BROWSER: browser}).execute(
        request(Capability.SEND_DM_REPLY)
    )
    assert result.ok and result.channel is Channel.BROWSER


async def test_barriers_never_fall_back(tmp_path: Any) -> None:
    api = StubAdapter(Channel.API, {Capability.READ_THREAD}, [ExecutionStatus.LOGIN_REQUIRED])
    browser = StubAdapter(Channel.BROWSER, {Capability.READ_THREAD}, [ExecutionStatus.SUCCESS])
    result = await executor(tmp_path, {Channel.API: api, Channel.BROWSER: browser}).execute(
        request(Capability.READ_THREAD)
    )
    assert result.status is ExecutionStatus.LOGIN_REQUIRED and browser.calls == 0


async def test_capability_unavailable_without_adapters(tmp_path: Any) -> None:
    result = await executor(tmp_path, {}).execute(request(Capability.SEARCH_ACCOUNTS))
    assert result.status is ExecutionStatus.CAPABILITY_UNAVAILABLE


# ------------------------------------------------------------------------ Graph API
@pytest.mark.parametrize(
    ("code", "sub", "status"),
    [
        (4, None, ExecutionStatus.RATE_LIMITED),
        (80002, None, ExecutionStatus.RATE_LIMITED),
        (613, 2534040, ExecutionStatus.RATE_LIMITED),
        (190, 463, ExecutionStatus.LOGIN_REQUIRED),
        (10, 2534022, ExecutionStatus.NOT_PERMITTED),
        (551, 1545041, ExecutionStatus.NOT_PERMITTED),
        (100, 2534014, ExecutionStatus.TARGET_NOT_FOUND),
        (110, 2207013, ExecutionStatus.TARGET_NOT_FOUND),
        (200, 2534041, ExecutionStatus.HUMAN_ACTION_REQUIRED),
        (2, None, ExecutionStatus.RETRYABLE_FAILURE),
    ],
)
def test_graph_error_mapping(code: int, sub: int | None, status: ExecutionStatus) -> None:
    assert map_error(GraphApiError(400, code, sub, "x"))[0] is status


def api_settings() -> ApiSettings:
    return ApiSettings(enabled=True, login_type="facebook", access_token="tok", ig_user_id="17840001")  # type: ignore[arg-type]


async def test_graph_client_sends_bearer_and_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"], seen["path"], seen["body"] = req.headers["authorization"], req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"recipient_id": "IGSID", "message_id": "mid.9"})

    client = GraphApiClient(api_settings(), transport=httpx.MockTransport(handler))
    reply = await client.send_text("IGSID", "hello")
    assert reply["message_id"] == "mid.9"
    assert seen == {
        "auth": "Bearer tok",
        "path": "/v26.0/17840001/messages",
        "body": {"recipient": {"id": "IGSID"}, "message": {"text": "hello"}},
    }


async def test_api_adapter_maps_window_error_and_parses_discovery(tmp_path: Any) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "This message is sent outside of allowed window.",
                        "code": 10,
                        "error_subcode": 2534022,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "business_discovery": {
                    "id": "178",
                    "username": "brew.room",
                    "name": "The Brew Room",
                    "biography": "Coffee in Thane",
                    "website": "https://brewroom.example",
                    "followers_count": 2400,
                    "media_count": 80,
                    "media": {
                        "data": [
                            {
                                "caption": "Cold brew",
                                "timestamp": "2026-09-20T10:00:00+0000",
                                "media_product_type": "REELS",
                                "permalink": "https://www.instagram.com/p/X/",
                            }
                        ]
                    },
                }
            },
        )

    clock = FakeClock(NOW)
    adapter = GraphApiAdapter(
        GraphApiClient(api_settings(), transport=httpx.MockTransport(handler)), api_settings(), clock
    )
    ctx = OperationContext(clock=clock)
    inspected = await adapter.execute(request(Capability.INSPECT_PROFILE), ctx)
    assert inspected.ok and inspected.data["profile"]["followers"] == 2400
    assert inspected.data["profile"]["recent_posts"][0]["media_type"] == "REELS"
    text = "Thanks for the reply!"
    reply = request(
        Capability.SEND_DM_REPLY,
        message=ApprovedMessage(action_id="act_1", text=text, sha256=text_sha256(text), kind="reply"),
        conversation=ConversationRef(
            conversation_id=1, peer_username="brew.room", peer_igsid="IGSID", last_inbound_at=NOW - timedelta(hours=2)
        ),
    )
    assert adapter.supports(reply)
    sent = await adapter.execute(reply, ctx)
    assert sent.status is ExecutionStatus.NOT_PERMITTED and sent.code == "outside_messaging_window"
    stale = reply.model_copy(
        update={
            "conversation": reply.conversation.model_copy(  # type: ignore[union-attr]
                update={"last_inbound_at": NOW - timedelta(hours=30)}
            )
        }
    )
    assert not adapter.supports(stale)  # outside the 24h window the API lane is never chosen


async def test_business_discovery_only_with_facebook_login() -> None:
    settings = ApiSettings(enabled=True, login_type="instagram", access_token="tok", ig_user_id="1")  # type: ignore[arg-type]
    adapter = GraphApiAdapter(GraphApiClient(settings), settings, FakeClock(NOW))
    assert Capability.INSPECT_PROFILE not in adapter.capabilities()


# ----------------------------------------------------------------------------- webhooks
def test_signature_verification() -> None:
    body = b'{"object":"instagram"}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, good)
    assert not verify_signature("secret", body + b" ", good)
    assert not verify_signature("secret", body, None)


def test_parse_messages_echoes_and_comments() -> None:
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "17840001",
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": "IGSID"},
                        "recipient": {"id": "17840001"},
                        "timestamp": 1758431000000,
                        "message": {"mid": "m1", "text": "Interested!"},
                    },
                    {
                        "sender": {"id": "17840001"},
                        "recipient": {"id": "IGSID"},
                        "timestamp": 1758431000000,
                        "message": {"mid": "m2", "text": "typed on phone", "is_echo": True},
                    },
                    {
                        "sender": {"id": "IGSID"},
                        "recipient": {"id": "17840001"},
                        "timestamp": 1758431000000,
                        "message": {"mid": "m3", "is_deleted": True},
                    },
                ],
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "c1",
                            "text": "Price?",
                            "from": {"id": "U2", "username": "cafe.x"},
                            "media": {"id": "M1"},
                        },
                    }
                ],
            }
        ],
    }
    events = parse_payload(payload, "17840001", NOW)
    kinds = [(e.kind, e.peer_igsid, e.platform_message_id) for e in events]
    assert kinds == [("message", "IGSID", "m1"), ("echo", "IGSID", "m2"), ("comment", "U2", None)]


def test_webhook_inbox_dedupes_retries(tmp_path: Any) -> None:
    db = Database(f"sqlite:///{tmp_path / 'wh.db'}")
    db.create_all()
    inbox = WebhookInbox(db, FakeClock(NOW), "17840001")
    payload = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {"sender": {"id": "IGSID"}, "recipient": {"id": "17840001"}, "message": {"mid": "m1", "text": "hi"}}
                ]
            }
        ],
    }
    assert inbox.store(payload) and not inbox.store(payload)
    assert [e.text for e in inbox.drain()] == ["hi"]
    assert inbox.drain() == []
