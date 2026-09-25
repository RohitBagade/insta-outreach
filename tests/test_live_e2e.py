"""LIVE-environment wiring end to end: orchestrator -> real Playwright agent -> mock site."""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_outreach.config import CampaignSettings, Settings, StrategySpec
from insta_outreach.devtools.mock_instagram import STATE
from insta_outreach.domain.enums import Environment, OperatingMode
from tests.conftest import run_ticks

pytestmark = pytest.mark.browser


def live_settings(tmp_path: Path, base_url: str) -> Settings:
    settings = Settings(environment=Environment.LIVE, data_dir=tmp_path / "data")
    settings.browser.enabled = True
    settings.browser.base_url = base_url
    settings.browser.allowed_hosts = ["127.0.0.1"]
    settings.browser.profiles_dir = tmp_path / "profiles"
    settings.browser.evidence_dir = tmp_path / "evidence"
    settings.browser.typing_delay_ms = 2
    settings.browser.trace_on_failure = False
    settings.browser.llm_fallback = False
    settings.scoring.check_websites = False  # no outbound HTTP from tests
    settings.campaigns = [
        CampaignSettings(
            id="thane-cafes",
            niches=["cafe"],
            locations=["Thane"],
            strategies=[StrategySpec(name="keyword_search", params={"max_queries_per_run": 1, "max_results": 5})],
        )
    ]
    return settings


async def test_orchestrator_drives_real_browser_agent(make_app, clock, tmp_path, mock_site) -> None:
    STATE.reset(prefix="mock.")
    app = make_app(settings=live_settings(tmp_path, mock_site))
    assert type(app.executor.adapters[next(iter(app.executor.adapters))]).__name__ == "PlaywrightBrowserAdapter"
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    try:
        await run_ticks(app, clock, 12)
        brew = app.control.lead_detail("mock.the.brew.room")
        assert brew["status"] == "CONTACTED", brew["status_reason"]
        assert {o["type"] for o in brew["opportunities"]} >= {"NEW_WEBSITE", "ONLINE_BOOKING"}
        assert [m["to"] for m in STATE.sent] == ["mock.the.brew.room"]
        assert "LemmeDeliver" in STATE.sent[0]["text"]
        # The account Rohit had already messaged by hand is never cold-messaged.
        chai = app.control.lead_detail("mock.chai.and.chapters")
        assert chai["status"] in ("HANDED_OFF", "CONTACTED")
        sent = [
            a
            for a in app.control.list_actions(limit=100)
            if a["type"] == "SEND_OUTREACH" and a["status"] == "SUCCEEDED"
        ]
        evidence = app.control.action_detail(sent[0]["id"])["attempt_log"][-1]["evidence"]
        assert any(e["kind"] == "screenshot" for e in evidence)
    finally:
        await app.executor.close()
