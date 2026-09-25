"""Live rollout guards: AUTONOMOUS prerequisites, sandbox allowlist, manual leads, audit trail."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from insta_outreach.cli import main
from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    Channel,
    Environment,
    IncidentSeverity,
    LeadStatus,
    OperatingMode,
    SuppressionKind,
)
from insta_outreach.orchestrator.control import ControlError
from insta_outreach.orchestrator.readiness import blocking
from insta_outreach.storage.models import AuditEvent
from tests.conftest import run_ticks


class FakeBrowser:
    """Stands in for a configured live browser lane (never executes here)."""

    channel = Channel.BROWSER
    simulated = False

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.SEND_NEW_DM, Capability.SEND_DM_REPLY, Capability.INSPECT_PROFILE})

    def supports(self, request: Any) -> bool:
        return True

    async def execute(self, request: Any, ctx: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("must not execute in this test")

    async def close(self) -> None:
        return None


def live(settings: Any) -> Any:
    settings.environment = Environment.LIVE
    return settings


def failing(app: Any) -> set[str]:
    return {c.name for c in blocking(app.control.preflight())}


def test_live_autonomous_refused_without_credentials_session_or_history(make_app, settings) -> None:
    app = make_app(settings=live(settings), adapters={})
    assert failing(app) == {"control_token", "send_lane", "approved_sends"}
    with pytest.raises(ControlError) as refused:
        app.control.set_mode(OperatingMode.AUTONOMOUS, by="rohit", confirm=True)
    assert "AUTONOMOUS refused" in str(refused.value) and "control_token" in str(refused.value)
    assert app.runtime.mode() is OperatingMode.OBSERVE  # unchanged
    assert app.control.audit_events("mode.refused")[0]["actor"] == "rohit"  # the attempt is on record
    # The lower modes never send on their own and stay available.
    for mode in (OperatingMode.DRAFT, OperatingMode.APPROVAL, OperatingMode.OBSERVE):
        app.control.set_mode(mode, by="rohit")


def test_live_autonomous_needs_every_check_to_pass(make_app, settings, tmp_path: Path) -> None:
    settings = live(settings)
    settings.control_api.token = SecretStr("s3cret")
    settings.browser.enabled = True
    app = make_app(settings=settings, adapters={Channel.BROWSER: FakeBrowser()})
    assert failing(app) == {"browser_session", "approved_sends"}

    profile = Path(settings.browser.profiles_dir) / settings.account.id
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"x")
    app.runtime.record_browser_session("login", by="cli")
    assert failing(app) == {"approved_sends"}

    with app.db.session() as session:
        for n in range(3):
            app.services.actions.create(
                session,
                key=f"outreach:lemmedeliver:test{n}:v1",
                account_id="lemmedeliver",
                type=ActionType.SEND_OUTREACH,
                capability=Capability.SEND_NEW_DM,
                status=ActionStatus.SUCCEEDED,
                mode=OperatingMode.APPROVAL,
                target_username=f"rohit.test{n}",
                message_text="hello",
                approved_by="human:rohit",
            )
    assert failing(app) == set()

    with app.db.session() as session:
        app.incidents.open(
            session,
            account_id="lemmedeliver",
            channel=Channel.BROWSER,
            severity=IncidentSeverity.CRITICAL,
            kind="CHECKPOINT_REQUIRED",
            title="checkpoint",
        )
    assert "incidents" in failing(app)
    with app.db.session() as session:
        app.incidents.resolve_for_lane(session, "lemmedeliver", Channel.BROWSER, "rohit", "done")

    with pytest.raises(ControlError, match="confirmation"):
        app.control.set_mode(OperatingMode.AUTONOMOUS, by="rohit")
    assert app.control.set_mode(OperatingMode.AUTONOMOUS, by="rohit", confirm=True)["mode"] == "AUTONOMOUS"


def _live_config(tmp_path: Path) -> str:
    config = tmp_path / "live.yaml"
    config.write_text(f"environment: live\ndata_dir: {tmp_path / 'livedata'}\n", encoding="utf-8")
    return str(config)


def test_cli_refuses_live_autonomous_and_run_will_not_start(tmp_path: Path, capsys) -> None:
    config = _live_config(tmp_path)
    assert main(["--config", config, "mode", "AUTONOMOUS", "--confirm"]) == 2
    assert "AUTONOMOUS refused" in capsys.readouterr().err
    assert main(["--config", config, "preflight"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "AUTONOMOUS would be REFUSED" in out

    # Even if the stored mode is AUTONOMOUS (set behind the CLI's back), `run` refuses to start.
    from insta_outreach.app import build_app
    from insta_outreach.config import load_settings

    app = build_app(load_settings(config))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, by="test")
    app.db.dispose()
    assert main(["--config", config, "run", "--no-api"]) == 2
    assert "REFUSING TO START" in capsys.readouterr().err


async def test_sandbox_allowlist_keeps_outbound_on_listed_handles(make_app, settings, clock) -> None:
    settings.rollout.allowed_targets = ["@sim.smileline.dental"]
    app = make_app(settings=settings)
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 40)
    assert app.world.sent_log and {m["to"] for m in app.world.sent_log} == {"sim.smileline.dental"}
    # Everyone else stays QUALIFIED (not disqualified), ready for when the sandbox is lifted.
    assert len(app.control.leads([LeadStatus.QUALIFIED])) >= 3


async def test_manual_lead_goes_through_the_normal_pipeline(make_app, settings, clock) -> None:
    settings.campaigns = []
    app = make_app(settings=settings)
    app.runtime.set_mode(OperatingMode.DRAFT, "test")
    added = app.control.add_lead("@sim.the.brew.room", by="cli", note="my test account")
    assert added["created"] and added["status"] == LeadStatus.DISCOVERED.value
    with pytest.raises(ControlError, match="simulated"):
        app.control.add_lead("real.business", by="cli")
    await run_ticks(app, clock, 6)
    lead = app.control.lead_detail("sim.the.brew.room")
    assert lead["status"] == LeadStatus.OUTREACH_PENDING.value
    assert lead["sources"][0]["strategy"] == "manual"
    assert [a["status"] for a in app.control.list_actions() if a["type"] == "SEND_OUTREACH"] == ["DRAFTED"]
    assert app.world.sent_log == []


async def test_audit_trail_records_operator_and_system_decisions(make_app, settings, clock) -> None:
    app = make_app(settings=settings)
    app.runtime.set_mode(OperatingMode.APPROVAL, "rohit")
    app.runtime.set_limit_overrides({"outreach_per_day": 5}, "rohit")
    await run_ticks(app, clock, 30)
    first, second = app.control.list_actions([ActionStatus.PENDING_APPROVAL])[:2]
    app.control.approve(first["id"], by="rohit")
    app.control.reject(second["id"], by="rohit", reason="tone", redraft=True)
    app.control.suppress(SuppressionKind.USERNAME, "sim.bombay.bao", "asked in person", by="rohit")
    await run_ticks(app, clock, 6)
    app.control.claim_conversation(first["target_username"], by="rohit")
    app.control.release_conversation(first["target_username"], by="rohit")
    with app.db.session() as session:
        kinds = {e.kind for e in session.scalars(select(AuditEvent))}
        sent = session.scalars(select(AuditEvent).where(AuditEvent.kind == "message.sent")).one()
    assert {
        "mode.changed",
        "limits.changed",
        "action.proposed",
        "action.approved",
        "action.rejected",
        "suppression.added",
        "message.sent",
        "conversation.human_owned",
        "conversation.released",
    } <= kinds
    assert sent.actor == "human:rohit" and sent.subject == f"@{first['target_username']}"


def test_dotenv_loader_never_overrides_the_environment(tmp_path: Path, monkeypatch) -> None:
    from insta_outreach.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text('# secrets\nCONTROL_API_TOKEN="abc123"\nexport IG_USER_ID=1784\nIG_APP_SECRET=\n', encoding="utf-8")
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    monkeypatch.setenv("IG_USER_ID", "from-shell")
    assert load_dotenv(env) == ["CONTROL_API_TOKEN"]
    import os

    assert os.environ["CONTROL_API_TOKEN"] == "abc123" and os.environ["IG_USER_ID"] == "from-shell"
    monkeypatch.delenv("CONTROL_API_TOKEN")
