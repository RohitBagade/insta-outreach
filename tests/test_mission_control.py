"""Mission Control: the live dashboard's page, overview and feed endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from insta_outreach.api.server import create_api
from insta_outreach.domain.enums import Channel, OperatingMode
from insta_outreach.policy import usage as u
from insta_outreach.storage.models import AuditEvent
from insta_outreach.util.clock import local_day_start
from tests.conftest import run_ticks

AUTH = {"authorization": "Bearer s3cret"}
TONES = {"good", "warning", "critical", "info", "neutral"}
NODES = {"discover", "analyze", "draft", "gate", "send", "conversation", None}


def client_for(app: Any) -> TestClient:
    app.settings.control_api.token = SecretStr("s3cret")
    return TestClient(create_api(app, run_orchestrator=False))


def test_page_is_static_and_locked_down(make_app) -> None:
    client = client_for(make_app())
    page = client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert "<script>" not in page.text and "style=" not in page.text  # nothing inline, so the CSP holds
    script = client.get("/static/app.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
    for path in ("/static/index.html", "/static/..%2Fserver.py", "/static/../server.py", "/static/nope.js"):
        assert client.get(path).status_code == 404, path
    # The page holds no data; everything behind it needs the token.
    for path in ("/api/overview", "/api/feed", "/api/leads/x/explain"):
        assert client.get(path).status_code == 401, path


async def test_overview_counts_match_the_gate(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 40)
    client = client_for(app)
    o = client.get("/api/overview", headers=AUTH).json()
    assert o["mode"] == "AUTONOMOUS" and o["simulated"] is True and not o["paused"]
    assert {lane["channel"] for lane in o["lanes"]} == {"API", "BROWSER"}
    assert all(lane["state"] == "ACTIVE" and lane["configured"] for lane in o["lanes"])

    status = client.get("/api/status", headers=AUTH).json()
    p = o["pipeline"]
    assert p["discover"]["total"] == sum(status["leads"].values()) > 0
    assert p["send"]["sent_today"] == status["sent_today"] > 0
    assert p["send"]["sent_today"] == sum(p["send"]["sent_today_by_channel"].values())

    # Limit meters read the same ledger the eligibility gate enforces.
    now = clock.now()
    with app.db.session() as session:
        expected_today = app.ledger.total(
            session, app.settings.account.id, [u.SEND_OUTREACH], local_day_start(now, app.settings.schedule.timezone)
        )
        expected_hour = app.ledger.total(session, app.settings.account.id, [u.SEND_OUTREACH], now - timedelta(hours=1))
    usage = o["usage"]
    assert usage["outreach_today"] == expected_today > 0
    assert usage["outreach_last_hour"] == expected_hour
    assert usage["outreach_per_day"] == app.runtime.limits().outreach_per_day
    assert usage["in_send_hours"] is True

    # Halting a lane shows up as something that needs attention.
    app.control.halt_lane(Channel.BROWSER, by="test", reason="manual check")
    o = client.get("/api/overview", headers=AUTH).json()
    assert o["attention"]["halted_lanes"] == ["BROWSER"]
    assert next(lane for lane in o["lanes"] if lane["channel"] == "BROWSER")["state"] == "HALTED"


async def test_feed_cursor_never_repeats_or_drops_events(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    client = client_for(app)
    first = client.get("/api/feed", headers=AUTH).json()
    assert [i["kind"] for i in first["items"]] == ["mode.changed"]
    assert first["cursor"] == {"audit": first["items"][0]["id"], "attempt": 0}

    seen = [(item["source"], item["id"]) for item in first["items"]]
    cursor = first["cursor"]
    for _ in range(4):
        await run_ticks(app, clock, 10)
        page = client.get("/api/feed", params={**cursor, "limit": 500}, headers=AUTH).json()
        assert page["cursor"]["audit"] >= cursor["audit"] and page["cursor"]["attempt"] >= cursor["attempt"]
        cursor = page["cursor"]
        for item in page["items"]:
            assert item["tone"] in TONES and item["node"] in NODES
            assert item["summary"] and item["at_local"]
        ats = [item["at"] for item in page["items"]]
        assert ats == sorted(ats)
        seen += [(item["source"], item["id"]) for item in page["items"]]
    assert len(seen) == len(set(seen))  # nothing twice

    # Every audit event is delivered exactly once.
    with app.db.session() as session:
        audit_ids = set(session.scalars(select(AuditEvent.id)))
    assert {i for source, i in seen if source == "audit"} == audit_ids
    kinds = {item["kind"] for item in client.get("/api/feed", params={"limit": 500}, headers=AUTH).json()["items"]}
    assert "message.sent" in kinds and "exec.discover" in kinds and "exec.inspect_profile" in kinds

    # Nothing new: an empty page with the same cursor.
    idle = client.get("/api/feed", params=cursor, headers=AUTH).json()
    assert idle == {"items": [], "cursor": cursor}


async def test_explain_endpoint_returns_the_decision_trail(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 40)
    client = client_for(app)
    pending = client.get("/api/actions", params={"status": "PENDING_APPROVAL"}, headers=AUTH).json()
    handle = pending[0]["target_username"]
    lines = client.get(f"/api/leads/{handle}/explain", headers=AUTH).json()["lines"]
    assert lines[0].startswith(f"@{handle}") and "HOW IT WAS FOUND" in lines
    assert client.get("/api/leads/nobody.here/explain", headers=AUTH).json()["lines"] == ["no lead @nobody.here"]

    edited = pending[0]["message"].replace("Hi", "Hey", 1) + " "
    approved = client.post(
        f"/api/actions/{pending[0]['id']}/approve", json={"edited_text": edited}, headers=AUTH
    ).json()
    assert approved["status"] == "APPROVED" and approved["message"] == edited.strip()
