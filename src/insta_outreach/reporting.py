"""Human-readable views of the database for the CLI (and the verification scenario).

Every view is read-only and deterministic: rows are ordered by explicit keys
and times are shown in the configured timezone (IST by default).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    ExecutionStatus,
    IncidentStatus,
    LeadStatus,
    SuppressionKind,
)
from insta_outreach.orchestrator.readiness import Check
from insta_outreach.policy.lanes import _HALT_MESSAGES
from insta_outreach.storage.models import (
    Action,
    ActionAttempt,
    AuditEvent,
    Conversation,
    Incident,
    Lead,
    LeadSource,
    Message,
    Suppression,
)

if TYPE_CHECKING:
    from insta_outreach.app import App


# ----------------------------------------------------------------- formatting
def when(value: datetime | str | None, tz: str) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    local = value.astimezone(ZoneInfo(tz))
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or tz)


def clip(text: Any, width: int) -> str:
    value = " ".join(str(text if text is not None else "").split())
    return value if len(value) <= width else value[: max(0, width - 1)] + "…"


def table(headers: list[str], rows: list[list[Any]], widths: dict[int, int] | None = None) -> list[str]:
    """Plain-text table. ``widths`` caps (and clips) selected columns."""
    cells = [[clip(c, (widths or {}).get(i, 10_000)) for i, c in enumerate(r)] for r in rows]
    size = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(size[i]) for i, h in enumerate(headers)).rstrip()
    out = [line, "  ".join("-" * size[i] for i in range(len(headers)))]
    out += ["  ".join(c.ljust(size[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    if not rows:
        out.append("(none)")
    return out


def _tz(app: App) -> str:
    return app.settings.schedule.timezone


# ----------------------------------------------------------------------- leads
def leads_view(app: App, statuses: list[LeadStatus] | None = None, limit: int = 200) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        query = select(Lead).order_by(Lead.score.desc().nulls_last(), Lead.username).limit(limit)
        if statuses:
            query = query.where(Lead.status.in_(statuses))
        rows = [
            [
                f"@{lead.username}",
                lead.score if lead.score is not None else "-",
                lead.status.value,
                ",".join(o["type"] for o in lead.opportunities or []) or "-",
                when(lead.created_at, tz),
                lead.status_reason or "",
            ]
            for lead in session.scalars(query)
        ]
    return table(["lead", "score", "status", "opportunities", "found", "why"], rows, {3: 40, 5: 70})


# --------------------------------------------------------------------- actions
def _action_row(a: Action, tz: str) -> list[Any]:
    return [
        when(a.created_at, tz),
        a.id,
        a.type.value,
        a.capability.value,
        a.status.value,
        f"@{a.target_username}" if a.target_username else "-",
        a.executed_channel.value if a.executed_channel else "-",
        a.approved_by or "-",
        a.status_reason or "",
    ]


def actions_view(
    app: App,
    types: list[ActionType] | None = None,
    statuses: list[ActionStatus] | None = None,
    target: str | None = None,
    limit: int = 100,
) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        query = select(Action).order_by(Action.created_at.desc(), Action.id.desc()).limit(limit)
        if types:
            query = query.where(Action.type.in_(types))
        if statuses:
            query = query.where(Action.status.in_(statuses))
        if target:
            query = query.where(Action.target_username == target.lstrip("@").lower())
        rows = [_action_row(a, tz) for a in reversed(session.scalars(query).all())]
    return table(
        ["created", "id", "type", "capability", "status", "target", "channel", "approved_by", "reason"],
        rows,
        {8: 60},
    )


def outbound_messages_view(app: App, types: list[ActionType], limit: int = 100) -> list[str]:
    """Generated messages with their fate (drafted / pending / sent / blocked ...)."""
    tz = _tz(app)
    out: list[str] = []
    with app.db.session() as session:
        actions = session.scalars(
            select(Action)
            .where(Action.type.in_(types))
            .order_by(Action.created_at.desc(), Action.id.desc())
            .limit(limit)
        ).all()
        for a in reversed(actions):
            label = a.type.value + (f" #{a.followup_number}" if a.followup_number else "")
            out.append(
                f"{when(a.created_at, tz)}  {a.id}  {label} -> @{a.target_username}  [{a.status.value}]"
                f"  via {a.capability.value}  composer={a.composer or '-'}  approved_by={a.approved_by or '-'}"
            )
            out.append(f"    {a.message_text or ''}")
            if a.status_reason:
                out.append(f"    status: {a.status_reason}")
    return out or ["(none)"]


def action_report(app: App, action_id: str) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        a = session.get(Action, action_id)
        if a is None:
            return [f"no action {action_id}"]
        attempts = session.scalars(
            select(ActionAttempt).where(ActionAttempt.action_id == a.id).order_by(ActionAttempt.attempt_no)
        ).all()
        out = [
            f"action {a.id}  ({a.idempotency_key})",
            f"  type {a.type.value} / {a.capability.value}   target @{a.target_username}   status {a.status.value}",
            f"  created {when(a.created_at, tz)} in mode {a.mode_at_creation.value}"
            f"   approved_by {a.approved_by or '-'} {when(a.approved_at, tz) if a.approved_at else ''}".rstrip(),
            f"  status reason: {a.status_reason or '-'}",
        ]
        if a.message_text:
            out += [
                f"  message ({a.message_kind}, composer {a.composer},"
                f" facts used: {', '.join(a.facts_used or []) or '-'}):",
                f"    {a.message_text}",
            ]
        params = {k: v for k, v in (a.params or {}).items() if k not in ("rejected_drafts",)}
        if params:
            out.append(f"  params: {params}")
        gate = a.gate or {}
        for phase in ("proposal", "execution"):
            if phase in gate:
                d = gate[phase]
                out.append(f"  gate at {phase}: {d['outcome']} - {'; '.join(d['reasons'])}")
        for h in gate.get("history", []):
            until = f" (until {when(h['not_before'], tz)})" if h.get("not_before") else ""
            out.append(f"    {when(h['at'], tz)}  {h['outcome']}: {'; '.join(h['reasons'])}{until}")
        for t in attempts:
            out.append(
                f"  attempt {t.attempt_no} {when(t.started_at, tz)} via {t.channel.value if t.channel else '-'}:"
                f" {t.status.value} {t.code or ''} {clip(t.detail, 120)}".rstrip()
            )
            for e in t.evidence or []:
                out.append(f"      evidence {e.get('kind')}: {e.get('path') or clip(e.get('content'), 100)}")
            if t.page_url:
                out.append(f"      page: {t.page_url}")
    return out


# -------------------------------------------------------------------- messages
def messages_view(app: App, handle: str | None = None, limit: int = 200, full: bool = False) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        query = (
            select(Message, Conversation.peer_username)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        if handle:
            query = query.where(Conversation.peer_username == handle.lstrip("@").lower())
        rows = [
            [
                when(m.sent_at or m.created_at, tz),
                f"@{peer}",
                "-> out" if m.direction.value == "OUTBOUND" else "<- in",
                m.sender_kind.value,
                m.delivery_state,
                m.intent.value if m.intent else "",
                m.text,
            ]
            for m, peer in reversed(session.execute(query).all())
        ]
    return table(["time", "peer", "dir", "sender", "state", "intent", "text"], rows, None if full else {6: 90})


# ---------------------------------------------------------- suppression / convs
def suppressions_view(app: App) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        rows = [
            [when(s.created_at, tz), s.kind.value, s.value, s.source, s.reason]
            for s in session.scalars(select(Suppression).order_by(Suppression.created_at, Suppression.id))
        ]
    return table(["since", "kind", "value", "source", "reason"], rows, {4: 70})


def conversations_view(app: App, paused_only: bool = False) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        query = select(Conversation).order_by(Conversation.id)
        if paused_only:
            query = query.where(Conversation.automation_paused.is_(True))
        rows = [
            [
                c.id,
                f"@{c.peer_username}" if c.peer_username else c.peer_igsid,
                c.owner.value,
                "PAUSED" if c.automation_paused else "active",
                when(c.paused_at, tz) if c.automation_paused else "-",
                c.paused_reason or "",
            ]
            for c in session.scalars(query)
        ]
    return table(["id", "peer", "owner", "automation", "paused_at", "reason"], rows, {5: 70})


def incidents_view(app: App, include_resolved: bool = False) -> list[str]:
    tz = _tz(app)
    out: list[str] = []
    with app.db.session() as session:
        query = select(Incident).order_by(Incident.created_at, Incident.id)
        if not include_resolved:
            query = query.where(Incident.status == IncidentStatus.OPEN)
        for i in session.scalars(query):
            out.append(f"#{i.id} {when(i.created_at, tz)} [{i.severity.value}] {i.status.value} {i.kind}: {i.title}")
            if i.detail:
                out.append(f"    detail: {clip(i.detail, 160)}")
            if i.page_url:
                out.append(f"    page: {i.page_url}")
            for e in i.evidence or []:
                out.append(f"    evidence {e.get('kind')}: {e.get('path') or clip(e.get('content'), 100)}")
            if i.resolved_at:
                out.append(f"    resolved {when(i.resolved_at, tz)} by {i.resolved_by}: {i.resolution_note}")
    return out or ["(no incidents)"]


# ----------------------------------------------------------------------- audit
def audit_view(app: App, kind_prefix: str | None = None, subject: str | None = None, limit: int = 200) -> list[str]:
    tz = _tz(app)
    with app.db.session() as session:
        query = select(AuditEvent).order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(limit)
        if kind_prefix:
            query = query.where(AuditEvent.kind.startswith(kind_prefix))
        if subject:
            query = query.where(AuditEvent.subject == subject)
        rows = [
            [when(e.at, tz), e.actor, e.kind, e.subject or "", e.summary]
            for e in reversed(session.scalars(query).all())
        ]
    return table(["time", "actor", "event", "subject", "summary"], rows, {1: 16, 4: 110})


# --------------------------------------------------------------------- explain
def explain_lead(app: App, handle: str) -> list[str]:
    """Everything the system knows and decided about one lead, in order."""
    tz = _tz(app)
    name = handle.lstrip("@").lower()
    out: list[str] = []
    with app.db.session() as session:
        lead = session.scalars(select(Lead).where(Lead.username == name)).first()
        if lead is None:
            return [f"no lead @{name}"]
        out.append(f"@{lead.username} - {lead.full_name or '?'}  ({lead.category or 'no category'})")
        out.append(
            f"status {lead.status.value}: {lead.status_reason or '-'}"
            f"   score {lead.score if lead.score is not None else '-'}"
            f"   campaign {lead.campaign_id or '-'}"
        )
        out.append("")
        out.append("HOW IT WAS FOUND")
        for src in session.scalars(
            select(LeadSource).where(LeadSource.lead_id == lead.id).order_by(LeadSource.discovered_at, LeadSource.id)
        ):
            extra = ""
            hints = src.hints or {}
            if hints.get("comment_text"):
                extra = f'  comment: "{clip(hints["comment_text"][0], 80)}"'
            if hints.get("note"):
                extra = f"  note: {hints['note'][0]}"
            out.append(
                f"  {when(src.discovered_at, tz)}  {src.strategy}  {src.query or ''} {src.seed or ''}".rstrip() + extra
            )
        out.append("")
        out.append("WHAT WAS OBSERVED AND WHY IT (DID NOT) QUALIFY")
        facts = (lead.signals or {}).get("facts", {})
        for key, value in facts.items():
            out.append(f"  fact {key}: {clip(value, 110)}")
        for opp in lead.opportunities or []:
            out.append(f"  opportunity {opp.get('type')} (weight {opp.get('weight')}): {opp.get('rationale')}")
        breakdown = {k: v for k, v in (lead.score_breakdown or {}).items() if isinstance(v, int | float)}
        if breakdown:
            parts = " + ".join(f"{k} {v}" for k, v in breakdown.items())
            out.append(f"  score = {parts} = {lead.score}")
        for reason in lead.disqualify_reasons or []:
            out.append(f"  disqualified: {reason}")
        if lead.duplicate_of_id:
            dup = session.get(Lead, lead.duplicate_of_id)
            out.append(f"  duplicate of @{dup.username if dup else lead.duplicate_of_id}")
        if lead.followups_blocked_reason:
            out.append(f"  follow-ups blocked: {lead.followups_blocked_reason}")
        out.append("")
        out.append("ACTIONS (and why each happened)")
        actions = session.scalars(
            select(Action).where(Action.lead_id == lead.id).order_by(Action.created_at, Action.id)
        ).all()
        for a in actions:
            label = a.type.value + (f" #{a.followup_number}" if a.followup_number else "")
            out.append(
                f"  {when(a.created_at, tz)}  {label} ({a.capability.value})  -> {a.status.value}"
                f"   approved_by {a.approved_by or '-'}   id {a.id}"
            )
            gate = a.gate or {}
            if "proposal" in gate:
                out.append(
                    f"      proposal gate: {gate['proposal']['outcome']} - {'; '.join(gate['proposal']['reasons'])}"
                )
            for h in gate.get("history", []):
                out.append(f"      {when(h['at'], tz)} execution gate: {h['outcome']} - {'; '.join(h['reasons'])}")
            for t in session.scalars(
                select(ActionAttempt).where(ActionAttempt.action_id == a.id).order_by(ActionAttempt.attempt_no)
            ):
                out.append(
                    f"      {when(t.started_at, tz)} attempt {t.attempt_no}"
                    f" via {t.channel.value if t.channel else '-'}: {t.status.value} {t.code or ''}".rstrip()
                )
            if a.message_text:
                out.append(f'      text: "{a.message_text}"')
            if a.status_reason:
                out.append(f"      result: {a.status_reason}")
        out.append("")
        out.append("MESSAGES")
        conv = session.scalars(select(Conversation).where(Conversation.lead_id == lead.id)).first()
        if conv is None:
            conv = session.scalars(select(Conversation).where(Conversation.peer_username == lead.username)).first()
        if conv is not None:
            for m in session.scalars(
                select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id)
            ):
                arrow = "->" if m.direction.value == "OUTBOUND" else "<-"
                out.append(
                    f"  {when(m.sent_at or m.created_at, tz)} {arrow} {m.sender_kind.value} [{m.delivery_state}]"
                    f"{' ' + m.intent.value if m.intent else ''}: {m.text}"
                )
            out.append(
                f"  conversation owner {conv.owner.value}"
                + (f", automation PAUSED: {conv.paused_reason}" if conv.automation_paused else ", automation active")
            )
        else:
            out.append("  (no conversation)")
        out.append("")
        out.append("AUDIT TRAIL")
        for e in session.scalars(
            select(AuditEvent).where(AuditEvent.subject == f"@{lead.username}").order_by(AuditEvent.at, AuditEvent.id)
        ):
            out.append(f"  {when(e.at, tz)}  {e.kind}  ({e.actor})  {e.summary}")
    return out


# ---------------------------------------------------------------------- safety
def safety_view(app: App) -> list[str]:
    s = app.settings
    lim = app.runtime.limits()
    overrides = app.runtime.limit_overrides()
    tz = s.schedule.timezone
    own = s.ownership

    def mark(key: str) -> str:
        return "  (runtime override)" if key in overrides else ""

    with app.db.session() as session:
        suppressed = session.scalars(select(Suppression)).all()
    by_kind: dict[str, int] = {}
    for row in suppressed:
        by_kind[row.kind.value] = by_kind.get(row.kind.value, 0) + 1
    sandbox = ", ".join("@" + t.lstrip("@") for t in s.rollout.allowed_targets) or "none (any qualified lead)"
    lines = [
        f"environment {s.environment.value}   mode {app.runtime.mode().value}   "
        f"global pause {'ON' if app.runtime.paused() else 'off'}   account @{s.account.username}",
        "",
        "SEND CAPS",
        f"  new conversations: {lim.outreach_per_day}/day{mark('outreach_per_day')}, "
        f"{lim.outreach_per_hour}/hour{mark('outreach_per_hour')}",
        f"  follow-ups: {lim.followups_per_day}/day{mark('followups_per_day')}",
        f"  replies: {lim.replies_per_hour}/hour{mark('replies_per_hour')}",
        f"  profile inspections: {lim.profile_inspections_per_day}/day"
        f"   discovery runs: {lim.discovery_runs_per_day}/day",
        f"  browser page views: {lim.browser_units_per_hour}/hour, at most {lim.max_units_per_operation} per operation,"
        f" {lim.max_concurrent_browser_sessions} browser session(s) at a time",
        "",
        "DELAYS",
        f"  between any two sends: >= {lim.min_seconds_between_sends}s + random 0-{lim.send_jitter_seconds}s"
        f"{mark('min_seconds_between_sends')}",
        f"  between browser page views: >= {lim.min_seconds_between_browser_units}s + random"
        f" 0-{lim.browser_unit_jitter_seconds}s",
        f"  retries: at most {lim.retry_max_attempts} attempts, backoff {lim.retry_base_seconds}s x 2^n",
        "",
        f"OPERATING HOURS ({tz})",
        f"  sends: {s.schedule.send_hours[0]}-{s.schedule.send_hours[1]}"
        f"   browser activity: {s.schedule.browser_active_hours[0]}-{s.schedule.browser_active_hours[1]}",
        "",
        "FOLLOW-UPS",
        f"  at most {lim.max_followups_per_lead} per lead, after {', '.join(str(d) for d in lim.followup_after_days)}"
        " day(s) since the previous message",
        "  never after they replied, never while Rohit owns the conversation, never after a private reply,",
        "  never while Instagram shows a pending message request",
        "",
        "DUPLICATE PREVENTION",
        "  one idempotency key per planned action (re-planning never duplicates work)",
        "  repeated contact denied if ANY earlier outbound message to the lead exists",
        "  first message refused (ALREADY_CONTACTED) if the thread already has history",
        "  identical text already in the thread -> reported as sent, never re-sent",
        "  one business = one lead: same IG user id, website domain, phone or email -> DUPLICATE",
        "",
        "SUPPRESSION",
        "  opt-out / 'not interested' replies suppress username, IGSID, website domain, phone and email",
        f"  currently suppressed: {sum(by_kind.values())} ("
        + (", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())) or "none")
        + ")",
        f"  sandbox allowlist (rollout.allowed_targets): {sandbox}",
        "",
        "HUMAN TAKEOVER LOCK",
        "  one owner per conversation (NONE / HUMAN / API_AGENT / BROWSER_AGENT), shared by API and browser",
        f"  automation lease ttl {own.lock_ttl_seconds}s, re-validated immediately before pressing send",
        "  any outbound message automation did not send (webhook echo or thread read) -> owner HUMAN,"
        " automation paused, open actions cancelled",
        f"  paused until: {'released by Rohit' if own.human_hold_hours is None else f'{own.human_hold_hours}h'}"
        f"   echo attribution window {own.echo_match_window_seconds}s",
        "",
        "STOP BEHAVIOUR (never bypassed)",
    ]
    for status, message in _HALT_MESSAGES.items():
        scope = "ALL lanes" if status is ExecutionStatus.ACCOUNT_RESTRICTED else "that lane"
        lines.append(f"  {status.value}: HALT {scope} + critical incident ({message}); actions parked")
    lines += [
        "  checkpoint detection: /challenge/, /auth_platform/, two-factor URLs, 'confirm it's you' texts,"
        " captcha iframes (recaptcha, hcaptcha, arkose)",
        f"  RATE_LIMITED: cooldown {lim.rate_limit_cooldown_minutes} min; {lim.rate_limit_events_before_halt}"
        " within 24h -> HALT",
        f"  UI_CHANGED: HALT after {lim.ui_changed_events_before_halt} consecutive",
        "  resume only by a human: `insta-outreach lane resume <api|browser>`",
        "",
        "LIVE AUTONOMOUS PREREQUISITES (see `insta-outreach preflight`)",
        "  control token, an outbound lane, browser session verified within"
        f" {s.rollout.browser_session_max_age_days} days, no halted lane, no open incident,",
        f"  >= {s.rollout.min_approved_sends_for_autonomous} human-approved live sends succeeded,"
        " explicit confirmation",
        f"  approval queue items expire after {lim.approval_ttl_hours}h",
    ]
    return lines


def preflight_view(checks: list[Check]) -> list[str]:
    rows = [[c.mark, c.name, "yes" if c.required else "no", c.detail] for c in checks]
    return table(["result", "check", "required", "detail"], rows, {3: 110})


SUPPRESSION_KINDS = [k.value for k in SuppressionKind]
