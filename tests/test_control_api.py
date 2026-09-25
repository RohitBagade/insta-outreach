from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from insta_outreach.api.server import create_api
from insta_outreach.domain.enums import Environment, OperatingMode
from tests.conftest import run_ticks

AUTH = {"authorization": "Bearer s3cret"}


def client_for(app: Any) -> TestClient:
    return TestClient(create_api(app, run_orchestrator=False))


def secure(settings: Any) -> Any:
    settings.control_api.token = SecretStr("s3cret")
    settings.api.app_secret = SecretStr("appsecret")
    settings.api.webhook_verify_token = SecretStr("verify-me")
    return settings


def test_control_plane_requires_token(make_app, settings) -> None:
    client = client_for(make_app(settings=secure(settings)))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"authorization": "Bearer nope"}).status_code == 401
    body = client.get("/api/status", headers=AUTH).json()
    assert body["mode"] == "OBSERVE" and body["environment"] == "local"
    assert client.get("/").status_code == 200  # dashboard shell itself holds no data


def test_webhook_verification_and_signatures(make_app, settings) -> None:
    app = make_app(settings=secure(settings))
    client = client_for(app)
    ok = client.get(
        "/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "123"}
    )
    assert ok.status_code == 200 and ok.text == "123"
    assert (
        client.get(
            "/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}
        ).status_code
        == 403
    )
    payload = json.dumps({"object": "instagram", "entry": []}).encode()
    signature = "sha256=" + hmac.new(b"appsecret", payload, hashlib.sha256).hexdigest()
    assert (
        client.post("/webhooks/instagram", content=payload, headers={"x-hub-signature-256": "sha256=bad"}).status_code
        == 403
    )
    assert (
        client.post("/webhooks/instagram", content=payload, headers={"x-hub-signature-256": signature}).status_code
        == 200
    )


def test_live_autonomous_needs_explicit_confirmation(make_app, settings) -> None:
    settings = secure(settings)
    settings.environment = Environment.LIVE
    client = client_for(make_app(settings=settings, adapters={}))
    refused = client.post("/api/mode", json={"mode": "AUTONOMOUS"}, headers=AUTH)
    assert refused.status_code == 400 and "confirmation" in refused.json()["detail"]
    assert client.post("/api/mode", json={"mode": "AUTONOMOUS", "confirm": True}, headers=AUTH).status_code == 200


async def test_approval_endpoints_and_conversation_claim(make_app, settings, clock) -> None:
    app = make_app(settings=secure(settings))
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 40)
    client = client_for(app)
    pending = client.get("/api/actions", params={"status": "PENDING_APPROVAL"}, headers=AUTH).json()
    assert pending
    target = pending[0]
    bad = client.post(
        f"/api/actions/{target['id']}/approve", json={"message": target["message"] + " 50% off!"}, headers=AUTH
    )
    assert bad.status_code == 400
    approved = client.post(f"/api/actions/{target['id']}/approve", json={}, headers=AUTH).json()
    assert approved["status"] == "APPROVED" and approved["approved_by"].startswith("human:")
    claimed = client.post(f"/api/conversations/@{target['target_username']}/claim", headers=AUTH).json()
    assert claimed["owner"] == "HUMAN" and claimed["cancelled_actions"] == 1
    assert client.get(f"/api/actions/{target['id']}", headers=AUTH).json()["status"] == "CANCELLED"


def test_evidence_endpoint_blocks_path_traversal(make_app, settings) -> None:
    client = client_for(make_app(settings=secure(settings)))
    assert client.get("/api/evidence/../../etc/passwd", headers=AUTH).status_code == 404
    assert client.get("/api/evidence/%2Fetc%2Fpasswd", headers=AUTH).status_code == 404


def test_limits_override_validation(make_app, settings) -> None:
    client = client_for(make_app(settings=secure(settings)))
    assert (
        client.patch("/api/limits", json={"overrides": {"outreach_per_day": 5}}, headers=AUTH).json()[
            "outreach_per_day"
        ]
        == 5
    )
    assert client.patch("/api/limits", json={"overrides": {"not_a_limit": 1}}, headers=AUTH).status_code == 400
