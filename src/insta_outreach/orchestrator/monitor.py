"""Read-only views for the live dashboard (Mission Control).

Everything here is derived from the same database the orchestrator writes:
the pipeline counts, limit usage (computed exactly like the gate computes it),
and a live feed that merges the audit trail with every executor attempt
(discovery, inspection, inbox reads), so nothing the agent does is hidden.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Channel,
    Environment,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
    LeadStatus,
)
from insta_outreach.orchestrator.pipeline import Services
from insta_outreach.policy import usage as u
from insta_outreach.policy.lanes import LaneService
from insta_outreach.storage.models import (
    Action,
    ActionAttempt,
    AuditEvent,
    Conversation,
    DiscoveryRun,
    Incident,
    Lead,
    Suppression,
)
from insta_outreach.util.clock import in_window, local_day_start, next_window_start

_SEND_TYPES = (ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY)
_RETRY_STATUSES = (ExecutionStatus.RETRYABLE_FAILURE, ExecutionStatus.UI_CHANGED)

# Which workflow node an audit event belongs to (the dashboard pulses it).
_NODE_BY_KIND = (
    ("lead.added", "discover"),
    ("action.proposed", "draft"),
    ("action.blocked", "gate"),
    ("action.cancelled", "gate"),
    ("action.demoted", "gate"),
    ("action.approved", "gate"),
    ("action.rejected", "gate"),
    ("message.sent", "send"),
    ("send.", "send"),
    ("lane.", "send"),
    ("incident.", "send"),
    ("browser.", "send"),
    ("reply.", "conversation"),
    ("conversation.", "conversation"),
    ("suppression.", "conversation"),
)
_GOOD = ("message.sent", "lane.resumed", "incident.resolved", "browser.session_verified", "conversation.released")
_WARN = ("action.blocked", "action.cancelled", "action.demoted", "send.not_sent", "lane.cooldown", "mode.refused")
_CRITICAL = ("lane.halted", "send.stopped")


def _node_for(kind: str) -> str | None:
    return next((node for prefix, node in _NODE_BY_KIND if kind.startswith(prefix)), None)


def _tone_for(kind: str, detail: dict[str, Any]) -> str:
    if kind.startswith(_CRITICAL) or (kind == "incident.opened" and "[CRITICAL]" in str(detail.get("summary", ""))):
        return "critical"
    if kind.startswith(_WARN) or kind == "incident.opened":
        return "warning"
    if kind.startswith(_GOOD):
        return "good"
    return "info"


class MonitorService:
    def __init__(self, services: Services, lanes: LaneService, ledger: u.UsageLedger) -> None:
        self.s = services
        self._lanes = lanes
        self._ledger = ledger
        self._account = services.settings.account.id

    # ------------------------------------------------------------------ helpers
    @property
    def _tz(self) -> str:
        return self.s.settings.schedule.timezone

    def _local(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        local = value.astimezone(ZoneInfo(self._tz))
        return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "")

    # ----------------------------------------------------------------- overview
    def overview(self) -> dict[str, Any]:
        now = self.s.clock.now()
        settings = self.s.settings
        day_start = local_day_start(now, self._tz)
        with self.s.db.session() as session:
            lanes = [
                {
                    "channel": lane.channel.value,
                    "state": lane.state.value,
                    "until": lane.until.isoformat() if lane.until else None,
                    "until_local": self._local(lane.until),
                    "reason": lane.reason,
                    "configured": lane.channel in self.s.executor.adapters,
                }
                for lane in self._lanes.all_snapshots(session, self._account)
            ]
            leads = {k.value: v for k, v in session.execute(select(Lead.status, func.count()).group_by(Lead.status))}
            sends = {
                (t.value, st.value): n
                for t, st, n in session.execute(
                    select(Action.type, Action.status, func.count())
                    .where(Action.type.in_(_SEND_TYPES))
                    .group_by(Action.type, Action.status)
                )
            }
            sent_today = {
                (ch.value if ch else "?"): n
                for ch, n in session.execute(
                    select(Action.executed_channel, func.count())
                    .where(
                        Action.type.in_(_SEND_TYPES),
                        Action.status == ActionStatus.SUCCEEDED,
                        Action.completed_at >= day_start,
                    )
                    .group_by(Action.executed_channel)
                )
            }
            queued = session.scalars(
                select(Action).where(Action.type.in_(_SEND_TYPES), Action.status == ActionStatus.APPROVED)
            ).all()
            waits: Counter[str] = Counter()
            earliest: datetime | None = None
            for action in queued:
                decision = (action.gate or {}).get("execution") or {}
                if decision.get("outcome") == "DEFER":
                    waits.update(decision.get("reasons", []))
                if action.not_before and (earliest is None or action.not_before < earliest):
                    earliest = action.not_before
            incidents = session.execute(
                select(Incident.severity, func.count())
                .where(Incident.status == IncidentStatus.OPEN)
                .group_by(Incident.severity)
            ).all()
            human_owned = session.scalar(
                select(func.count()).select_from(Conversation).where(Conversation.automation_paused.is_(True))
            )
            suppressed = session.scalar(select(func.count()).select_from(Suppression))
            failed_today = session.scalar(
                select(func.count())
                .select_from(Action)
                .where(
                    Action.type.in_(_SEND_TYPES),
                    Action.status == ActionStatus.FAILED,
                    Action.completed_at >= day_start,
                )
            )
            cursor = {
                "audit": session.scalar(select(func.max(AuditEvent.id))) or 0,
                "attempt": session.scalar(select(func.max(ActionAttempt.id))) or 0,
            }

        def count(action_type: ActionType | None, status: ActionStatus) -> int:
            return sum(
                n
                for (t, st), n in sends.items()
                if st == status.value and (action_type is None or t == action_type.value)
            )

        total_leads = sum(leads.values())
        open_by_severity = {sev.value: n for sev, n in incidents}
        return {
            "environment": settings.environment.value,
            "simulated": settings.environment is Environment.LOCAL,
            "account": settings.account.username,
            "mode": self.s.runtime.mode().value,
            "paused": self.s.runtime.paused(),
            "now": now.isoformat(),
            "now_local": self._local(now),
            "timezone": self._tz,
            "lanes": lanes,
            "pipeline": {
                "discover": {"total": total_leads, "waiting": leads.get(LeadStatus.DISCOVERED.value, 0)},
                "analyze": {
                    "analyzed": total_leads - leads.get(LeadStatus.DISCOVERED.value, 0),
                    "not_qualified": leads.get(LeadStatus.ANALYZED.value, 0),
                    "disqualified": leads.get(LeadStatus.DISQUALIFIED.value, 0),
                    "duplicate": leads.get(LeadStatus.DUPLICATE.value, 0),
                },
                "qualify": {"qualified": leads.get(LeadStatus.QUALIFIED.value, 0)},
                "draft": {
                    "drafted": count(None, ActionStatus.DRAFTED),
                    "pending_approval": count(None, ActionStatus.PENDING_APPROVAL),
                    "generated": sum(sends.values()),
                },
                "gate": {
                    "queued": len(queued),
                    "blocked": count(None, ActionStatus.BLOCKED),
                    "parked": count(None, ActionStatus.NEEDS_HUMAN),
                    "executing": count(None, ActionStatus.EXECUTING),
                    "waiting_for": [{"reason": r, "count": n} for r, n in waits.most_common(3)],
                    "next_attempt_local": self._local(earliest),
                },
                "send": {
                    "sent_today": sum(sent_today.values()),
                    "sent_today_by_channel": sent_today,
                    "sent_total": count(None, ActionStatus.SUCCEEDED),
                    "followups_total": count(ActionType.SEND_FOLLOW_UP, ActionStatus.SUCCEEDED),
                    "failed_today": failed_today or 0,
                },
                "conversation": {
                    "contacted": leads.get(LeadStatus.CONTACTED.value, 0),
                    "replied": leads.get(LeadStatus.REPLIED.value, 0),
                    "handed_off": leads.get(LeadStatus.HANDED_OFF.value, 0),
                    "closed": leads.get(LeadStatus.CLOSED.value, 0),
                    "unreachable": leads.get(LeadStatus.UNREACHABLE.value, 0),
                    "suppressed": suppressed or 0,
                },
            },
            "usage": self.usage(),
            "attention": {
                "pending_approvals": count(None, ActionStatus.PENDING_APPROVAL) + count(None, ActionStatus.DRAFTED),
                "open_incidents": sum(open_by_severity.values()),
                "critical_incidents": open_by_severity.get(IncidentSeverity.CRITICAL.value, 0),
                "halted_lanes": [lane["channel"] for lane in lanes if lane["state"] == "HALTED"],
                "human_owned": human_owned or 0,
            },
            "cursor": cursor,
        }

    # -------------------------------------------------------------------- usage
    def usage(self) -> dict[str, Any]:
        """Limit usage, computed the way the eligibility gate computes it."""
        now = self.s.clock.now()
        limits = self.s.runtime.limits()
        schedule = self.s.settings.schedule
        day_start = local_day_start(now, self._tz)
        hour_ago = now - timedelta(hours=1)
        ledger, account = self._ledger, self._account
        with self.s.db.session() as session:
            last_send = ledger.last_at(session, account, u.SEND_KINDS)
            data: dict[str, Any] = {
                "outreach_today": ledger.total(session, account, [u.SEND_OUTREACH], day_start),
                "outreach_per_day": limits.outreach_per_day,
                "outreach_last_hour": ledger.total(session, account, [u.SEND_OUTREACH], hour_ago),
                "outreach_per_hour": limits.outreach_per_hour,
                "followups_today": ledger.total(session, account, [u.SEND_FOLLOWUP], day_start),
                "followups_per_day": limits.followups_per_day,
                "replies_last_hour": ledger.total(session, account, [u.SEND_REPLY], hour_ago),
                "replies_per_hour": limits.replies_per_hour,
                "browser_units_last_hour": ledger.total(session, account, [u.PAGE_VIEW], hour_ago, Channel.BROWSER),
                "browser_units_per_hour": limits.browser_units_per_hour,
                "inspections_today": ledger.total(session, account, [u.INSPECTION], day_start),
                "profile_inspections_per_day": limits.profile_inspections_per_day,
            }
        earliest = last_send + timedelta(seconds=limits.min_seconds_between_sends) if last_send else None
        data |= {
            "last_send_local": self._local(last_send),
            "next_send_earliest_local": self._local(earliest) if earliest and earliest > now else None,
            "min_seconds_between_sends": limits.min_seconds_between_sends,
            "send_jitter_seconds": limits.send_jitter_seconds,
            "send_hours": "-".join(schedule.send_hours),
            "in_send_hours": in_window(now, self._tz, schedule.send_hours),
            "next_send_window_local": self._local(next_window_start(now, self._tz, schedule.send_hours)),
            "browser_hours": "-".join(schedule.browser_active_hours),
            "in_browser_hours": in_window(now, self._tz, schedule.browser_active_hours),
        }
        return data

    # --------------------------------------------------------------------- feed
    def feed(self, after_audit: int | None = None, after_attempt: int | None = None, limit: int = 80) -> dict[str, Any]:
        """New audit events + executor attempts since the cursors (latest ``limit`` if none)."""
        limit = max(1, min(limit, 500))
        with self.s.db.session() as session:
            audit_q = select(AuditEvent)
            if after_audit is not None:
                audit_q = audit_q.where(AuditEvent.id > after_audit).order_by(AuditEvent.id).limit(limit)
            else:
                audit_q = audit_q.order_by(AuditEvent.id.desc()).limit(limit)
            events = session.scalars(audit_q).all()
            attempt_q = select(ActionAttempt, Action).join(Action, Action.id == ActionAttempt.action_id)
            if after_attempt is not None:
                attempt_q = attempt_q.where(ActionAttempt.id > after_attempt).order_by(ActionAttempt.id).limit(limit)
            else:
                attempt_q = attempt_q.order_by(ActionAttempt.id.desc()).limit(limit)
            attempts = session.execute(attempt_q).all()
            runs = {
                r.action_id: r
                for r in session.scalars(
                    select(DiscoveryRun).where(DiscoveryRun.action_id.in_([a.id for _, a in attempts]))
                )
            }
            items = [self._audit_item(e) for e in events]
            items += [
                item
                for attempt, action in attempts
                if (item := self._attempt_item(attempt, action, runs.get(action.id))) is not None
            ]
            cursor = {
                "audit": max([after_audit or 0, *(e.id for e in events)]),
                "attempt": max([after_attempt or 0, *(a.id for a, _ in attempts)]),
            }
            if after_audit is None and after_attempt is None:
                cursor = {
                    "audit": session.scalar(select(func.max(AuditEvent.id))) or 0,
                    "attempt": session.scalar(select(func.max(ActionAttempt.id))) or 0,
                }
        items.sort(key=lambda i: (i["at"], i["source"] == "audit", i["id"]))
        return {"items": items[-limit:], "cursor": cursor}

    def _audit_item(self, e: AuditEvent) -> dict[str, Any]:
        subject = e.subject or ""
        return {
            "source": "audit",
            "id": e.id,
            "at": e.at.isoformat(),
            "at_local": self._local(e.at),
            "kind": e.kind,
            "actor": e.actor,
            "subject": subject,
            "handle": subject[1:] if subject.startswith("@") else None,
            "summary": e.summary,
            "tone": _tone_for(e.kind, {"summary": e.summary}),
            "node": _node_for(e.kind),
        }

    def _attempt_item(self, t: ActionAttempt, a: Action, run: DiscoveryRun | None) -> dict[str, Any] | None:
        if a.type in _SEND_TYPES and t.status not in _RETRY_STATUSES:
            return None  # sends and their outcomes are already in the audit trail
        params = a.params or {}
        channel = t.channel.value if t.channel else "-"
        if a.type is ActionType.DISCOVER:
            what = params.get("query") or params.get("tag") or a.target_username or params.get("location") or ""
            label = f"discovery [{a.capability.value}] {what}".strip()
            node = "discover"
        elif a.type is ActionType.INSPECT_PROFILE:
            label, node = f"inspected @{a.target_username}", "analyze"
        elif a.type is ActionType.SYNC_INBOX:
            label, node = "read the inbox", "conversation"
        elif a.type is ActionType.SYNC_THREAD:
            label, node = f"read the thread with @{a.target_username}", "conversation"
        else:
            label, node = f"{a.type.value} @{a.target_username} (retry)", "send"
        ok = t.status is ExecutionStatus.SUCCESS
        outcome = "ok" if ok else f"{t.status.value} {t.code or ''}".strip()
        if run is not None and ok:
            outcome = f"found {run.found}, {run.new_leads} new"
        tone = "neutral" if ok else ("critical" if t.status.is_barrier else "warning")
        return {
            "source": "exec",
            "id": t.id,
            "at": t.finished_at.isoformat(),
            "at_local": self._local(t.finished_at),
            "kind": f"exec.{a.type.value.lower()}",
            "actor": channel,
            "subject": f"@{a.target_username}" if a.target_username else "",
            "handle": a.target_username if a.type is not ActionType.DISCOVER else None,
            "summary": f"{label} via {channel}: {outcome}",
            "tone": tone,
            "node": node,
        }
