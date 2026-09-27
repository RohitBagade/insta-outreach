"""Mission Control without the command line: settings, logins, alerts, restart,
the home page's numbers, analytics and sales stages."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from insta_outreach.api.server import Restarter, create_api
from insta_outreach.config import load_settings, overlay_path
from insta_outreach.deploy import launcher, set_env_values
from insta_outreach.domain.enums import (
    ActionStatus,
    Channel,
    Environment,
    LaneState,
    LeadStatus,
    OperatingMode,
)
from insta_outreach.execution.simulator import SimulatedApiAdapter, SimulatedBrowserAdapter, SimulatedWorld
from insta_outreach.orchestrator.monitor import plain_event
from insta_outreach.storage.models import Lead
from tests.conftest import logged_in, run_ticks

AUTH = {"authorization": "Bearer s3cret"}
SETTINGS_YAML = """\
# my own comment, never rewritten
environment: local
default_mode: OBSERVE
"""


def client_for(app: Any, restarter: Restarter | None = None) -> TestClient:
    app.settings.control_api.token = SecretStr("s3cret")
    return TestClient(create_api(app, run_orchestrator=False, restarter=restarter))


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "settings.yaml"
    path.parent.mkdir()
    path.write_text(
        SETTINGS_YAML + f"data_dir: {tmp_path / 'data'}\nbrowser:\n  profiles_dir: {tmp_path / 'profiles'}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def file_app(make_app, config_file: Path, monkeypatch) -> Any:
    monkeypatch.setenv("CONTROL_API_TOKEN", "s3cret")
    app = make_app(settings=load_settings(config_file))
    app.setup.env_file = config_file.parent.parent / ".env"
    return app


# ------------------------------------------------------------------ settings
def test_settings_page_writes_its_own_file_and_applies_at_once(file_app, config_file: Path) -> None:
    client = client_for(file_app)
    view = client.get("/api/setup", headers=AUTH).json()
    assert view["can_edit"] and view["values"]["environment"] == "local" and view["pending_restart"] == []
    campaigns = view["values"]["campaigns"]
    campaigns[0]["locations"] = "Thane, Vashi,  Thane"
    campaigns[0]["strategies"][2]["params"]["tags"] = ["#ThaneDentist", "vashi salon"]
    changes = {
        "rollout.allowed_targets": "@My.Test.Account, my.test.account",
        "schedule.send_hours": ["11:00", "19:30"],
        "scoring.min_score_to_contact": 70,
        "campaigns": campaigns,
    }
    out = client.patch("/api/setup", json={"changes": changes}, headers=AUTH).json()
    assert sorted(out["applied"]) == sorted(changes) and out["restart_required"] == []

    # Applied to the running program: the gate and the planner read these objects.
    running = file_app.settings
    assert running.rollout.allowed_targets == ["my.test.account"]
    assert running.schedule.send_hours == ("11:00", "19:30")
    assert running.scoring.min_score_to_contact == 70
    assert running.campaigns[0].locations == ["Thane", "Vashi"]
    assert running.campaigns[0].strategies[2].params["tags"] == ["thanedentist", "vashisalon"]

    # Saved next to settings.yaml, which keeps its comments; a fresh load sees both.
    assert config_file.read_text(encoding="utf-8").startswith("# my own comment")
    saved = yaml.safe_load(overlay_path(config_file).read_text(encoding="utf-8"))
    assert saved["rollout"] == {"allowed_targets": ["my.test.account"]}
    fresh = load_settings(config_file)
    assert fresh.schedule.send_hours == ("11:00", "19:30") and fresh.campaigns[0].locations == ["Thane", "Vashi"]

    # A setting that shapes how the program is built waits for a restart.
    out = client.patch("/api/setup", json={"changes": {"browser.headless": False}}, headers=AUTH).json()
    assert out["applied"] == [] and out["restart_required"] == ["browser.headless"]
    assert running.browser.headless is True and load_settings(config_file).browser.headless is False
    # None puts the settings.yaml value back.
    out = client.patch("/api/setup", json={"changes": {"browser.headless": None}}, headers=AUTH).json()
    assert out["restart_required"] == []
    titles = [i["title"] for i in file_app.monitor.feed(limit=50)["items"] if i["kind"] == "settings.changed"]
    assert "You changed settings: Hide the browser window while it works (after a restart)" in titles


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schedule.send_hours": ["25:00", "20:00"]}, "sending hours"),
        ({"rollout.allowed_targets": "not a handle!"}, "test accounts"),
        ({"research.enabled": True}, "research account's username"),
        ({"research.enabled": True, "research.account.username": "@LemmeDeliver"}, "different account"),
        ({"offer.website_url": "lemmedeliver.com"}, "https://"),
        ({"control_api.token": "x"}, "cannot be changed here"),
        ({"campaigns": [{"id": "Bad Name", "niches": ["x"], "locations": ["y"], "strategies": []}]}, "campaign name"),
    ],
)
def test_bad_settings_are_refused_with_a_reason(file_app, config_file: Path, changes: dict, message: str) -> None:
    client = client_for(file_app)
    response = client.patch("/api/setup", json={"changes": changes}, headers=AUTH)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert not overlay_path(config_file).exists()  # nothing half-written


def test_real_instagram_needs_the_control_token(file_app, config_file: Path, monkeypatch) -> None:
    client = client_for(file_app)
    monkeypatch.delenv("CONTROL_API_TOKEN")
    response = client.patch("/api/setup", json={"changes": {"environment": "live"}}, headers=AUTH)
    assert response.status_code == 400 and "CONTROL_API_TOKEN" in response.json()["detail"]
    monkeypatch.setenv("CONTROL_API_TOKEN", "s3cret")
    out = client.patch(
        "/api/setup", json={"changes": {"environment": "live", "browser.enabled": True}}, headers=AUTH
    ).json()
    assert sorted(out["restart_required"]) == ["browser.enabled", "environment"]
    assert load_settings(config_file).environment is Environment.LIVE
    assert file_app.settings.environment is Environment.LOCAL  # until the restart


def test_demo_settings_are_fixed(make_app) -> None:
    client = client_for(make_app())  # built in code, like the demo: no settings file
    assert client.get("/api/setup", headers=AUTH).json()["can_edit"] is False
    response = client.patch("/api/setup", json={"changes": {"scoring.min_score_to_contact": 10}}, headers=AUTH)
    assert response.status_code == 400 and "demo" in response.json()["detail"]


def test_restart_is_offered_only_by_the_run_command(make_app) -> None:
    app = make_app()
    assert client_for(app).post("/api/setup/restart", headers=AUTH).status_code == 409
    restarter = Restarter()
    client = client_for(app, restarter)
    assert client.get("/api/setup", headers=AUTH).json()["restart_supported"] is True
    with client:
        assert client.post("/api/setup/restart", headers=AUTH).json() == {"restarting": True}
        deadline = time.time() + 3
        while not restarter.requested and time.time() < deadline:
            time.sleep(0.05)
    assert restarter.requested


def test_launcher_starts_the_program_and_opens_the_page(tmp_path: Path) -> None:
    path, content = launcher("win32", tmp_path, Path("C:/py/python.exe"))
    assert path.name == "Mission Control.cmd" and "\r\n" in content
    assert "-m insta_outreach run --open" in content and "PYTHONUTF8=1" in content
    path, content = launcher("linux", tmp_path, Path("/venv/bin/python"))
    assert path.name == "mission-control.sh" and content.startswith("#!/bin/sh")


# -------------------------------------------------------------------- alerts
def test_env_file_values_are_one_line_and_other_lines_stay(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# keep me\nCONTROL_API_TOKEN=abc\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    set_env_values(env, {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
    assert env.read_text(encoding="utf-8") == (
        "# keep me\nCONTROL_API_TOKEN=abc\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=42\n"
    )
    set_env_values(env, {"TELEGRAM_CHAT_ID": None})
    assert "TELEGRAM_CHAT_ID" not in env.read_text(encoding="utf-8")
    for bad in ("a\nEVIL=1", "", "x\r"):
        with pytest.raises(ValueError):
            set_env_values(env, {"TELEGRAM_CHAT_ID": bad})
    with pytest.raises(ValueError):
        set_env_values(env, {"lower-case": "1"})


def test_phone_alerts_are_set_up_from_the_page(file_app, monkeypatch) -> None:
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):  # undone after the test
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    sent: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, token: str, chat_id: str, *_: Any) -> None:
            self.chat_id, self.last_error = chat_id, None

        async def notify(self, title: str, *_: Any) -> None:
            sent.append((self.chat_id, title))

    async def one_chat(_token: str) -> list[tuple[str, str]]:
        return [("987654", "Rohit")]

    monkeypatch.setattr("insta_outreach.orchestrator.setup.TelegramNotifier", FakeTelegram)
    monkeypatch.setattr("insta_outreach.orchestrator.setup.telegram_chats", one_chat)
    client = client_for(file_app)
    token = "123456789:" + "A" * 35
    assert client.post("/api/alerts/telegram", json={"bot_token": "nope"}, headers=AUTH).status_code == 400
    assert client.post("/api/alerts/test", headers=AUTH).status_code == 400  # nothing set up yet
    alerts = client.post("/api/alerts/telegram", json={"bot_token": token}, headers=AUTH).json()
    assert alerts["telegram_token_set"] and not alerts["telegram_ready"]
    found = client.post("/api/alerts/find-chat", headers=AUTH).json()
    assert found["saved"] and "Rohit" in found["message"]
    assert client.post("/api/alerts/test", headers=AUTH).json()["results"][0]["ok"] is True
    assert sent == [("987654", "Test alert from insta-outreach")]
    env = file_app.setup.env_file.read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={token}" in env and "TELEGRAM_CHAT_ID=987654" in env
    # Real alerts go there too, without a restart; the token is never echoed back.
    kinds = [type(n).__name__ for n in file_app.notifier._notifiers]
    assert "TelegramNotifier" in kinds
    assert token not in client.get("/api/setup", headers=AUTH).text
    assert token not in str(file_app.control.audit_events("alerts"))


# ------------------------------------------------------------ account logins
def live_app(make_app, settings: Any, clock: Any) -> Any:
    settings.environment = Environment.LIVE
    world = SimulatedWorld.seeded(clock, 7)
    app = make_app(
        settings=settings,
        world=world,
        adapters={Channel.API: SimulatedApiAdapter(world), Channel.BROWSER: SimulatedBrowserAdapter(world)},
    )
    app.world = world
    return app


async def test_login_window_pauses_the_lane_and_records_the_session(make_app, settings, clock) -> None:
    app = live_app(make_app, settings, clock)
    opened = asyncio.Event()
    finish = asyncio.Event()
    seen: list[str] = []

    async def fake_login(chosen: Any, announce: Any) -> tuple[bool, str]:
        seen.append(chosen.account.username)
        announce("A browser window is open")
        opened.set()
        await finish.wait()
        return True, "logged in"

    app.setup.accounts._login = fake_login
    job = app.setup.accounts.start_login("brand", by="operator")
    assert job["state"] == "running"
    await opened.wait()
    rows = app.setup.accounts.accounts()
    assert rows[0]["job"]["message"] == "A browser window is open"
    assert rows[0]["lane_state"] == LaneState.HALTED.value  # no automated step shares the profile
    with pytest.raises(Exception, match="Wait for the login"):
        app.setup.accounts.start_check("brand", by="operator")
    finish.set()
    await app.setup.accounts._jobs["brand"].task
    row = app.setup.accounts.accounts()[0]
    assert row["job"]["state"] == "ok" and row["lane_state"] == LaneState.ACTIVE.value
    assert row["verified_via"] == "login" and seen == ["lemmedeliver"]
    with pytest.raises(Exception, match="research"):
        app.setup.accounts.start_login("research", by="operator")


async def test_test_login_is_read_only_and_stops_on_a_checkpoint(make_app, settings, clock) -> None:
    app = live_app(make_app, settings, clock)
    world = app.world
    settings.account.username = next(iter(world.accounts))  # a profile the simulated Instagram knows
    sends = world.sends
    app.setup.accounts.start_check("brand", by="operator")
    await app.setup.accounts._jobs["brand"].task
    row = app.setup.accounts.accounts()[0]
    assert row["job"]["state"] == "ok" and row["verified_via"] == "check" and world.sends == sends

    world.faults.checkpoint_after_browser_ops = world.browser_ops
    app.setup.accounts.start_check("brand", by="operator")
    await app.setup.accounts._jobs["brand"].task
    row = app.setup.accounts.accounts()[0]
    assert row["job"]["state"] == "failed" and "confirm it's you" in row["job"]["message"]
    assert row["lane_state"] == LaneState.HALTED.value  # stopped for a human, like any real step
    assert app.control.incidents()


async def test_no_browser_work_on_a_real_account_before_its_first_login(make_app, settings, clock) -> None:
    app = live_app(make_app, settings, clock)
    await run_ticks(app, clock, 6)
    assert app.world.browser_ops == 0  # it would only have found Instagram's login page
    lane = next(lane for lane in app.monitor.overview()["lanes"] if lane["channel"] == "BROWSER")
    assert lane["needs_login"] and lane["state"] == "ACTIVE"  # nothing went wrong: no incident, no alert
    waiting = [a["status_reason"] for a in app.control.list_actions(limit=50) if a["type"] == "DISCOVER"]
    assert waiting and all("not logged in on this computer" in reason for reason in waiting)
    assert app.control.incidents() == []
    logged_in(settings)
    await run_ticks(app, clock, 6)
    assert app.world.browser_ops > 0
    assert not next(lane for lane in app.monitor.overview()["lanes"] if lane["channel"] == "BROWSER")["needs_login"]


def test_simulation_has_nothing_to_log_in_to(make_app) -> None:
    client = client_for(make_app())
    response = client.post("/api/accounts/brand/login", headers=AUTH)
    assert response.status_code == 400 and "simulation" in response.json()["detail"]


# ------------------------------------------------------ home page and numbers
async def test_home_page_numbers_add_up(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 60)
    client = client_for(app)
    d = client.get("/api/dashboard", headers=AUTH).json()
    f = app.monitor.overview()["funnel"]
    k = d["kpis"]
    assert k["found"]["total"] == f["found"] > 0 and k["messaged"]["total"] == f["messaged"] > 0
    assert k["good_fit"]["total"] == f["good_fit"] and k["replied"]["total"] == f["replied"]
    assert all(0 <= v["week"] <= v["total"] for v in k.values())
    assert d["latest"] and all(item["status"] in ActionStatus.__members__ for item in d["latest"])
    assert sum(c["found"] for c in d["categories"]) == f["found"]
    assert [t["score"] for t in d["top"]] == sorted((t["score"] for t in d["top"]), reverse=True)

    a = client.get("/api/analytics", params={"days": 14}, headers=AUTH).json()
    assert len(a["days"]) == 14 and a["days"][-1]["date"] > a["days"][0]["date"]
    funnel = {s["stage"]: s["count"] for s in a["funnel"]}
    assert funnel["found"] >= funnel["good_fit"] >= funnel["messaged"] >= funnel["replied"]
    assert sum(day["messaged"] for day in a["days"]) <= funnel["messaged"]
    assert sum(g["found"] for g in a["by_source"]) == funnel["found"]
    assert a["rates"]["reply"] == pytest.approx(funnel["replied"] / funnel["messaged"], abs=0.001)


async def test_sales_stage_moves_a_lead_to_client(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 60)
    client = client_for(app)
    with app.db.session() as session:
        handle = session.scalars(select(Lead.username).where(Lead.status == LeadStatus.CONTACTED)).first()
    assert handle
    before = client.get("/api/dashboard", headers=AUTH).json()["kpis"]
    lead = client.put(f"/api/leads/{handle}/stage", json={"stage": "won", "note": "paid"}, headers=AUTH).json()
    assert lead["stage"] == "WON" and lead["stage_auto"] is False
    after = client.get("/api/dashboard", headers=AUTH).json()["kpis"]
    assert after["clients"] == {"total": 1, "week": 1}
    assert after["interested"]["total"] == before["interested"]["total"] + 1
    listed = next(x for x in client.get("/api/leads?limit=500", headers=AUTH).json() if x["username"] == handle)
    assert listed["stage"] == "WON" and listed["found_at"]
    assert client.put(f"/api/leads/{handle}/stage", json={"stage": "maybe"}, headers=AUTH).status_code == 400
    client.put(f"/api/leads/{handle}/stage", json={"stage": None}, headers=AUTH)
    assert client.get("/api/dashboard", headers=AUTH).json()["kpis"]["clients"]["total"] == 0
    titles = [i["title"] for i in app.monitor.feed(limit=500)["items"] if i["kind"] == "lead.stage"]
    assert titles[0] == f"You marked @{handle}: client"


async def test_find_businesses_now(make_app, clock) -> None:
    app = make_app()
    client = client_for(app)
    first = client.post("/api/discovery/run", headers=AUTH).json()
    assert first["planned"] > 0 and "Queued" in first["message"]
    planned = [first["planned"]]
    while planned[-1]:  # each press queues the next searches, up to the daily search limit
        planned.append(client.post("/api/discovery/run", headers=AUTH).json()["planned"])
    assert sum(planned) == app.runtime.limits().discovery_runs_per_day
    assert "Nothing new" in client.post("/api/discovery/run", headers=AUTH).json()["message"]
    app.runtime.set_paused(True, "test")
    assert client.post("/api/discovery/run", headers=AUTH).status_code == 400
    app.runtime.set_paused(False, "test")
    for campaign in app.settings.campaigns:
        campaign.enabled = False
    response = client.post("/api/discovery/run", headers=AUTH)
    assert response.status_code == 400 and "Campaigns" in response.json()["detail"]
    text, shown = plain_event("discovery.requested", "discovery", "", {"planned": 3})
    assert shown and text == "You asked it to look for new businesses now (3 searches queued)"


def test_campaigns_page_round_trip(file_app) -> None:
    client = client_for(file_app)
    page = client.get("/api/campaigns", headers=AUTH).json()
    names = {s["name"] for s in page["strategies"]}
    assert {"keyword_search", "hashtag", "suggested_accounts", "location"} <= names and page["can_edit"]
    campaigns = page["campaigns"]
    campaigns.append(
        {
            "id": "thane-salons",
            "enabled": True,
            "niches": ["salon"],
            "locations": ["Thane"],
            "min_score": "65",
            "strategies": [
                {"name": "keyword_search", "enabled": True, "params": {"extra_queries": "bridal makeup thane\n"}},
                {"name": "location", "enabled": True, "params": {"locations": ["Thane West | "]}},
            ],
        }
    )
    saved = client.put("/api/campaigns", json={"campaigns": campaigns}, headers=AUTH).json()
    added = next(c for c in saved["campaigns"] if c["id"] == "thane-salons")
    assert added["min_score"] == 65
    assert added["strategies"][0]["params"]["extra_queries"] == ["bridal makeup thane"]
    assert added["strategies"][1]["params"]["locations"] == [{"name": "Thane West"}]
    assert [c.id for c in file_app.settings.campaigns][-1] == "thane-salons"  # the planner sees it now
    bad = [dict(added, strategies=[{"name": "post_engagers", "params": {"posts": ["https://example.com/p/1"]}}])]
    assert client.put("/api/campaigns", json={"campaigns": bad}, headers=AUTH).status_code == 400
