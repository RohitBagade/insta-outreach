"""The orchestrator loop: one tick = inbound events -> planning -> execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from insta_outreach.domain.enums import ActionStatus, ActionType, IncidentStatus, LeadStatus, OperatingMode
from insta_outreach.domain.models import InboundEvent
from insta_outreach.orchestrator.pipeline import Pipeline, Services
from insta_outreach.orchestrator.worker import ExecutionWorker, summarize
from insta_outreach.storage.models import Action, Incident, Lead, UsageEvent, WebhookEvent
from insta_outreach.util.files import prune_dated_folders

log = logging.getLogger(__name__)

EventSource = Callable[[], Awaitable[list[InboundEvent]]]


@dataclass
class TickReport:
    mode: str
    events: dict[str, int] = field(default_factory=dict)
    discovery_planned: int = 0
    inspections_planned: int = 0
    outreach_prepared: int = 0
    followups_prepared: int = 0
    replies_handled: int = 0
    inbox_syncs_planned: int = 0
    executed: list[dict[str, Any]] = field(default_factory=list)
    maintenance: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Orchestrator:
    def __init__(
        self, services: Services, pipeline: Pipeline, worker: ExecutionWorker, event_source: EventSource | None = None
    ) -> None:
        self.s = services
        self.pipeline = pipeline
        self.worker = worker
        self._event_source = event_source
        self._last_retention: datetime | None = None

    def maintenance(self) -> dict[str, int]:
        limits = self.s.runtime.limits()
        now = self.s.clock.now()
        with self.s.db.session() as session:
            recovered = self.s.actions.recover_stale(session)
            expired_ids = list(
                session.scalars(
                    select(Action.lead_id).where(
                        Action.type == ActionType.SEND_OUTREACH,
                        Action.status.in_([ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED]),
                        Action.created_at < now - timedelta(hours=limits.approval_ttl_hours),
                    )
                )
            )
            expired = self.s.actions.expire_stale_approvals(session, limits.approval_ttl_hours)
            if expired_ids:
                # Unapproved drafts expire; the lead becomes eligible for a fresh draft.
                session.execute(
                    update(Lead)
                    .where(Lead.id.in_([i for i in expired_ids if i]), Lead.status == LeadStatus.OUTREACH_PENDING)
                    .values(status=LeadStatus.QUALIFIED, status_reason="previous draft expired")
                )
            released = self.s.ownership.release_expired_holds(session)
            pruned = session.execute(delete(UsageEvent).where(UsageEvent.at < now - timedelta(days=8)))
        return {
            "recovered": recovered,
            "expired": expired,
            "holds_released": released,
            "usage_pruned": int(getattr(pruned, "rowcount", 0) or 0),
        } | self.retention()

    def retention(self, force: bool = False) -> dict[str, int]:
        """Delete old raw webhook payloads and evidence (at most once a day)."""
        now = self.s.clock.now()
        if not force and self._last_retention is not None and now - self._last_retention < timedelta(days=1):
            return {}
        self._last_retention = now
        cfg = self.s.settings.retention
        with self.s.db.session() as session:
            webhooks = session.execute(
                delete(WebhookEvent).where(
                    WebhookEvent.processed_at.is_not(None),
                    WebhookEvent.received_at < now - timedelta(days=cfg.webhook_payload_days),
                )
            )
            cited = {
                Path(item["path"]).parent.name
                for incident in session.scalars(select(Incident).where(Incident.status == IncidentStatus.OPEN))
                for item in incident.evidence or []
                if isinstance(item, dict) and item.get("path")
            }
        # Evidence folders are named by UTC date (see EvidenceRecorder).
        cutoff = (now - timedelta(days=cfg.evidence_days)).date()
        folders = prune_dated_folders(self.s.settings.browser.evidence_dir, cutoff, keep=cited)
        return {"webhooks_pruned": int(getattr(webhooks, "rowcount", 0) or 0), "evidence_folders_pruned": folders}

    async def tick(self) -> TickReport:
        mode = self.s.runtime.mode()
        report = TickReport(mode=mode.value)
        report.maintenance = self.maintenance()
        if self._event_source is not None:
            events = await self._event_source()
            if events:
                report.events = await self.pipeline.process_events(events)
        paused = self.s.runtime.paused()
        if not paused:
            report.discovery_planned = self.pipeline.plan_discovery()
            report.inspections_planned = self.pipeline.plan_inspections()
            report.outreach_prepared = await self.pipeline.plan_outreach(mode)
            report.followups_prepared = await self.pipeline.plan_followups(mode)
            report.inbox_syncs_planned = self.pipeline.plan_inbox_sync()
        report.replies_handled = await self.pipeline.plan_replies(mode)
        results = await self.worker.run_once(mode, self.s.settings.schedule.max_actions_per_tick)
        report.executed = summarize(results)
        await self.s.incidents.flush()
        return report

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info(
            "orchestrator started (environment=%s, mode=%s)",
            self.s.settings.environment.value,
            self.s.runtime.mode().value,
        )
        while not stop.is_set():
            try:
                report = await self.tick()
                if report.executed or report.events:
                    log.info("tick: %s", {k: v for k, v in report.as_dict().items() if v})
            except Exception:  # one bad tick must not stop the loop
                log.exception("tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.s.settings.schedule.tick_seconds)

    @property
    def mode(self) -> OperatingMode:
        return self.s.runtime.mode()
