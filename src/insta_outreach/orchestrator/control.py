"""Human control surface shared by the CLI and the HTTP control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Channel,
    Environment,
    IncidentStatus,
    LeadStatus,
    OperatingMode,
    SuppressionKind,
)
from insta_outreach.domain.models import text_sha256
from insta_outreach.orchestrator.pipeline import Services
from insta_outreach.personalization.validator import MessageValidator
from insta_outreach.policy.lanes import LaneService
from insta_outreach.policy.suppression import add_suppression, remove_suppression
from insta_outreach.storage.models import (
    Action,
    ActionAttempt,
    Conversation,
    Incident,
    Lead,
    LeadSource,
    Message,
    Suppression,
)
from insta_outreach.util.clock import local_day_start


class ControlError(ValueError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def action_dict(a: Action) -> dict[str, Any]:
    return {
        "id": a.id,
        "type": a.type.value,
        "capability": a.capability.value,
        "status": a.status.value,
        "status_reason": a.status_reason,
        "target_username": a.target_username,
        "lead_id": a.lead_id,
        "conversation_id": a.conversation_id,
        "message": a.message_text,
        "message_kind": a.message_kind,
        "followup_number": a.followup_number,
        "composer": a.composer,
        "facts_used": a.facts_used,
        "approved_by": a.approved_by,
        "mode_at_creation": a.mode_at_creation.value,
        "attempts": a.attempts,
        "not_before": _iso(a.not_before),
        "executed_channel": a.executed_channel.value if a.executed_channel else None,
        "last_result": a.last_result_status.value if a.last_result_status else None,
        "last_result_code": a.last_result_code,
        "gate": a.gate,
        "created_at": _iso(a.created_at),
        "completed_at": _iso(a.completed_at),
        "params": {k: v for k, v in (a.params or {}).items() if k != "rejected_drafts"},
    }


def lead_dict(lead: Lead, detail: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": lead.id,
        "username": lead.username,
        "full_name": lead.full_name,
        "category": lead.category,
        "status": lead.status.value,
        "status_reason": lead.status_reason,
        "score": lead.score,
        "niche": lead.niche,
        "location": lead.location_match,
        "followers": lead.followers,
        "website": lead.website,
        "opportunities": lead.opportunities,
        "campaign_id": lead.campaign_id,
        "contacted_at": _iso(lead.contacted_at),
        "replied_at": _iso(lead.replied_at),
        "followups_sent": lead.followups_sent,
        "followups_blocked_reason": lead.followups_blocked_reason,
    }
    if detail:
        data |= {
            "biography": lead.biography,
            "signals": lead.signals,
            "score_breakdown": lead.score_breakdown,
            "disqualify_reasons": lead.disqualify_reasons,
            "hooks": lead.hooks,
            "website_check": lead.website_check,
            "contact": lead.contact,
            "themes": lead.themes,
            "recent_posts": lead.recent_posts[:6],
            "duplicate_of_id": lead.duplicate_of_id,
        }
    return data


def incident_dict(i: Incident) -> dict[str, Any]:
    return {
        "id": i.id,
        "account_id": i.account_id,
        "channel": i.channel.value if i.channel else None,
        "severity": i.severity.value,
        "status": i.status.value,
        "kind": i.kind,
        "title": i.title,
        "detail": i.detail,
        "action_id": i.action_id,
        "page_url": i.page_url,
        "evidence": i.evidence,
        "created_at": _iso(i.created_at),
        "resolved_at": _iso(i.resolved_at),
        "resolved_by": i.resolved_by,
        "resolution_note": i.resolution_note,
    }


def conversation_dict(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "peer_username": c.peer_username,
        "peer_igsid": c.peer_igsid,
        "lead_id": c.lead_id,
        "owner": c.owner.value,
        "automation_paused": c.automation_paused,
        "paused_reason": c.paused_reason,
        "pause_until": _iso(c.pause_until),
        "last_inbound_at": _iso(c.last_inbound_at),
        "last_outbound_at": _iso(c.last_outbound_at),
        "last_human_activity_at": _iso(c.last_human_activity_at),
    }


class ControlService:
    def __init__(self, services: Services, lanes: LaneService, validator: MessageValidator) -> None:
        self.s = services
        self._lanes = lanes
        self._validator = validator
        self._account = services.settings.account.id

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = self.s.clock.now()
        day_start = local_day_start(now, self.s.settings.schedule.timezone)
        with self.s.db.session() as session:
            lanes = [
                {
                    "channel": lane.channel.value,
                    "state": lane.state.value,
                    "until": _iso(lane.until),
                    "reason": lane.reason,
                }
                for lane in self._lanes.all_snapshots(session, self._account)
            ]
            lead_counts = dict(session.execute(select(Lead.status, func.count()).group_by(Lead.status)).all())
            action_counts = dict(session.execute(select(Action.status, func.count()).group_by(Action.status)).all())
            sent_today = session.scalar(
                select(func.count())
                .select_from(Action)
                .where(
                    Action.type.in_([ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY]),
                    Action.status == ActionStatus.SUCCEEDED,
                    Action.completed_at >= day_start,
                )
            )
            open_incidents = session.scalar(
                select(func.count()).select_from(Incident).where(Incident.status == IncidentStatus.OPEN)
            )
            paused_conversations = session.scalar(
                select(func.count()).select_from(Conversation).where(Conversation.automation_paused.is_(True))
            )
        adapters = {c.value: {"simulated": a.simulated} for c, a in self.s.executor.adapters.items()}
        return {
            "environment": self.s.settings.environment.value,
            "mode": self.s.runtime.mode().value,
            "global_pause": self.s.runtime.paused(),
            "account": self.s.settings.account.username,
            "adapters": adapters,
            "lanes": lanes,
            "limits": self.s.runtime.limits().model_dump(),
            "leads": {k.value: v for k, v in lead_counts.items()},
            "actions": {k.value: v for k, v in action_counts.items()},
            "sent_today": sent_today or 0,
            "open_incidents": open_incidents or 0,
            "paused_conversations": paused_conversations or 0,
            "now": now.isoformat(),
        }

    # -- mode / pause ------------------------------------------------------------
    def set_mode(self, mode: OperatingMode, by: str, confirm: bool = False) -> dict[str, str]:
        if mode is OperatingMode.AUTONOMOUS and self.s.settings.environment is Environment.LIVE and not confirm:
            raise ControlError("switching the LIVE account to AUTONOMOUS requires explicit confirmation")
        previous = self.s.runtime.set_mode(mode, by)
        return {"previous": previous.value, "mode": mode.value}

    def set_paused(self, paused: bool, by: str) -> None:
        self.s.runtime.set_paused(paused, by)

    # -- approvals ------------------------------------------------------------------
    def list_actions(self, statuses: list[ActionStatus] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            query = select(Action).order_by(Action.created_at.desc()).limit(limit)
            if statuses:
                query = query.where(Action.status.in_(statuses))
            return [action_dict(a) for a in session.scalars(query)]

    def action_detail(self, action_id: str) -> dict[str, Any]:
        with self.s.db.session() as session:
            action = self._get_action(session, action_id)
            attempts = session.scalars(
                select(ActionAttempt).where(ActionAttempt.action_id == action_id).order_by(ActionAttempt.attempt_no)
            ).all()
            return action_dict(action) | {
                "attempt_log": [
                    {
                        "attempt": t.attempt_no,
                        "channel": t.channel.value if t.channel else None,
                        "status": t.status.value,
                        "code": t.code,
                        "detail": t.detail,
                        "confirmed": t.confirmed,
                        "simulated": t.simulated,
                        "evidence": t.evidence,
                        "page_url": t.page_url,
                        "started_at": _iso(t.started_at),
                        "finished_at": _iso(t.finished_at),
                    }
                    for t in attempts
                ]
            }

    def _get_action(self, session: Session, action_id: str) -> Action:
        action = session.get(Action, action_id)
        if action is None:
            raise ControlError(f"no action {action_id}")
        return action

    def approve(self, action_id: str, by: str, edited_text: str | None = None) -> dict[str, Any]:
        with self.s.db.session() as session:
            action = self._get_action(session, action_id)
            if not action.type.is_outbound:
                raise ControlError("only outbound actions need approval")
            if action.status not in (ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED):
                raise ControlError(f"action is {action.status.value}, not awaiting approval")
            if edited_text is not None and edited_text.strip() != (action.message_text or "").strip():
                lead = session.get(Lead, action.lead_id) if action.lead_id else None
                facts = dict((lead.signals or {}).get("facts", {})) if lead else {}
                problems = self._validator.validate(
                    edited_text.strip(), kind=action.message_kind or "initial", facts=facts
                )
                if problems:
                    raise ControlError("edited message rejected: " + "; ".join(problems))
                action.message_text = edited_text.strip()
                action.message_sha256 = text_sha256(action.message_text)
                action.composer = "human"
            action.status = ActionStatus.APPROVED
            action.approved_by, action.approved_at = f"human:{by}", self.s.clock.now()
            action.not_before = None
            action.status_reason = f"approved by {by}"
            return action_dict(action)

    def reject(self, action_id: str, by: str, reason: str = "", redraft: bool = False) -> dict[str, Any]:
        with self.s.db.session() as session:
            action = self._get_action(session, action_id)
            if action.status not in (ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED, ActionStatus.APPROVED):
                raise ControlError(f"action is {action.status.value}; cannot reject")
            action.status = ActionStatus.REJECTED
            action.status_reason = f"rejected by {by}: {reason}".strip()[:1000]
            lead = session.get(Lead, action.lead_id) if action.lead_id else None
            if lead is not None and action.type is ActionType.SEND_OUTREACH:
                if redraft:
                    lead.status, lead.status_reason = LeadStatus.QUALIFIED, f"redraft requested by {by}"
                else:
                    lead.status, lead.status_reason = LeadStatus.DISQUALIFIED, f"rejected by {by}: {reason}"[:512]
            return action_dict(action)

    # -- lanes & incidents ------------------------------------------------------------
    def incidents(self, open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            query = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
            if open_only:
                query = query.where(Incident.status == IncidentStatus.OPEN)
            return [incident_dict(i) for i in session.scalars(query)]

    def resume_lane(self, channel: Channel, by: str, note: str = "") -> int:
        with self.s.db.session() as session:
            return self._lanes.resume(session, self._account, channel, by, note)

    def halt_lane(self, channel: Channel, by: str, reason: str) -> None:
        with self.s.db.session() as session:
            self._lanes.halt_manually(session, self._account, channel, f"{by}: {reason}")

    # -- conversations -------------------------------------------------------------------
    def conversations(self, paused_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            query = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
            if paused_only:
                query = query.where(Conversation.automation_paused.is_(True))
            return [conversation_dict(c) for c in session.scalars(query)]

    def _conversation(self, session: Session, ref: str | int) -> Conversation:
        conv = (
            session.get(Conversation, int(ref))
            if str(ref).isdigit()
            else self.s.ownership.find(session, self._account, peer_username=str(ref).lstrip("@").lower())
        )
        if conv is None:
            raise ControlError(f"no conversation {ref}")
        return conv

    def claim_conversation(self, ref: str | int, by: str) -> dict[str, Any]:
        with self.s.db.session() as session:
            conv = self._conversation(session, ref)
            cancelled = self.s.ownership.take_over_by_human(session, conv, f"claimed by {by}")
            return conversation_dict(conv) | {"cancelled_actions": cancelled}

    def release_conversation(self, ref: str | int, by: str) -> dict[str, Any]:
        with self.s.db.session() as session:
            conv = self._conversation(session, ref)
            self.s.ownership.release_to_automation(session, conv, by)
            return conversation_dict(conv)

    def conversation_messages(self, ref: str | int) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            conv = self._conversation(session, ref)
            return [
                {
                    "direction": m.direction.value,
                    "sender": m.sender_kind.value,
                    "text": m.text,
                    "state": m.delivery_state,
                    "sent_at": _iso(m.sent_at),
                    "via": m.observed_via,
                    "intent": m.intent.value if m.intent else None,
                }
                for m in session.scalars(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
                )
            ]

    # -- leads & suppression ----------------------------------------------------------------
    def leads(self, statuses: list[LeadStatus] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            query = select(Lead).order_by(Lead.score.desc().nulls_last(), Lead.id).limit(limit)
            if statuses:
                query = query.where(Lead.status.in_(statuses))
            return [lead_dict(lead) for lead in session.scalars(query)]

    def lead_detail(self, ref: str | int) -> dict[str, Any]:
        with self.s.db.session() as session:
            lead = (
                session.get(Lead, int(ref))
                if str(ref).isdigit()
                else session.scalars(select(Lead).where(Lead.username == str(ref).lstrip("@").lower())).first()
            )
            if lead is None:
                raise ControlError(f"no lead {ref}")
            sources = session.scalars(select(LeadSource).where(LeadSource.lead_id == lead.id)).all()
            return lead_dict(lead, detail=True) | {
                "sources": [
                    {
                        "strategy": s.strategy,
                        "query": s.query,
                        "seed": s.seed,
                        "post_url": s.post_url,
                        "discovered_at": _iso(s.discovered_at),
                    }
                    for s in sources
                ]
            }

    def suppress(self, kind: SuppressionKind, value: str, reason: str, by: str) -> bool:
        with self.s.db.session() as session:
            created = add_suppression(session, kind, value, f"{reason} (by {by})", "MANUAL")
            if kind is SuppressionKind.USERNAME:
                lead = session.scalars(select(Lead).where(Lead.username == value.lstrip("@").lower())).first()
                if lead is not None:
                    lead.status, lead.status_reason = LeadStatus.CLOSED, f"suppressed by {by}: {reason}"[:512]
                    conv = self.s.ownership.find(session, self._account, peer_username=lead.username)
                    if conv is not None:
                        self.s.ownership.cancel_open_outbound(session, conv, "prospect suppressed")
            return created

    def unsuppress(self, kind: SuppressionKind, value: str) -> bool:
        with self.s.db.session() as session:
            return remove_suppression(session, kind, value)

    def suppressions(self) -> list[dict[str, Any]]:
        with self.s.db.session() as session:
            return [
                {
                    "kind": s.kind.value,
                    "value": s.value,
                    "reason": s.reason,
                    "source": s.source,
                    "created_at": _iso(s.created_at),
                }
                for s in session.scalars(select(Suppression).order_by(Suppression.created_at.desc()))
            ]
