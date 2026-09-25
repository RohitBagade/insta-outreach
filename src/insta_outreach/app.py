"""Composition root: builds the object graph for LOCAL or LIVE environments."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from insta_outreach.config import Settings
from insta_outreach.conversations.ownership import OwnershipService
from insta_outreach.domain.enums import Channel, Environment
from insta_outreach.domain.models import InboundEvent
from insta_outreach.execution.base import InstagramAdapter
from insta_outreach.execution.executor import InstagramExecutor
from insta_outreach.execution.simulator import (
    SimulatedApiAdapter,
    SimulatedBrowserAdapter,
    SimulatedWebsiteChecker,
    SimulatedWorld,
)
from insta_outreach.intelligence.analyzer import LeadAnalyzer
from insta_outreach.intelligence.website import HttpWebsiteChecker, WebsiteChecker
from insta_outreach.llm import AnthropicLLM, StructuredLLM
from insta_outreach.notify import FanoutNotifier, LogNotifier, Notifier, TelegramNotifier, WebhookNotifier
from insta_outreach.orchestrator.actions import ActionService
from insta_outreach.orchestrator.control import ControlService
from insta_outreach.orchestrator.monitor import MonitorService
from insta_outreach.orchestrator.pipeline import IdentityResolver, Pipeline, Services
from insta_outreach.orchestrator.service import EventSource, Orchestrator
from insta_outreach.orchestrator.worker import ExecutionWorker
from insta_outreach.personalization.composer import MessageComposer
from insta_outreach.personalization.validator import MessageValidator
from insta_outreach.policy.gate import EligibilityGate
from insta_outreach.policy.incidents import IncidentService
from insta_outreach.policy.lanes import LaneService
from insta_outreach.policy.usage import UsageLedger
from insta_outreach.runtime import RuntimeControl
from insta_outreach.storage.db import Database
from insta_outreach.util.clock import Clock, SystemClock
from insta_outreach.webhooks import WebhookInbox

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    db: Database
    clock: Clock
    runtime: RuntimeControl
    services: Services
    pipeline: Pipeline
    worker: ExecutionWorker
    orchestrator: Orchestrator
    control: ControlService
    monitor: MonitorService
    lanes: LaneService
    incidents: IncidentService
    ledger: UsageLedger
    executor: InstagramExecutor
    webhook_inbox: WebhookInbox
    notifier: Notifier
    llm: StructuredLLM
    world: SimulatedWorld | None = None

    async def close(self) -> None:
        await self.executor.close()
        self.db.dispose()


def build_notifier(settings: Settings) -> FanoutNotifier:
    """Log always; plus the webhook and/or Telegram when configured."""
    cfg = settings.notifications
    extra: list[Notifier] = []
    if cfg.webhook_url:
        extra.append(WebhookNotifier(cfg.webhook_url, cfg.min_severity))
    if cfg.telegram_bot_token is not None and cfg.telegram_chat_id:
        extra.append(
            TelegramNotifier(
                cfg.telegram_bot_token.get_secret_value(), cfg.telegram_chat_id, cfg.min_severity, cfg.dashboard_url
            )
        )
    return FanoutNotifier(LogNotifier(), *extra)


def _live_adapters(
    settings: Settings, clock: Clock, llm: StructuredLLM, db: Database
) -> tuple[dict[Channel, InstagramAdapter], IdentityResolver | None]:
    adapters: dict[Channel, InstagramAdapter] = {}
    resolver: IdentityResolver | None = None
    if settings.api.configured:
        from insta_outreach.execution.api.adapter import GraphApiAdapter
        from insta_outreach.execution.api.client import GraphApiClient

        client = GraphApiClient(settings.api)
        adapters[Channel.API] = GraphApiAdapter(client, settings.api, clock)

        async def resolve(igsid: str) -> str | None:
            profile = await client.get_user_profile(igsid)
            return profile.get("username")

        resolver = resolve
    if settings.browser.enabled:
        from insta_outreach.execution.browser.adapter import PlaywrightBrowserAdapter

        adapters[Channel.BROWSER] = PlaywrightBrowserAdapter(settings.browser, settings.account, clock, llm, db)
    if not adapters:
        log.warning(
            "LIVE environment with no adapters configured: nothing will be executed "
            "(set api.enabled + credentials and/or browser.enabled)"
        )
    return adapters, resolver


def build_app(
    settings: Settings,
    *,
    clock: Clock | None = None,
    llm: StructuredLLM | None = None,
    world: SimulatedWorld | None = None,
    adapters: dict[Channel, InstagramAdapter] | None = None,
    website_checker: WebsiteChecker | None = None,
    notifier: Notifier | None = None,
    seed: int | None = None,
) -> App:
    clock = clock or SystemClock()
    rng = random.Random(seed if seed is not None else settings.simulation.seed)
    db = Database(settings.resolved_database_url)
    db.create_all()
    if notifier is None:
        notifier = build_notifier(settings)
    incidents = IncidentService(clock, notifier)
    lanes = LaneService(clock, incidents)
    ledger = UsageLedger(clock)
    runtime = RuntimeControl(db, settings, clock)
    llm = llm or AnthropicLLM(settings.llm)
    validator = MessageValidator(settings.offer)
    webhook_inbox = WebhookInbox(db, clock, settings.api.ig_user_id)

    identity_resolver: IdentityResolver | None = None
    event_source: EventSource | None = None
    if settings.environment is Environment.LOCAL:
        world = world or SimulatedWorld.seeded(clock, settings.simulation.seed)
        if adapters is None:
            adapters = {
                Channel.API: SimulatedApiAdapter(world, business_discovery=settings.simulation.api_business_discovery),
                Channel.BROWSER: SimulatedBrowserAdapter(world),
            }
        website_checker = website_checker or SimulatedWebsiteChecker(world)
        sim_world = world

        async def resolve_sim(igsid: str) -> str | None:
            return sim_world.igsid_to_username(igsid)

        async def sim_events() -> list[InboundEvent]:
            if settings.simulation.prospects_reply:
                sim_world.run_prospect_behaviour()
            return sim_world.drain_events() + webhook_inbox.drain()

        identity_resolver, event_source = resolve_sim, sim_events
    else:
        if adapters is None:
            adapters, identity_resolver = _live_adapters(settings, clock, llm, db)
        website_checker = website_checker or HttpWebsiteChecker(clock, settings.scoring.website_check_timeout_seconds)

        async def webhook_events() -> list[InboundEvent]:
            return webhook_inbox.drain()

        event_source = webhook_events

    executor = InstagramExecutor(
        adapters=adapters,
        lanes=lanes,
        db=db,
        clock=clock,
        ledger=ledger,
        limits=runtime.limits,
        evidence_dir=settings.browser.evidence_dir,
        rng=rng,
    )
    services = Services(
        settings=settings,
        db=db,
        clock=clock,
        runtime=runtime,
        actions=ActionService(clock),
        analyzer=LeadAnalyzer(settings.scoring, settings.offer),
        website_checker=website_checker,
        composer=MessageComposer(settings.offer, llm, validator, settings.llm),
        gate=EligibilityGate(settings, clock, ledger, lanes, rng),
        executor=executor,
        ownership=OwnershipService(clock, settings.ownership),
        incidents=incidents,
        rng=rng,
        identity_resolver=identity_resolver,
    )
    pipeline = Pipeline(services)
    worker = ExecutionWorker(services, pipeline, ledger, lanes)
    orchestrator = Orchestrator(services, pipeline, worker, event_source)
    control = ControlService(services, lanes, validator)
    return App(
        settings=settings,
        db=db,
        clock=clock,
        runtime=runtime,
        services=services,
        pipeline=pipeline,
        worker=worker,
        orchestrator=orchestrator,
        control=control,
        monitor=MonitorService(services, lanes, ledger),
        lanes=lanes,
        incidents=incidents,
        ledger=ledger,
        executor=executor,
        webhook_inbox=webhook_inbox,
        notifier=notifier,
        llm=llm,
        world=world,
    )


def describe(app: App) -> dict[str, Any]:
    return {
        "environment": app.settings.environment.value,
        "adapters": {c.value: type(a).__name__ for c, a in app.executor.adapters.items()},
        "llm": type(app.llm).__name__ if app.llm.available else "unavailable (template fallback)",
    }
