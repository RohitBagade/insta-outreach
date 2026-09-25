"""Incidents: barriers and anomalies surfaced for human intervention."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import Channel, IncidentSeverity, IncidentStatus
from insta_outreach.notify import Notifier
from insta_outreach.policy.audit import audit
from insta_outreach.storage.models import Incident
from insta_outreach.util.clock import Clock


@dataclass
class _Pending:
    title: str
    detail: str
    severity: IncidentSeverity
    data: dict[str, Any] = field(default_factory=dict)


class IncidentService:
    """Creates/resolves incidents. Notifications are flushed after commit."""

    def __init__(self, clock: Clock, notifier: Notifier) -> None:
        self._clock = clock
        self._notifier = notifier
        self._pending: list[_Pending] = []

    def open(
        self,
        session: Session,
        *,
        account_id: str,
        channel: Channel | None,
        severity: IncidentSeverity,
        kind: str,
        title: str,
        detail: str = "",
        action_id: str | None = None,
        page_url: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> Incident:
        existing = session.scalars(
            select(Incident).where(
                Incident.account_id == account_id,
                Incident.channel == channel,
                Incident.kind == kind,
                Incident.status == IncidentStatus.OPEN,
            )
        ).first()
        if existing is not None:
            # Same barrier hit again while still open: enrich, don't spam.
            existing.detail = detail or existing.detail
            existing.evidence = [*(existing.evidence or []), *(evidence or [])][-20:]
            existing.page_url = page_url or existing.page_url
            return existing
        incident = Incident(
            account_id=account_id,
            channel=channel,
            severity=severity,
            status=IncidentStatus.OPEN,
            kind=kind,
            title=title,
            detail=detail,
            action_id=action_id,
            page_url=page_url,
            evidence=evidence or [],
            created_at=self._clock.now(),
        )
        session.add(incident)
        session.flush()
        audit(
            session,
            incident.created_at,
            actor="system",
            kind="incident.opened",
            subject=f"lane:{channel.value}" if channel else None,
            summary=f"incident #{incident.id} [{severity.value}] {title}",
            incident_id=incident.id,
            incident_kind=kind,
            page_url=page_url,
            action_id=action_id,
            evidence=[e.get("path") for e in (evidence or []) if e.get("path")],
        )
        self._pending.append(
            _Pending(
                title=title,
                detail=detail,
                severity=severity,
                data={
                    "incident_id": incident.id,
                    "account": account_id,
                    "channel": channel.value if channel else None,
                    "kind": kind,
                    "action_id": action_id,
                    "page_url": page_url,
                },
            )
        )
        return incident

    def resolve_for_lane(self, session: Session, account_id: str, channel: Channel | None, by: str, note: str) -> int:
        query = select(Incident).where(Incident.account_id == account_id, Incident.status == IncidentStatus.OPEN)
        if channel is not None:
            query = query.where(Incident.channel == channel)
        incidents = session.scalars(query).all()
        for incident in incidents:
            self._mark_resolved(session, incident, by, note)
        return len(incidents)

    def resolve(self, session: Session, incident_id: int, by: str, note: str) -> Incident | None:
        incident = session.get(Incident, incident_id)
        if incident is not None and incident.status is IncidentStatus.OPEN:
            self._mark_resolved(session, incident, by, note)
        return incident

    def _mark_resolved(self, session: Session, incident: Incident, by: str, note: str) -> None:
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = self._clock.now()
        incident.resolved_by = by
        incident.resolution_note = note
        audit(
            session,
            incident.resolved_at,
            actor=by,
            kind="incident.resolved",
            subject=f"lane:{incident.channel.value}" if incident.channel else None,
            summary=f"incident #{incident.id} resolved by {by}: {note}",
            incident_id=incident.id,
        )

    def notify_later(self, title: str, detail: str, severity: IncidentSeverity, **data: Any) -> None:
        self._pending.append(_Pending(title=title, detail=detail, severity=severity, data=data))

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        for item in pending:
            await self._notifier.notify(item.title, item.detail, item.severity, item.data)
