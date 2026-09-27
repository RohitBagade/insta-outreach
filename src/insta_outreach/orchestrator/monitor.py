"""Read-only views for the live dashboard (Mission Control).

Everything here is derived from the same database the orchestrator writes:
the pipeline counts, limit usage (computed exactly like the gate computes it),
and a live feed that merges the audit trail with every executor attempt
(discovery, inspection, inbox reads), so nothing the agent does is hidden.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    Channel,
    Environment,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
    LeadStatus,
    MessageDirection,
    SalesStage,
)
from insta_outreach.orchestrator.control import INTERESTED_INTENTS
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
    LeadSource,
    LeadStage,
    Message,
    Suppression,
)
from insta_outreach.util.clock import in_window, local_day_start, next_window_start

_SEND_TYPES = (ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY)
_RETRY_STATUSES = (ExecutionStatus.RETRYABLE_FAILURE, ExecutionStatus.UI_CHANGED)
# Checked and worth a message (whatever happened after).
GOOD_FIT = frozenset(
    {
        LeadStatus.QUALIFIED,
        LeadStatus.OUTREACH_PENDING,
        LeadStatus.CONTACTED,
        LeadStatus.REPLIED,
        LeadStatus.HANDED_OFF,
        LeadStatus.CLOSED,
        LeadStatus.UNREACHABLE,
    }
)
_OPEN_SENDS = (ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED, ActionStatus.APPROVED, ActionStatus.EXECUTING)

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
_GOOD = (
    "message.sent",
    "lane.resumed",
    "incident.resolved",
    "browser.session_verified",
    "conversation.released",
    "action.approved",
)
_WARN = ("action.blocked", "action.cancelled", "action.demoted", "send.not_sent", "lane.cooldown", "mode.refused")
_CRITICAL = ("lane.halted", "send.stopped")


_CHANNEL_NAME = {"BROWSER": "browser", "API": "official API", "RESEARCH": "research account"}
_STOP_REASON = {
    "CHECKPOINT_REQUIRED": "Instagram asked to confirm it's you",
    "LOGIN_REQUIRED": "Instagram logged the account out",
    "ACCOUNT_RESTRICTED": "Instagram restricted the account",
    "HUMAN_ACTION_REQUIRED": "Instagram is asking for something only you can do",
    "UI_CHANGED": "Instagram's page looked different than expected",
    "RATE_LIMITED": "Instagram said 'try again later' twice in a day",
}
_NOT_SENT_REASON = {
    "ALREADY_CONTACTED": "you have already chatted with them",
    "TARGET_NOT_FOUND": "their profile or message button wasn't found",
    "RETRYABLE_FAILURE": "it kept failing, so it gave up",
}


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def plain_event(kind: str, subject: str, summary: str, detail: dict[str, Any]) -> tuple[str, bool]:
    """A short plain-English line for Mission Control's simple view, and whether it
    belongs there (False: technical detail, shown only with "technical details" on)."""
    who = subject if subject.startswith("@") else "them"
    channel_key = subject.removeprefix("lane:")
    channel = _CHANNEL_NAME.get(channel_key, channel_key.lower())
    if kind == "message.sent":
        message_kind = detail.get("message_kind") or summary.split(" ", 1)[0]
        if detail.get("capability") == "private_reply":
            return f"Replied privately to {who}'s comment", True
        if message_kind == "followup":
            return f"Sent a follow-up to {who}", True
        if message_kind == "reply":
            return f"Replied to {who}", True
        return f"Sent a first message to {who}", True
    if kind == "send.stopped":
        reason = _STOP_REASON.get(str(detail.get("status")), "Instagram stopped it")
        return f"Couldn't message {who}: {reason}. It will wait for you", True
    if kind == "send.not_sent":
        reason = _NOT_SENT_REASON.get(str(detail.get("status")), "it didn't go through")
        return f"Didn't message {who}: {reason}", True
    if kind == "reply.handled":
        intent = str(detail.get("intent") or "")
        quote = _clip(detail.get("reply"), 80)
        said = f": \u201c{quote}\u201d" if quote else ""
        if intent in ("INTERESTED", "QUESTION"):
            return f"{who} replied{said}. Your turn: they're waiting for you", True
        if intent in ("OPT_OUT", "NOT_INTERESTED"):
            return f"{who} said no{said}. They won't be messaged again", True
        return f"{who} replied{said}. Have a look", True
    if kind == "conversation.human_owned":
        reason = str(detail.get("reason") or "")
        if reason.startswith("handed off"):
            return f"The chat with {who} is now yours; the bot stepped back", False
        if reason.startswith("claimed by"):
            return f"You took over the chat with {who}", True
        return f"You messaged {who} yourself, so the bot leaves that chat to you", True
    if kind == "conversation.released":
        return f"You handed the chat with {who} back to the bot", True
    if kind == "action.proposed":
        status = str(detail.get("status") or "")
        if status == "PENDING_APPROVAL":
            return f"A message for {who} is waiting for your approval", True
        if status == "DRAFTED":
            return f"Wrote a draft for {who} (Draft mode: nothing is sent)", True
        return f"Queued a message for {who}", False
    if kind == "action.approved":
        return f"You approved the message to {who}" + (" (with your edit)" if detail.get("edited") else ""), True
    if kind == "action.rejected":
        return f"You rejected the message to {who}", True
    if kind == "lead.added":
        note = _clip(detail.get("note"), 120)
        return f"You added {who}" + (f" (note: {note})" if note else ""), True
    if kind == "lane.halted":
        if detail.get("status") is None:  # a person stopped it
            return f"The {channel} was stopped by hand: {_clip(detail.get('reason'), 80) or 'no reason given'}", True
        reason = _STOP_REASON.get(str(detail.get("status")), "Instagram stopped it")
        return f"{reason}. The {channel} is stopped until you fix it and press Resume", True
    if kind == "lane.cooldown":
        return f"Instagram said 'try again later', so the {channel} is resting for a while", True
    if kind == "lane.resumed":
        return f"The {channel} is running again", True
    if kind == "mode.changed":
        return f"Mode changed to {str(detail.get('mode') or '').title() or 'a new mode'}", True
    if kind == "mode.refused":
        return "Autonomous mode was refused: the go-live checks don't pass yet", True
    if kind == "pause.changed":
        return ("Everything is paused" if detail.get("paused") else "Resumed after a pause"), True
    if kind in ("limits.changed", "limits.cleared"):
        return ("Sending limits were changed" if kind == "limits.changed" else "Sending limits reset"), True
    if kind == "suppression.added":
        return f"{detail.get('value') or who} is now on the never-contact list", True
    if kind == "suppression.removed":
        return f"{subject.split(':', 1)[-1]} was removed from the never-contact list", True
    if kind == "browser.session_verified":
        research = channel_key == "RESEARCH"
        if detail.get("via") == "login":
            return ("Logged in to the research account" if research else "Logged in to Instagram"), True
        return f"Checked {'the research account' if research else 'the Instagram'} login: it works", True
    if kind == "settings.changed":
        labels = [str(label) for label in detail.get("labels") or []]
        later = " (after a restart)" if detail.get("restart_required") else ""
        return "You changed settings: " + (", ".join(labels) or "settings") + later, True
    if kind == "alerts.changed":
        return "Phone alerts were set up", True
    if kind == "discovery.requested":
        planned = int(detail.get("planned") or 0)
        return "You asked it to look for new businesses now" + (
            f" ({planned} search{'es' if planned != 1 else ''} queued)" if planned else " (nothing new to search yet)"
        ), True
    if kind == "lead.stage":
        stage = str(detail.get("stage") or "")
        return (
            f"You marked {who}: {_STAGE_WORDS.get(stage, stage.lower())}" if stage else f"You cleared {who}'s stage"
        ), True
    return summary, False  # incidents mirror lane events; blocks, cancellations: technical


_STAGE_WORDS = {
    "INTERESTED": "interested",
    "MEETING": "meeting booked",
    "PROPOSAL": "proposal sent",
    "WON": "client",
    "LOST": "not now",
}


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
        self.started = datetime.now(UTC).isoformat()  # tells the page when the program restarted

    # ------------------------------------------------------------------ helpers
    @property
    def _tz(self) -> str:
        return self.s.settings.schedule.timezone

    def local(self, value: datetime | None) -> str | None:
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
                    "until_local": self.local(lane.until),
                    "reason": lane.reason,
                    "configured": lane.channel in self.s.executor.adapters,
                    "needs_login": lane.channel in self.s.executor.adapters
                    and settings.first_login_pending(lane.channel),
                }
                for lane in self._lanes.all_snapshots(session, self._account, self.s.executor.adapters)
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
            messaged = session.scalar(select(func.count()).select_from(Lead).where(Lead.contacted_at.is_not(None)))
            replied = session.scalar(select(func.count()).select_from(Lead).where(Lead.replied_at.is_not(None)))
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
        not_yet = {  # found but (so far) not a lead worth a message
            "checking": leads.get(LeadStatus.DISCOVERED.value, 0),
            "not_fit": leads.get(LeadStatus.DISQUALIFIED.value, 0),
            "duplicates": leads.get(LeadStatus.DUPLICATE.value, 0),
            "no_need": leads.get(LeadStatus.ANALYZED.value, 0),
        }
        return {
            "environment": settings.environment.value,
            "simulated": settings.environment is Environment.LOCAL,
            "account": settings.account.username,
            "mode": self.s.runtime.mode().value,
            "paused": self.s.runtime.paused(),
            "now": now.isoformat(),
            "now_local": self.local(now),
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
                    "next_attempt_local": self.local(earliest),
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
            # The same numbers as a plain funnel, for the simple view.
            "funnel": {
                "found": total_leads,
                "good_fit": total_leads - sum(not_yet.values()),
                "messaged": messaged or 0,
                "replied": replied or 0,
                "with_you": human_owned or 0,
                "waiting_approval": count(None, ActionStatus.PENDING_APPROVAL) + count(None, ActionStatus.DRAFTED),
                "queued": len(queued),
                **not_yet,
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
            "started": self.started,
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
                "research_on": Channel.RESEARCH in self.s.executor.adapters,
                "research_units_last_hour": ledger.total(session, account, [u.PAGE_VIEW], hour_ago, Channel.RESEARCH),
                "browser_units_per_hour": limits.browser_units_per_hour,
                "inspections_today": ledger.total(session, account, [u.INSPECTION], day_start),
                "profile_inspections_per_day": limits.profile_inspections_per_day,
            }
        earliest = last_send + timedelta(seconds=limits.min_seconds_between_sends) if last_send else None
        data |= {
            "last_send_local": self.local(last_send),
            "next_send_earliest_local": self.local(earliest) if earliest and earliest > now else None,
            "min_seconds_between_sends": limits.min_seconds_between_sends,
            "send_jitter_seconds": limits.send_jitter_seconds,
            "send_hours": "-".join(schedule.send_hours),
            "in_send_hours": in_window(now, self._tz, schedule.send_hours),
            "next_send_window_local": self.local(next_window_start(now, self._tz, schedule.send_hours)),
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
        title, important = plain_event(e.kind, subject, e.summary, e.detail or {})
        return {
            "source": "audit",
            "id": e.id,
            "at": e.at.isoformat(),
            "at_local": self.local(e.at),
            "kind": e.kind,
            "actor": e.actor,
            "subject": subject,
            "handle": subject[1:] if subject.startswith("@") else None,
            "summary": e.summary,
            "title": title,
            "important": important,
            "tone": _tone_for(e.kind, {"summary": e.summary}),
            "node": _node_for(e.kind),
        }

    def _attempt_item(self, t: ActionAttempt, a: Action, run: DiscoveryRun | None) -> dict[str, Any] | None:
        if a.type in _SEND_TYPES and t.status not in _RETRY_STATUSES:
            return None  # sends and their outcomes are already in the audit trail
        params = a.params or {}
        channel = t.channel.value if t.channel else "-"
        where = ""  # the discovery source, in plain words
        if a.type is ActionType.DISCOVER:
            what = params.get("query") or params.get("tag") or a.target_username or params.get("location") or ""
            label = f"discovery [{a.capability.value}] {what}".strip()
            node = "discover"
            where = {
                Capability.HASHTAG_POSTS: f"#{what}",
                Capability.SEARCH_ACCOUNTS: f"searching '{what}'",
                Capability.SUGGESTED_ACCOUNTS: f"accounts similar to @{what}",
            }.get(a.capability, str(what))
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
        title, important = label, False
        if run is not None and ok:
            outcome = f"found {run.found}, {run.new_leads} new"
            if run.new_leads:  # the only agent chore worth a line in the simple view
                noun = "business" if run.new_leads == 1 else "businesses"
                title, important = f"Found {run.new_leads} new {noun}" + (f" via {where}" if where else ""), True
        tone = "neutral" if ok else ("critical" if t.status.is_barrier else "warning")
        return {
            "source": "exec",
            "id": t.id,
            "at": t.finished_at.isoformat(),
            "at_local": self.local(t.finished_at),
            "kind": f"exec.{a.type.value.lower()}",
            "actor": channel,
            "subject": f"@{a.target_username}" if a.target_username else "",
            "handle": a.target_username if a.type is not ActionType.DISCOVER else None,
            "summary": f"{label} via {channel}: {outcome}",
            "title": title,
            "important": important,
            "tone": tone,
            "node": node,
        }

    # ---------------------------------------------------------------- dashboard
    def dashboard(self) -> dict[str, Any]:
        """The home page: totals with the last 7 days, the latest messages, the best
        prospects, businesses by category, and chats waiting on Rohit."""
        now = self.s.clock.now()
        week_ago = now - timedelta(days=7)
        with self.s.db.session() as session:
            facts = _Facts.load(session)
            latest = session.execute(
                select(Action, Lead)
                .outerjoin(Lead, Lead.id == Action.lead_id)
                .where(Action.type.in_(_SEND_TYPES), Action.status != ActionStatus.PROPOSED)
                .order_by(func.coalesce(Action.completed_at, Action.created_at).desc(), Action.id)
                .limit(12)
            ).all()
            top_leads = session.scalars(
                select(Lead)
                .where(Lead.status.in_([LeadStatus.QUALIFIED, LeadStatus.OUTREACH_PENDING]))
                .order_by(Lead.score.desc().nulls_last(), Lead.id)
                .limit(6)
            ).all()
            open_actions = {
                a.lead_id: a
                for a in session.scalars(
                    select(Action)
                    .where(
                        Action.lead_id.in_([lead.id for lead in top_leads]),
                        Action.type == ActionType.SEND_OUTREACH,
                        Action.status.in_(_OPEN_SENDS),
                    )
                    .order_by(Action.created_at)
                )
            }
            awaiting = self._awaiting_you(session)

        def kpi(times: list[datetime | None]) -> dict[str, int]:
            present = [t for t in times if t is not None]
            return {"total": len(present), "week": sum(1 for t in present if t >= week_ago)}

        leads = facts.leads
        return {
            "kpis": {
                "found": kpi([row.created_at for row in leads]),
                "good_fit": kpi([row.analyzed_at or row.created_at for row in leads if row.status in GOOD_FIT]),
                "messaged": kpi([row.contacted_at for row in leads]),
                "replied": kpi([row.replied_at for row in leads]),
                "interested": kpi(list(facts.interested().values())),
                "clients": kpi(list(facts.clients().values())),
            },
            "latest": [self._message_item(action, lead) for action, lead in latest],
            "top": [
                {
                    "handle": lead.username,
                    "full_name": lead.full_name,
                    "score": lead.score,
                    "niche": lead.niche,
                    "location": lead.location_match,
                    "followers": lead.followers,
                    "why": lead.status_reason,
                    "opportunities": [o.get("type") for o in lead.opportunities or [] if isinstance(o, dict)],
                    "action_id": open_actions[lead.id].id if lead.id in open_actions else None,
                    "action_status": open_actions[lead.id].status.value if lead.id in open_actions else None,
                    "message": open_actions[lead.id].message_text if lead.id in open_actions else None,
                }
                for lead in top_leads
            ],
            "categories": facts.breakdown(lambda row: row.niche or "other"),
            "awaiting_you": awaiting,
        }

    def _message_item(self, action: Action, lead: Lead | None) -> dict[str, Any]:
        kind = {
            ActionType.SEND_OUTREACH: "first message",
            ActionType.SEND_FOLLOW_UP: "follow-up",
            ActionType.SEND_REPLY: "reply",
        }[action.type]
        if action.capability is Capability.PRIVATE_REPLY:
            kind = "private reply"
        return {
            "id": action.id,
            "handle": action.target_username,
            "kind": kind,
            "status": action.status.value,
            "at_local": self.local(action.completed_at or action.created_at),
            "preview": _clip(action.message_text, 160),
            "score": lead.score if lead else None,
            "niche": lead.niche if lead else None,
        }

    @staticmethod
    def _awaiting_you(session: Any) -> int:
        """Chats Rohit owns where the prospect wrote last."""
        paused = select(Conversation.id).where(Conversation.automation_paused.is_(True))
        last_ids = session.scalars(
            select(func.max(Message.id)).where(Message.conversation_id.in_(paused)).group_by(Message.conversation_id)
        ).all()
        return int(
            session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.id.in_(last_ids), Message.direction == MessageDirection.INBOUND)
            )
            or 0
        )

    # ---------------------------------------------------------------- analytics
    def analytics(self, days: int = 14) -> dict[str, Any]:
        days = max(7, min(days, 90))
        now = self.s.clock.now()
        zone = ZoneInfo(self._tz)
        today = now.astimezone(zone).date()
        dates = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
        start = datetime.combine(dates[0], time(0), tzinfo=zone)
        with self.s.db.session() as session:
            facts = _Facts.load(session)
            followups = session.scalars(
                select(Action.completed_at).where(
                    Action.type == ActionType.SEND_FOLLOW_UP,
                    Action.status == ActionStatus.SUCCEEDED,
                    Action.completed_at >= start,
                )
            ).all()
            source: dict[int, str] = {}
            for lead_id, strategy in session.execute(
                select(LeadSource.lead_id, LeadSource.strategy).order_by(LeadSource.discovered_at, LeadSource.id)
            ):
                source.setdefault(lead_id, strategy)

        def per_day(times: list[datetime | None]) -> dict[date, int]:
            counts: Counter[date] = Counter()
            for t in times:
                if t is not None and t >= start:
                    counts[t.astimezone(zone).date()] += 1
            return counts

        leads = facts.leads
        interested, clients = facts.interested(), facts.clients()
        series = {
            "found": per_day([row.created_at for row in leads]),
            "messaged": per_day([row.contacted_at for row in leads]),
            "followups": per_day(list(followups)),
            "replied": per_day([row.replied_at for row in leads]),
            "interested": per_day(list(interested.values())),
        }
        funnel = {
            "found": len(leads),
            "good_fit": sum(1 for row in leads if row.status in GOOD_FIT),
            "messaged": sum(1 for row in leads if row.contacted_at),
            "replied": sum(1 for row in leads if row.replied_at),
            "interested": len(interested),
            "clients": len(clients),
        }

        def rate(part: int, whole: int, scale: int = 1) -> float | None:
            return round(part * scale / whole, 3) if whole else None

        return {
            "days": [
                {"date": d.isoformat(), "label": f"{d.day} {d.strftime('%b')}"}
                | {k: v.get(d, 0) for k, v in series.items()}
                for d in dates
            ],
            "funnel": [{"stage": stage, "count": count} for stage, count in funnel.items()],
            "rates": {
                "good_fit": rate(funnel["good_fit"], funnel["found"]),
                "reply": rate(funnel["replied"], funnel["messaged"]),
                "interested_per_100": rate(funnel["interested"], funnel["messaged"], 100),
                "clients_per_100": rate(funnel["clients"], funnel["messaged"], 100),
            },
            "by_niche": facts.breakdown(lambda row: row.niche or "other"),
            "by_source": facts.breakdown(lambda row: source.get(row.id, "unknown")),
            "timezone": self._tz,
        }


@dataclass(frozen=True)
class _LeadRow:
    id: int
    niche: str | None
    status: LeadStatus
    created_at: datetime
    analyzed_at: datetime | None
    contacted_at: datetime | None
    replied_at: datetime | None


@dataclass
class _Facts:
    """Every lead's funnel timestamps, plus interest and sales stages, in one read."""

    leads: list[_LeadRow]
    stages: dict[int, tuple[SalesStage, datetime]]
    interested_replies: dict[int, datetime]  # lead id -> first interested/question reply

    @classmethod
    def load(cls, session: Any) -> _Facts:
        leads = [
            _LeadRow(*row)
            for row in session.execute(
                select(
                    Lead.id,
                    Lead.niche,
                    Lead.status,
                    Lead.created_at,
                    Lead.analyzed_at,
                    Lead.contacted_at,
                    Lead.replied_at,
                )
            )
        ]
        stages = {row.lead_id: (row.stage, row.updated_at) for row in session.scalars(select(LeadStage))}
        replies = dict(
            session.execute(
                select(Conversation.lead_id, func.min(Message.created_at))
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Message.direction == MessageDirection.INBOUND,
                    Message.intent.in_(INTERESTED_INTENTS),
                    Conversation.lead_id.is_not(None),
                )
                .group_by(Conversation.lead_id)
            ).all()
        )
        return cls(leads, stages, replies)

    def interested(self) -> dict[int, datetime]:
        """Leads interested now (a stage Rohit set that isn't LOST, else an interested reply), with since when."""
        found = {lead_id: at for lead_id, at in self.interested_replies.items() if lead_id not in self.stages}
        found |= {lead_id: at for lead_id, (stage, at) in self.stages.items() if stage.still_interested}
        return found

    def clients(self) -> dict[int, datetime]:
        return {lead_id: at for lead_id, (stage, at) in self.stages.items() if stage is SalesStage.WON}

    def breakdown(self, key: Any) -> list[dict[str, Any]]:
        interested = self.interested()
        groups: dict[str, dict[str, Any]] = {}
        for row in self.leads:
            name = str(key(row))
            group = groups.setdefault(
                name, {"name": name, "found": 0, "good_fit": 0, "messaged": 0, "replied": 0, "interested": 0}
            )
            group["found"] += 1
            group["good_fit"] += row.status in GOOD_FIT
            group["messaged"] += row.contacted_at is not None
            group["replied"] += row.replied_at is not None
            group["interested"] += row.id in interested
        return sorted(groups.values(), key=lambda g: (-g["found"], g["name"]))
