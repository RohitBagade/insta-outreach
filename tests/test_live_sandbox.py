"""Rehearsal of docs/LIVE_CHECKLIST.md phase 4 (sandbox) with the real browser agent
against the local mock site: live environment, allowlist, manual test lead,
OBSERVE -> DRAFT -> APPROVAL, one approved send, one inbound reply, AUTONOMOUS refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from insta_outreach.config import Settings
from insta_outreach.devtools.mock_instagram import STATE
from insta_outreach.domain.enums import ActionStatus, Environment, LeadStatus, OperatingMode
from insta_outreach.orchestrator.control import ControlError
from insta_outreach.orchestrator.readiness import blocking
from tests.conftest import run_ticks

pytestmark = pytest.mark.browser

TEST_ACCOUNT = "mock.the.brew.room"  # stands in for your own test account
REAL_PROSPECT = "mock.smileline.dental"  # a real business that must NOT be messaged during the sandbox


def sandbox_settings(tmp_path: Path, base_url: str) -> Settings:
    """The same settings docs/LIVE_CHECKLIST.md asks for, pointed at the mock site."""
    s = Settings(environment=Environment.LIVE, data_dir=tmp_path / "data")
    s.control_api.token = SecretStr("sandbox-token")
    s.browser.enabled = True
    s.browser.base_url = base_url
    s.browser.allowed_hosts = ["127.0.0.1"]
    s.browser.profiles_dir = tmp_path / "profiles"
    s.browser.evidence_dir = tmp_path / "evidence"
    s.browser.typing_delay_ms = 2
    s.browser.trace_on_failure = False
    s.browser.llm_fallback = False
    s.scoring.check_websites = False
    s.scoring.min_followers = 0
    s.campaigns = []  # no discovery during the sandbox test
    s.rollout.allowed_targets = [TEST_ACCOUNT]
    s.limits.outreach_per_day = 1
    s.limits.max_followups_per_lead = 0
    return s


async def test_sandbox_rehearsal_follows_the_live_checklist(make_app, clock, tmp_path, mock_site) -> None:
    STATE.reset(prefix="mock.")
    app = make_app(settings=sandbox_settings(tmp_path, mock_site))
    try:
        app.control.add_lead(f"@{TEST_ACCOUNT}", by="rohit", note="my test account")
        app.control.add_lead(f"@{REAL_PROSPECT}", by="rohit", note="must stay untouched")

        # OBSERVE: inspected and analysed in the (mock) web UI, nothing prepared.
        app.control.set_mode(OperatingMode.OBSERVE, by="rohit")
        await run_ticks(app, clock, 3)
        assert app.control.lead_detail(TEST_ACCOUNT)["status"] == LeadStatus.QUALIFIED.value
        assert app.control.lead_detail(REAL_PROSPECT)["status"] == LeadStatus.QUALIFIED.value
        assert not [a for a in app.control.list_actions(limit=100) if a["type"].startswith("SEND")]

        # DRAFT: only the allowlisted test account gets a draft; nothing is sent.
        app.control.set_mode(OperatingMode.DRAFT, by="rohit")
        await run_ticks(app, clock, 2)
        drafts = [a for a in app.control.list_actions(limit=100) if a["type"] == "SEND_OUTREACH"]
        assert [(a["target_username"], a["status"]) for a in drafts] == [(TEST_ACCOUNT, "DRAFTED")]
        assert STATE.sent == []

        # APPROVAL: the human approves the one draft; the browser agent sends it.
        app.control.set_mode(OperatingMode.APPROVAL, by="rohit")
        app.control.approve(drafts[0]["id"], by="rohit")
        await run_ticks(app, clock, 3)
        assert [m["to"] for m in STATE.sent] == [TEST_ACCOUNT]
        assert app.control.action_detail(drafts[0]["id"])["status"] == ActionStatus.SUCCEEDED.value

        # One inbound reply, seen through the browser inbox (no API configured).
        STATE.thread_for(TEST_ACCOUNT).messages.append(("in", "yes interested, tell me more"))
        await run_ticks(app, clock, 6)
        lead = app.control.lead_detail(TEST_ACCOUNT)
        assert lead["status"] == LeadStatus.HANDED_OFF.value, lead["status_reason"]
        assert any(c["peer_username"] == TEST_ACCOUNT for c in app.control.conversations(paused_only=True))

        # The real prospect was never messaged, and AUTONOMOUS is still refused (1 of 3 approved sends).
        assert all(m["to"] != REAL_PROSPECT for m in STATE.sent)
        assert "approved_sends" in {c.name for c in blocking(app.control.preflight())}
        with pytest.raises(ControlError, match="approved_sends"):
            app.control.set_mode(OperatingMode.AUTONOMOUS, by="rohit", confirm=True)
    finally:
        await app.executor.close()
