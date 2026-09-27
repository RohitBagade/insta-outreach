"""A separate research account: it searches and reads profiles, the brand account only sends.

The two lanes stop independently: a checkpoint on the research account pauses
finding leads but never sending, and vice versa. Research work never falls back
onto the brand account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from insta_outreach.cli import _browser_account
from insta_outreach.domain.enums import (
    ActionType,
    Capability,
    Channel,
    Environment,
    ExecutionStatus,
    LaneState,
    OperatingMode,
)
from insta_outreach.domain.models import OperationRequest
from insta_outreach.execution.base import OperationContext
from insta_outreach.execution.executor import DEFAULT_ROUTES
from insta_outreach.execution.research import RESEARCH_CAPABILITIES, ResearchAdapter, research_routes
from insta_outreach.execution.simulator import SimulatedBrowserAdapter, SimulatedWorld
from insta_outreach.orchestrator.control import ControlError
from insta_outreach.orchestrator.readiness import blocking
from insta_outreach.policy import usage as u
from insta_outreach.storage.models import Action, ActionAttempt
from tests.conftest import run_ticks

RESEARCH_TYPES = (ActionType.DISCOVER, ActionType.INSPECT_PROFILE)
SEND_TYPES = (ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY)


def with_research(settings: Any) -> Any:
    settings.research.enabled = True
    settings.research.account.username = "lemmedeliver.research"
    return settings


def attempts(app: Any, after_id: int = 0) -> list[tuple[int, ActionType, Channel | None]]:
    with app.db.session() as session:
        rows = session.execute(
            select(ActionAttempt.id, Action.type, ActionAttempt.channel)
            .join(Action, Action.id == ActionAttempt.action_id)
            .where(ActionAttempt.id > after_id)
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def lane_state(app: Any, channel: Channel) -> LaneState:
    with app.db.session() as session:
        return app.lanes.snapshot(session, app.settings.account.id, channel).state


def test_routes_move_only_research_work() -> None:
    routes = research_routes(DEFAULT_ROUTES)
    assert routes[Capability.SEARCH_ACCOUNTS] == (Channel.RESEARCH,)
    assert routes[Capability.HASHTAG_POSTS] == (Channel.RESEARCH,)
    assert routes[Capability.INSPECT_PROFILE] == (Channel.API, Channel.RESEARCH)  # never the brand account
    assert routes[Capability.SEND_NEW_DM] == (Channel.BROWSER,)
    assert routes[Capability.READ_INBOX] == (Channel.API, Channel.BROWSER)  # its own chats stay with the brand
    assert not any(Channel.BROWSER in routes[c] for c in RESEARCH_CAPABILITIES)


def test_off_by_default(make_app) -> None:
    app = make_app()
    assert Channel.RESEARCH not in app.executor.adapters
    assert app.executor.candidate_channels(Capability.SEARCH_ACCOUNTS) == [Channel.BROWSER]


async def test_research_adapter_is_read_only(clock) -> None:
    world = SimulatedWorld.seeded(clock, 7)
    research = ResearchAdapter(SimulatedBrowserAdapter(world))
    assert research.channel is Channel.RESEARCH
    assert research.capabilities() == RESEARCH_CAPABILITIES
    send = OperationRequest(
        action_id="act_x", account_id="lemmedeliver", capability=Capability.SEND_NEW_DM, target_username="sim.x"
    )
    assert not research.supports(send)
    outcome = await research.execute(send, OperationContext(clock=clock))
    assert outcome.status is ExecutionStatus.NOT_PERMITTED and outcome.channel is Channel.RESEARCH
    assert world.sends == 0


async def test_research_account_searches_brand_account_sends(make_app, settings, clock) -> None:
    app = make_app(settings=with_research(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 40)
    rows = attempts(app)
    research = [ch for _, kind, ch in rows if kind in RESEARCH_TYPES]
    sends = [ch for _, kind, ch in rows if kind in SEND_TYPES]
    assert Channel.RESEARCH in research and Channel.BROWSER not in research
    assert sends and Channel.RESEARCH not in sends
    with app.db.session() as session:  # each account has its own page-view budget
        since = datetime(2000, 1, 1, tzinfo=UTC)
        research_views = app.ledger.total(session, app.settings.account.id, [u.PAGE_VIEW], since, Channel.RESEARCH)
    assert research_views > 0
    overview = app.monitor.overview()
    assert {"channel": "RESEARCH", "configured": True}.items() <= next(
        lane for lane in overview["lanes"] if lane["channel"] == "RESEARCH"
    ).items()
    assert overview["usage"]["research_on"] is True


async def test_checkpoint_on_research_pauses_finding_not_sending(make_app, settings, clock) -> None:
    app = make_app(settings=with_research(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 12)
    world = app.world
    world.faults.checkpoint_after_browser_ops = world.browser_ops
    world.faults.checkpoint_channel = Channel.RESEARCH
    last = max((i for i, _, _ in attempts(app)), default=0)
    sends_before = world.sends
    await run_ticks(app, clock, 40)

    assert lane_state(app, Channel.RESEARCH) is LaneState.HALTED
    assert lane_state(app, Channel.BROWSER) is LaneState.ACTIVE
    assert world.sends > sends_before  # @lemmedeliver keeps sending to leads already found
    later = attempts(app, last)
    assert not [1 for _, kind, ch in later if kind in RESEARCH_TYPES and ch is Channel.BROWSER]  # no fallback


async def test_checkpoint_on_brand_stops_sending_not_finding(make_app, settings, clock) -> None:
    app = make_app(settings=with_research(settings))
    app.runtime.set_mode(OperatingMode.AUTONOMOUS, "test")
    await run_ticks(app, clock, 12)
    world = app.world
    world.faults.checkpoint_after_browser_ops = world.browser_ops
    world.faults.checkpoint_channel = Channel.BROWSER
    await run_ticks(app, clock, 6)
    assert lane_state(app, Channel.BROWSER) is LaneState.HALTED
    last = max(i for i, _, _ in attempts(app))
    sends = world.sends
    clock.advance(days=1)  # a new day of searching (the daily search budget is spent early)
    await run_ticks(app, clock, 30)

    assert lane_state(app, Channel.RESEARCH) is LaneState.ACTIVE
    assert world.sends == sends  # nothing is sent while the brand account needs its owner
    assert [1 for _, kind, ch in attempts(app, last) if kind in RESEARCH_TYPES and ch is Channel.RESEARCH]


def test_live_research_account_gets_its_own_browser_profile(make_app, settings) -> None:
    settings.environment = Environment.LIVE
    settings.browser.enabled = True
    settings.research.enabled = True
    with pytest.raises(ValueError, match=r"research\.account\.username"):
        make_app(settings=settings, adapters=None)
    settings.research.account.username = "lemmedeliver.research"
    app = make_app(settings=settings, adapters=None)
    brand, research = app.executor.adapters[Channel.BROWSER], app.executor.adapters[Channel.RESEARCH]
    assert research.session.profile_dir != brand.session.profile_dir
    assert research.session.profile_dir.name == "research"
    checks = {c.name: c for c in app.control.preflight()}
    assert checks["research_session"].mark == "WARN"  # finding leads needs it; sending does not
    assert "research_session" not in {c.name for c in blocking(list(checks.values()))}
    assert "browser login --account research" in checks["research_session"].detail


def test_login_command_picks_the_account(settings) -> None:
    with pytest.raises(ControlError):
        _browser_account(settings, "research")
    chosen, channel = _browser_account(with_research(settings), "research")
    assert channel is Channel.RESEARCH and chosen.account.username == "lemmedeliver.research"
    assert _browser_account(settings, "brand") == (settings, Channel.BROWSER)


def test_session_records_are_per_account(make_app) -> None:
    app = make_app()
    app.runtime.record_browser_session("login", by="test", channel=Channel.RESEARCH)
    assert app.runtime.browser_session(Channel.RESEARCH)["via"] == "login"
    assert app.runtime.browser_session() == {}  # the brand account is still unverified
