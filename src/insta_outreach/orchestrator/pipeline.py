"""Pipeline stages: DISCOVER -> ANALYZE -> DEDUPE -> SCORE -> PERSONALIZE ->
ELIGIBILITY GATE -> OUTREACH, plus follow-ups and inbound handling.

This module is where business decisions live. Executors never appear here
except through queued actions; the worker executes them and calls back into
the ``handle_*`` methods with structured results.
"""

from __future__ import annotations

import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from insta_outreach.config import Settings, StrategySpec
from insta_outreach.conversations.ownership import OwnershipService
from insta_outreach.discovery.strategies import (
    REGISTRY,
    DiscoveryStrategy,
    DiscoveryTask,
    PlanningContext,
    build_strategies,
)
from insta_outreach.domain.enums import (
    OPEN_ACTION_STATUSES,
    ActionStatus,
    ActionType,
    Capability,
    Environment,
    ExecutionStatus,
    GateOutcome,
    IncidentSeverity,
    LeadStatus,
    MessageDirection,
    OperatingMode,
    OpportunityType,
    ReplyIntent,
    SenderKind,
    SuppressionKind,
)
from insta_outreach.domain.models import (
    CandidateRef,
    ExecutionResult,
    InboundEvent,
    ProfileObservation,
    ThreadSnapshot,
)
from insta_outreach.execution.executor import InstagramExecutor
from insta_outreach.intelligence.analyzer import LeadAnalyzer, SourceHints
from insta_outreach.intelligence.dedupe import register_and_find_duplicate
from insta_outreach.intelligence.website import WebsiteCheck, WebsiteChecker
from insta_outreach.orchestrator.actions import ActionService
from insta_outreach.personalization.composer import CompositionFailed, MessageComposer, keyword_intent
from insta_outreach.policy.audit import audit
from insta_outreach.policy.gate import AUTO_APPROVER, EligibilityGate, sandbox_targets
from insta_outreach.policy.incidents import IncidentService
from insta_outreach.policy.suppression import add_suppression
from insta_outreach.runtime import RuntimeControl
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import (
    Action,
    Conversation,
    DiscoveryRun,
    Lead,
    LeadSource,
    Message,
    ProfileSnapshot,
)
from insta_outreach.util.clock import Clock, local_day_start, utc
from insta_outreach.util.text import collapse_whitespace, registrable_domain, truncate, try_canonical_username

log = logging.getLogger(__name__)

IdentityResolver = Callable[[str], Awaitable[str | None]]

OWN_POST_COMMENT = "own_post_comment"
# Instagram allows one private reply to a comment, within 7 days of it.
PRIVATE_REPLY_WINDOW = timedelta(days=6, hours=12)


@dataclass
class Services:
    settings: Settings
    db: Database
    clock: Clock
    runtime: RuntimeControl
    actions: ActionService
    analyzer: LeadAnalyzer
    website_checker: WebsiteChecker
    composer: MessageComposer
    gate: EligibilityGate
    executor: InstagramExecutor
    ownership: OwnershipService
    incidents: IncidentService
    rng: random.Random
    identity_resolver: IdentityResolver | None = None


def status_for_new_outbound(
    mode: OperatingMode, action_type: ActionType, autonomous_replies: bool
) -> tuple[ActionStatus, str | None]:
    """How a freshly prepared outbound action enters the queue in each mode."""
    if mode is OperatingMode.DRAFT:
        return ActionStatus.DRAFTED, None
    if mode is OperatingMode.APPROVAL:
        return ActionStatus.PENDING_APPROVAL, None
    if mode is OperatingMode.AUTONOMOUS:
        if action_type is ActionType.SEND_REPLY and not autonomous_replies:
            return ActionStatus.PENDING_APPROVAL, None
        return ActionStatus.APPROVED, AUTO_APPROVER
    raise ValueError("OBSERVE mode never prepares outbound actions")


_QUEUE_VERB = {
    ActionStatus.DRAFTED: "drafted (DRAFT mode: never sent)",
    ActionStatus.PENDING_APPROVAL: "queued for human approval",
    ActionStatus.APPROVED: "auto-approved; sends when every gate check passes",
}


def _outbound_label(action_type: ActionType, capability: Capability, followup_number: int) -> str:
    if action_type is ActionType.SEND_FOLLOW_UP:
        return f"follow-up #{followup_number}"
    if action_type is ActionType.SEND_REPLY:
        return "reply"
    if capability is Capability.PRIVATE_REPLY:
        return "first message (private reply to their comment)"
    return "first message"


class Pipeline:
    def __init__(self, services: Services) -> None:
        self.s = services
        self._account = services.settings.account.id
        self._last_discovery_plan: datetime | None = None
        self._last_inbox_plan: datetime | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> Settings:
        return self.s.settings

    def _now(self) -> datetime:
        return self.s.clock.now()

    def _allowed_username(self, username: str) -> bool:
        if username == self.settings.account.username.lower():
            return False
        is_sim = username.startswith("sim.")
        # Simulated leads never reach live execution; real handles never enter the simulator.
        return is_sim if self.settings.environment is Environment.LOCAL else not is_sim

    def _recent_automation_texts(self, session: Session, limit: int = 40) -> list[str]:
        return list(
            session.scalars(
                select(Message.text)
                .where(
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.sender_kind.in_([SenderKind.API_AGENT, SenderKind.BROWSER_AGENT]),
                )
                .order_by(Message.created_at.desc())
                .limit(limit)
            )
        )

    def _recent_drafts(self, session: Session, limit: int = 40) -> list[str]:
        rows = session.scalars(
            select(Action.message_text)
            .where(Action.message_text.is_not(None), Action.type == ActionType.SEND_OUTREACH)
            .order_by(Action.created_at.desc())
            .limit(limit)
        )
        return [r for r in rows if r]

    # ---------------------------------------------------------------- DISCOVER
    def plan_discovery(self, force: bool = False) -> int:
        now = self._now()
        interval = timedelta(minutes=self.settings.schedule.discovery_interval_minutes)
        if not force and self._last_discovery_plan and now - self._last_discovery_plan < interval:
            return 0
        self._last_discovery_plan = now
        created = 0
        day_start = local_day_start(now, self.settings.schedule.timezone)
        day = day_start.date().isoformat()
        with self.s.db.session() as session:
            open_actions = session.scalars(
                select(Action).where(Action.type == ActionType.DISCOVER, Action.status.in_(OPEN_ACTION_STATUSES))
            ).all()
            open_keys = {(a.params or {}).get("query_key") for a in open_actions}
            ran_today = (
                session.scalar(
                    select(func.count())
                    .select_from(DiscoveryRun)
                    .where(DiscoveryRun.started_at >= day_start, DiscoveryRun.status == "SUCCEEDED")
                )
                or 0
            )
            # Never queue more runs than today's cap leaves room for.
            budget = self.s.runtime.limits().discovery_runs_per_day - ran_today - len(open_actions)
            if budget <= 0:
                return 0
            seeds = list(
                session.scalars(
                    select(Lead.username)
                    .where(
                        Lead.status.in_(
                            [
                                LeadStatus.QUALIFIED,
                                LeadStatus.OUTREACH_PENDING,
                                LeadStatus.CONTACTED,
                                LeadStatus.REPLIED,
                            ]
                        )
                    )
                    .order_by(Lead.score.desc())
                    .limit(20)
                )
            )
            for campaign in self.settings.campaigns:
                if not campaign.enabled:
                    continue
                for strategy in build_strategies(campaign):
                    cutoff = now - timedelta(days=strategy.cooldown_days())
                    recent = set(
                        session.scalars(
                            select(DiscoveryRun.query_key).where(
                                DiscoveryRun.strategy == strategy.name,
                                DiscoveryRun.started_at >= cutoff,
                                DiscoveryRun.status.in_(["PLANNED", "SUCCEEDED"]),
                            )
                        )
                    )
                    ctx = PlanningContext(
                        now=now,
                        recently_run=recent | {k for k in open_keys if k},
                        qualified_seeds=[s for s in seeds if self._allowed_username(s)],
                    )
                    for task in strategy.plan(campaign, ctx):
                        if created >= budget:
                            return created
                        created += self._queue_discovery(session, task, day)
        return created

    def _queue_discovery(self, session: Session, task: DiscoveryTask, day: str) -> int:
        params = dict(task.params) | {
            "strategy": task.strategy,
            "query_key": task.query_key,
            "hints": task.hints,
            "campaign_id": task.campaign_id,
        }
        action, created = self.s.actions.create(
            session,
            key=f"discover:{self._account}:{task.query_key}:{day}",
            account_id=self._account,
            type=ActionType.DISCOVER,
            capability=task.capability,
            status=ActionStatus.APPROVED,
            mode=self.s.runtime.mode(),
            target_username=task.target_username,
            campaign_id=task.campaign_id,
            params=params,
            approved_by=AUTO_APPROVER,
            max_attempts=2,
        )
        if created:
            session.add(
                DiscoveryRun(
                    campaign_id=task.campaign_id,
                    strategy=task.strategy,
                    query_key=task.query_key,
                    action_id=action.id,
                    status="PLANNED",
                    started_at=self._now(),
                )
            )
        return int(created)

    def handle_discovery_result(self, session: Session, action: Action, result: ExecutionResult, final: bool) -> None:
        run = session.scalars(select(DiscoveryRun).where(DiscoveryRun.action_id == action.id)).first()
        params = action.params or {}
        if not result.ok:
            if run is not None and final:
                run.status, run.detail, run.finished_at = (
                    "FAILED",
                    f"{result.status}: {result.detail}"[:1000],
                    self._now(),
                )
            return
        strategy_cls = REGISTRY.get(str(params.get("strategy")))
        strategy: DiscoveryStrategy | None = (
            strategy_cls(StrategySpec(name=strategy_cls.name)) if strategy_cls else None
        )
        task = DiscoveryTask(
            campaign_id=str(params.get("campaign_id") or ""),
            strategy=str(params.get("strategy")),
            capability=action.capability,
            query_key=str(params.get("query_key")),
            target_username=action.target_username,
            hints=params.get("hints") or {},
        )
        candidates = strategy.interpret(task, result) if strategy else []
        found, new = self.upsert_candidates(session, task, candidates, action.id)
        if run is not None:
            run.status, run.found, run.new_leads, run.finished_at = "SUCCEEDED", found, new, self._now()

    def upsert_candidates(
        self, session: Session, task: DiscoveryTask, candidates: list[Any], action_id: str | None
    ) -> tuple[int, int]:
        found = new = 0
        for ref in candidates:
            username = try_canonical_username(ref.username)
            if not username or not self._allowed_username(username):
                continue
            found += 1
            lead = session.scalars(select(Lead).where(Lead.username == username)).first()
            if lead is None:
                lead = Lead(
                    username=username,
                    full_name=ref.full_name,
                    status=LeadStatus.DISCOVERED,
                    campaign_id=task.campaign_id or None,
                    created_at=self._now(),
                    updated_at=self._now(),
                )
                session.add(lead)
                session.flush()
                new += 1
            seed = task.seed or task.target_username or ""
            exists = session.scalars(
                select(LeadSource.id).where(
                    LeadSource.lead_id == lead.id,
                    LeadSource.strategy == task.strategy,
                    LeadSource.query == task.query_key,
                    LeadSource.seed == seed,
                )
            ).first()
            if exists is None:
                session.add(
                    LeadSource(
                        lead_id=lead.id,
                        campaign_id=task.campaign_id or None,
                        strategy=task.strategy,
                        query=task.query_key,
                        seed=seed,
                        post_url=ref.source_post_url,
                        action_id=action_id,
                        hints=task.hints or {},
                        discovered_at=self._now(),
                    )
                )
        return found, new

    # ----------------------------------------------------------------- ANALYZE
    def plan_inspections(self, limit: int = 10) -> int:
        created = 0
        with self.s.db.session() as session:
            leads = session.scalars(
                select(Lead).where(Lead.status == LeadStatus.DISCOVERED).order_by(Lead.created_at).limit(limit)
            ).all()
            for lead in leads:
                if not self._allowed_username(lead.username):
                    continue
                _, was_created = self.s.actions.create(
                    session,
                    key=f"inspect:{self._account}:{lead.id}",
                    account_id=self._account,
                    type=ActionType.INSPECT_PROFILE,
                    capability=Capability.INSPECT_PROFILE,
                    status=ActionStatus.APPROVED,
                    mode=self.s.runtime.mode(),
                    lead_id=lead.id,
                    target_username=lead.username,
                    campaign_id=lead.campaign_id,
                    params={"limit": 12},
                    approved_by=AUTO_APPROVER,
                )
                created += int(was_created)
        return created

    def handle_inspection_result(
        self, session: Session, action: Action, result: ExecutionResult, final: bool
    ) -> int | None:
        """Persist the observation. Returns the lead id to analyze next."""
        lead = session.get(Lead, action.lead_id) if action.lead_id else None
        if lead is None:
            return None
        if result.status is ExecutionStatus.TARGET_NOT_FOUND:
            lead.status, lead.status_reason = LeadStatus.UNREACHABLE, f"profile not found ({result.code})"
            return None
        if not result.ok:
            if final:
                lead.status_reason = f"inspection failed: {result.status.value} {result.code or ''}".strip()
                lead.status = LeadStatus.UNREACHABLE
            return None
        obs = ProfileObservation.model_validate(result.data["profile"])
        self.apply_observation(lead, obs)
        session.add(
            ProfileSnapshot(
                lead_id=lead.id, channel=obs.observed_via, observed_at=obs.observed_at, data=obs.model_dump(mode="json")
            )
        )
        return lead.id

    @staticmethod
    def apply_observation(lead: Lead, obs: ProfileObservation) -> None:
        """Merge: a later observation never erases a value an earlier one had."""
        for field_name, value in (
            ("ig_user_id", obs.ig_user_id),
            ("full_name", obs.full_name),
            ("biography", obs.biography),
            ("category", obs.category),
            ("is_business", obs.is_business),
            ("is_private", obs.is_private),
            ("is_verified", obs.is_verified),
            ("website", obs.website),
            ("followers", obs.followers),
            ("following", obs.following),
            ("posts_count", obs.posts_count),
        ):
            if value is not None:
                setattr(lead, field_name, value)
        if obs.bio_links:
            lead.bio_links = obs.bio_links
        if obs.recent_posts:
            lead.recent_posts = [p.model_dump(mode="json") for p in obs.recent_posts]
        contact = obs.contact.model_dump(mode="json")
        if any(contact.values()):
            lead.contact = contact
        lead.observed_at = obs.observed_at

    def _merged_observation(self, session: Session, lead: Lead) -> ProfileObservation | None:
        snapshots = session.scalars(
            select(ProfileSnapshot).where(ProfileSnapshot.lead_id == lead.id).order_by(ProfileSnapshot.observed_at)
        ).all()
        if not snapshots:
            return None
        merged: dict[str, Any] = {}
        for snap in snapshots:
            for key, value in snap.data.items():
                if value not in (None, [], {}) or key not in merged:
                    merged[key] = value
        return ProfileObservation.model_validate(merged)

    async def analyze_lead(self, lead_id: int) -> LeadStatus | None:
        now = self._now()
        cfg = self.settings.scoring
        with self.s.db.session() as session:
            lead = session.get(Lead, lead_id)
            if lead is None:
                return None
            obs = self._merged_observation(session, lead)
            if obs is None:
                return None
            sources = session.scalars(select(LeadSource).where(LeadSource.lead_id == lead.id)).all()
            hints = SourceHints(
                query_locations=sorted(
                    {loc for s in sources for loc in (s.hints or {}).get("query_locations", []) if loc}
                ),
                query_niches=sorted({n for s in sources for n in (s.hints or {}).get("query_niches", []) if n}),
            )
            stored_check = dict(lead.website_check or {})
            min_score = self.s.gate.min_score_for(lead.campaign_id)
        signals = self.s.analyzer.signals(obs, now)
        website: WebsiteCheck | None = None
        if cfg.check_websites and signals.own_website:
            checked_at = (
                utc(datetime.fromisoformat(stored_check["checked_at"])) if stored_check.get("checked_at") else None
            )
            fresh = (
                checked_at is not None
                and stored_check.get("url") == signals.own_website
                and now - checked_at < timedelta(days=cfg.website_recheck_days)
            )
            if fresh:
                website = WebsiteCheck.model_validate(stored_check)
            else:
                website = await self.s.website_checker.check(signals.own_website)
        analysis = self.s.analyzer.analyze(obs, now, website, hints, min_score)
        with self.s.db.session() as session:
            lead = session.get(Lead, lead_id)
            if lead is None:
                return None
            s = analysis.signals
            lead.signals = s.as_dict() | {
                "facts": analysis.facts,
                "hints": {"query_locations": hints.query_locations, "query_niches": hints.query_niches},
            }
            lead.website_check = website.as_dict() if website else {}
            lead.website_domain = registrable_domain(s.own_website) if s.own_website else None
            lead.opportunities = [o.as_dict() for o in analysis.opportunities]
            lead.hooks = analysis.hooks
            lead.score, lead.score_breakdown = analysis.score, analysis.breakdown
            lead.disqualify_reasons = analysis.disqualify_reasons
            lead.niche, lead.location_match, lead.themes = s.niche, s.location, s.themes
            if s.activity:
                lead.last_post_at, lead.posts_last_30d = s.activity.last_post_at, s.activity.posts_last_30d
            lead.analyzed_at = now
            if lead.status not in (
                LeadStatus.DISCOVERED,
                LeadStatus.ANALYZED,
                LeadStatus.QUALIFIED,
                LeadStatus.DISQUALIFIED,
            ):
                return lead.status  # already in outreach/conversation: refresh facts only
            duplicate = register_and_find_duplicate(session, lead)
            if duplicate is not None:
                other, why = duplicate
                lead.status, lead.duplicate_of_id, lead.status_reason = LeadStatus.DUPLICATE, other.id, why
            elif analysis.disqualified:
                lead.status, lead.status_reason = LeadStatus.DISQUALIFIED, "; ".join(analysis.disqualify_reasons)
            elif analysis.qualified:
                top = analysis.top_opportunity
                lead.status = LeadStatus.QUALIFIED
                lead.status_reason = (
                    f"score {analysis.score}; {top.type.value}: {top.rationale}" if top else f"score {analysis.score}"
                )
            else:
                lead.status, lead.status_reason = LeadStatus.ANALYZED, analysis.not_qualified_reason
            return lead.status

    # -------------------------------------------------------- PERSONALIZE + GATE
    async def plan_outreach(self, mode: OperatingMode, limit: int = 5) -> int:
        if not mode.prepares_outreach:
            return 0
        created = 0
        with self.s.db.session() as session:
            # Lead status drives planning: QUALIFIED means "no outreach in flight". Every
            # outcome of an outreach action moves the lead out of QUALIFIED, except
            # expiry / reject-and-redraft which deliberately put it back.
            query = select(Lead).where(Lead.status == LeadStatus.QUALIFIED)
            sandbox = sandbox_targets(self.settings)
            if sandbox:  # others stay QUALIFIED and are drafted once the sandbox is lifted
                query = query.where(Lead.username.in_(sorted(sandbox)))
            leads = session.scalars(query.order_by(Lead.score.desc(), Lead.id).limit(limit)).all()
            can_private_reply = bool(self.s.executor.candidate_channels(Capability.PRIVATE_REPLY))
            batch = []
            for lead in leads:
                if not self._allowed_username(lead.username):
                    continue
                version = 1 + (
                    session.scalar(
                        select(func.count())
                        .select_from(Action)
                        .where(Action.lead_id == lead.id, Action.type == ActionType.SEND_OUTREACH)
                    )
                    or 0
                )
                facts = dict((lead.signals or {}).get("facts", {}))
                capability, reply_to = Capability.SEND_NEW_DM, {}
                comment = self._recent_comment(session, lead.id)
                if comment is not None:
                    text = ((comment.hints or {}).get("comment_text") or [""])[0]
                    facts["their_comment"] = truncate(collapse_whitespace(text), 200) or "(a comment on our post)"
                    # Expired or rejected drafts never ran, so they do not use up the private reply.
                    tried = session.scalar(
                        select(func.count())
                        .select_from(Action)
                        .where(
                            Action.lead_id == lead.id,
                            Action.capability == Capability.PRIVATE_REPLY,
                            Action.attempts > 0,
                        )
                    )
                    if can_private_reply and not tried:
                        # Official API, no browser: answer their comment privately (once, within 7 days).
                        capability = Capability.PRIVATE_REPLY
                        commented_at = ((comment.hints or {}).get("commented_at") or [None])[0]
                        reply_to = {
                            "comment_id": comment.seed,
                            "comment_at": commented_at or comment.discovered_at.isoformat(),
                        }
                batch.append((lead.id, version, facts, list(lead.opportunities or []), capability, reply_to))
            recent = self._recent_automation_texts(session) + self._recent_drafts(session)
        for lead_id, version, facts, opportunities, capability, reply_to in batch:
            top = max(opportunities, key=lambda o: o.get("weight", 0), default=None)
            opportunity = (OpportunityType(top["type"]), str(top["rationale"])) if top else None
            try:
                composed = await self.s.composer.compose_initial(
                    facts=facts, opportunity=opportunity, recent_texts=recent
                )
            except CompositionFailed as exc:
                log.warning("lead %s: could not compose a valid first message: %s", lead_id, exc)
                with self.s.db.session() as session:
                    failed = session.get(Lead, lead_id)
                    if failed is not None:
                        failed.status, failed.status_reason = LeadStatus.ANALYZED, f"composition failed: {exc}"[:512]
                continue
            recent.insert(0, composed.text)
            created += self._queue_outbound(
                mode,
                ActionType.SEND_OUTREACH,
                capability,
                lead_id,
                key=f"outreach:{self._account}:{lead_id}:v{version}",
                text=composed.text,
                kind="initial",
                facts_used=composed.facts_used,
                composer=composed.composer,
                extra={
                    "opportunity": composed.opportunity,
                    "rejected_drafts": composed.rejected_drafts[-3:],
                    **reply_to,
                },
            )
        return created

    def _queue_outbound(
        self,
        mode: OperatingMode,
        action_type: ActionType,
        capability: Capability,
        lead_id: int | None,
        *,
        key: str,
        text: str,
        kind: str,
        facts_used: list[str],
        composer: str,
        conversation_id: int | None = None,
        followup_number: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> int:
        status, approved_by = status_for_new_outbound(mode, action_type, self.settings.replies.autonomous)
        limits = self.s.runtime.limits()
        with self.s.db.session() as session:
            lead = session.get(Lead, lead_id) if lead_id else None
            conv: Conversation | None
            if conversation_id is None:
                if lead is None:
                    return 0
                conv = self.s.ownership.get_or_create(
                    session, self._account, peer_username=lead.username, peer_igsid=lead.igsid, lead_id=lead.id
                )
                conversation_id = conv.id
            conv = session.get(Conversation, conversation_id)
            action, created = self.s.actions.create(
                session,
                key=key,
                account_id=self._account,
                type=action_type,
                capability=capability,
                status=status,
                mode=mode,
                lead_id=lead_id,
                conversation_id=conversation_id,
                target_username=(lead.username if lead else conv.peer_username if conv else None),
                campaign_id=lead.campaign_id if lead else None,
                params=extra or {},
                message_text=text,
                message_kind=kind,
                followup_number=followup_number,
                facts_used=facts_used,
                composer=composer,
                approved_by=approved_by,
                max_attempts=limits.retry_max_attempts,
            )
            if not created:
                return 0
            decision = self.s.gate.preview(session, action, self.s.executor.candidate_channels(capability), limits)
            action.gate = {"proposal": decision.as_dict()}
            label = _outbound_label(action_type, capability, followup_number)
            target = f"@{action.target_username}"
            if decision.outcome is GateOutcome.DENY:
                action.status, action.status_reason = ActionStatus.BLOCKED, "; ".join(decision.reasons)
                if lead is not None and action_type is ActionType.SEND_OUTREACH:
                    self._lead_after_block(lead, decision.reasons)
                audit(
                    session,
                    self._now(),
                    actor="system",
                    kind="action.blocked",
                    subject=target,
                    summary=f"{label} to {target} BLOCKED before reaching the queue: {'; '.join(decision.reasons)}",
                    action_id=action.id,
                    reasons=decision.reasons,
                )
                return 0
            if lead is not None and action_type is ActionType.SEND_OUTREACH:
                lead.status = LeadStatus.OUTREACH_PENDING
            audit(
                session,
                self._now(),
                actor="system",
                kind="action.proposed",
                subject=target,
                summary=f"{label} to {target} {_QUEUE_VERB[status]} ({composer} text; mode {mode.value})",
                action_id=action.id,
                status=status,
                capability=capability,
                composer=composer,
                facts_used=facts_used,
                opportunity=(extra or {}).get("opportunity"),
            )
            return 1

    @staticmethod
    def _lead_after_block(lead: Lead, reasons: list[str]) -> None:
        """A blocked proposal must always move the lead out of QUALIFIED,
        otherwise it would be re-drafted on every tick."""
        text = "; ".join(reasons)
        if "suppressed" in text:
            lead.status = LeadStatus.CLOSED
        elif "already contacted" in text:
            lead.status = LeadStatus.CONTACTED
        elif "human" in text:
            lead.status = LeadStatus.HANDED_OFF
        else:
            lead.status = LeadStatus.DISQUALIFIED
        lead.status_reason = f"outreach blocked: {text}"[:512]

    async def plan_followups(self, mode: OperatingMode, limit: int = 5) -> int:
        if not mode.prepares_outreach:
            return 0
        limits = self.s.runtime.limits()
        now = self._now()
        days = limits.followup_after_days
        created = 0
        with self.s.db.session() as session:
            query = select(Lead).where(
                Lead.status == LeadStatus.CONTACTED,
                Lead.replied_at.is_(None),
                Lead.followups_sent < limits.max_followups_per_lead,
                Lead.followups_blocked_reason.is_(None),
                Lead.last_outbound_at.is_not(None),
            )
            sandbox = sandbox_targets(self.settings)
            if sandbox:
                query = query.where(Lead.username.in_(sorted(sandbox)))
            leads = session.scalars(query.order_by(Lead.last_outbound_at).limit(limit * 4)).all()
            due = []
            for lead in leads:
                number = lead.followups_sent + 1
                gap = days[min(number - 1, len(days) - 1)] if days else 3
                if self.s.actions.get_by_key(session, f"followup:{self._account}:{lead.id}:{number}"):
                    continue  # already prepared (or expired/rejected): never recompose
                if lead.last_outbound_at and now >= lead.last_outbound_at + timedelta(days=gap):
                    conv = self.s.ownership.find(session, self._account, peer_username=lead.username)
                    if conv is None or conv.automation_paused:
                        continue
                    previous = session.scalars(
                        select(Message.text)
                        .where(Message.conversation_id == conv.id, Message.direction == MessageDirection.OUTBOUND)
                        .order_by(Message.created_at.desc())
                    ).first()
                    due.append((lead.id, conv.id, number, dict((lead.signals or {}).get("facts", {})), previous or ""))
                if len(due) >= limit:
                    break
            recent = self._recent_automation_texts(session)
        for lead_id, conv_id, number, facts, previous in due:
            try:
                composed = await self.s.composer.compose_followup(
                    facts=facts, number=number, previous_message=previous, recent_texts=recent
                )
            except CompositionFailed as exc:
                log.warning("lead %s: follow-up composition failed: %s", lead_id, exc)
                continue
            created += self._queue_outbound(
                mode,
                ActionType.SEND_FOLLOW_UP,
                Capability.SEND_DM_REPLY,
                lead_id,
                key=f"followup:{self._account}:{lead_id}:{number}",
                text=composed.text,
                kind="followup",
                facts_used=composed.facts_used,
                composer=composed.composer,
                conversation_id=conv_id,
                followup_number=number,
            )
        return created

    # ------------------------------------------------------------------ INBOUND
    async def process_events(self, events: list[InboundEvent]) -> dict[str, int]:
        stats = {"inbound": 0, "echo_ours": 0, "echo_human": 0, "comments": 0, "ignored": 0}
        for event in events:
            if event.kind == "comment":
                stats["comments" if self._on_comment(event) else "ignored"] += 1
                continue
            username = try_canonical_username(event.peer_username) if event.peer_username else None
            if username is None and event.peer_igsid:
                username = await self._username_for_igsid(event.peer_igsid)
            if event.kind not in ("message", "echo") or not (username or event.peer_igsid):
                stats["ignored"] += 1
                continue
            if username and not self._allowed_username(username):
                stats["ignored"] += 1
                continue
            with self.s.db.session() as session:
                if username is None and event.kind == "echo":
                    matched = self.s.ownership.match_automation_echo(
                        session, self._account, text=event.text or "", at=event.at
                    )
                    if matched is not None and matched.peer_username:
                        username = matched.peer_username
                lead = session.scalars(select(Lead).where(Lead.username == username)).first() if username else None
                known = None
                if username:
                    known = self.s.ownership.find(session, self._account, peer_username=username)
                if known is None and event.peer_igsid:
                    known = self.s.ownership.find(session, self._account, peer_igsid=event.peer_igsid)
                if lead is None and known is None:
                    # Privacy: the account's other DMs (friends, unrelated chats) are never stored.
                    stats["ignored"] += 1
                    continue
                if lead is not None and event.peer_igsid and not lead.igsid:
                    lead.igsid = event.peer_igsid
                conv = self.s.ownership.get_or_create(
                    session,
                    self._account,
                    peer_username=username,
                    peer_igsid=event.peer_igsid,
                    lead_id=lead.id if lead else None,
                )
                if event.kind == "message":
                    msg = self.s.ownership.record_inbound(
                        session,
                        conv,
                        text=event.text or "",
                        platform_message_id=event.platform_message_id,
                        at=event.at,
                        observed_via=event.source,
                    )
                    if msg is not None:
                        stats["inbound"] += 1
                        self._on_new_inbound(session, conv, event.at)
                else:
                    kind = self.s.ownership.record_echo(
                        session, conv, text=event.text or "", platform_message_id=event.platform_message_id, at=event.at
                    )
                    if kind is SenderKind.HUMAN:
                        stats["echo_human"] += 1
                        self.s.incidents.notify_later(
                            f"Rohit took over @{username or event.peer_igsid}",
                            "A message was sent from the Instagram app; automation paused for this conversation.",
                            IncidentSeverity.INFO,
                            conversation_id=conv.id,
                        )
                    else:
                        stats["echo_ours"] += 1
        return stats

    def _on_comment(self, event: InboundEvent) -> bool:
        """A comment on one of our own posts: the warmest possible lead.

        The commenter enters the normal pipeline (inspect -> score -> gate); if
        they qualify, the first message can be an official API *private reply*
        to the comment (allowed once, within 7 days), no browser needed.
        """
        username = try_canonical_username(event.peer_username)
        if not username or not event.comment_id or not self._allowed_username(username):
            return False
        if keyword_intent(event.text or "") in (ReplyIntent.OPT_OUT, ReplyIntent.NOT_INTERESTED):
            return False  # a negative comment is never a reason to message someone
        campaign = next((c.id for c in self.settings.campaigns if c.enabled), None)
        task = DiscoveryTask(
            campaign_id=campaign or "",
            strategy=OWN_POST_COMMENT,
            capability=Capability.PRIVATE_REPLY,
            query_key=f"comment|{event.media_id or ''}",
            seed=event.comment_id,
            hints={"comment_text": [(event.text or "")[:300]], "commented_at": [event.at.isoformat()]},
        )
        with self.s.db.session() as session:
            self.upsert_candidates(session, task, [CandidateRef(username=username, snippet=event.text)], None)
            lead = session.scalars(select(Lead).where(Lead.username == username)).first()
            if lead is not None and event.peer_igsid and not lead.igsid:
                lead.igsid = event.peer_igsid
        return True

    def _recent_comment(self, session: Session, lead_id: int) -> LeadSource | None:
        window_start = self._now() - PRIVATE_REPLY_WINDOW
        return session.scalars(
            select(LeadSource)
            .where(
                LeadSource.lead_id == lead_id,
                LeadSource.strategy == OWN_POST_COMMENT,
                LeadSource.discovered_at >= window_start,
            )
            .order_by(LeadSource.discovered_at.desc())
        ).first()

    async def _username_for_igsid(self, igsid: str) -> str | None:
        with self.s.db.session() as session:
            lead = session.scalars(select(Lead).where(Lead.igsid == igsid)).first()
            if lead is not None:
                return lead.username
            conv = self.s.ownership.find(session, self._account, peer_igsid=igsid)
            if conv is not None and conv.peer_username:
                return conv.peer_username
        if self.s.identity_resolver is not None:
            try:
                return try_canonical_username(await self.s.identity_resolver(igsid) or "")
            except Exception as exc:
                log.warning("could not resolve IGSID %s: %s", igsid, exc)
        return None

    def _on_new_inbound(self, session: Session, conv: Conversation, at: datetime) -> None:
        if conv.lead_id is None:
            return
        lead = session.get(Lead, conv.lead_id)
        if lead is None:
            return
        lead.last_inbound_at = max(filter(None, [lead.last_inbound_at, at]))
        if lead.status in (LeadStatus.CONTACTED, LeadStatus.OUTREACH_PENDING, LeadStatus.QUALIFIED):
            lead.status, lead.replied_at, lead.status_reason = LeadStatus.REPLIED, at, "prospect replied"
        # Never follow up on someone who answered.
        for action in session.scalars(
            select(Action).where(
                Action.lead_id == lead.id,
                Action.type.in_([ActionType.SEND_FOLLOW_UP, ActionType.SEND_OUTREACH]),
                Action.status.in_(
                    [
                        ActionStatus.DRAFTED,
                        ActionStatus.PENDING_APPROVAL,
                        ActionStatus.APPROVED,
                        ActionStatus.NEEDS_HUMAN,
                        ActionStatus.PROPOSED,
                    ]
                ),
            )
        ):
            action.status, action.status_reason = ActionStatus.CANCELLED, "prospect replied"

    def reconcile_thread(self, session: Session, conv: Conversation, snapshot: ThreadSnapshot, via: str) -> None:
        outcome = self.s.ownership.reconcile(session, conv, snapshot, via)
        if outcome.new_inbound:
            self._on_new_inbound(session, conv, max(m.sent_at or self._now() for m in outcome.new_inbound))
        if outcome.human_took_over:
            self.s.incidents.notify_later(
                f"Rohit is active in the thread with @{conv.peer_username}",
                "Found outbound messages automation did not send; automation paused for this conversation.",
                IncidentSeverity.INFO,
                conversation_id=conv.id,
            )

    async def plan_replies(self, mode: OperatingMode, limit: int = 10) -> int:
        """Classify unhandled inbound messages and decide the next step."""
        with self.s.db.session() as session:
            rows = session.scalars(
                select(Message)
                .where(Message.direction == MessageDirection.INBOUND, Message.handled.is_(False))
                .order_by(Message.created_at)
                .limit(limit)
            ).all()
            pending = [(m.id, m.conversation_id, m.text) for m in rows]
        handled = 0
        for message_id, conv_id, text in pending:
            intent, why = await self.s.composer.classify_reply(text)
            await self._act_on_reply(mode, message_id, conv_id, text, intent, why)
            handled += 1
        return handled

    async def _act_on_reply(
        self, mode: OperatingMode, message_id: int, conv_id: int, text: str, intent: ReplyIntent, why: str
    ) -> None:
        suggestion: str | None = None
        facts: dict[str, str] = {}
        history: list[tuple[str, str]] = []
        with self.s.db.session() as session:
            msg = session.get(Message, message_id)
            conv = session.get(Conversation, conv_id)
            if msg is None or conv is None:
                return
            msg.intent, msg.handled = intent, True
            lead = session.get(Lead, conv.lead_id) if conv.lead_id else None
            who = f"@{conv.peer_username or conv.peer_igsid}"
            if intent in (ReplyIntent.OPT_OUT, ReplyIntent.NOT_INTERESTED):
                source = "OPT_OUT" if intent is ReplyIntent.OPT_OUT else "NEGATIVE_REPLY"
                reason = f"{intent.value}: {text[:200]}"
                # The business, not just this handle: branch accounts share a website/phone/email.
                keys: list[tuple[SuppressionKind, str | None]] = [
                    (SuppressionKind.USERNAME, conv.peer_username),
                    (SuppressionKind.IGSID, conv.peer_igsid),
                ]
                if lead is not None:
                    contact = lead.contact or {}
                    keys.append((SuppressionKind.DOMAIN, lead.website_domain))
                    keys += [(SuppressionKind.EMAIL, e) for e in contact.get("emails", [])]
                    keys += [(SuppressionKind.PHONE, p) for p in contact.get("phones", [])]
                for kind, value in keys:
                    if value:
                        with contextlib.suppress(ValueError):  # unparseable identifier: nothing to key on
                            add_suppression(session, kind, value, reason, source, self._now())
                self.s.ownership.cancel_open_outbound(session, conv, f"prospect replied {intent.value}")
                if lead is not None:
                    lead.status, lead.status_reason = LeadStatus.CLOSED, f"{intent.value} ({why})"
                audit(
                    session,
                    self._now(),
                    actor="system",
                    kind="reply.handled",
                    subject=who,
                    summary=f"reply classified {intent.value} ({why}) -> suppressed: "
                    + ", ".join(f"{kind.value}={value}" for kind, value in keys if value),
                    conversation_id=conv.id,
                    intent=intent,
                    reply=text[:300],
                    suppressed=[f"{kind.value}:{value}" for kind, value in keys if value],
                )
                self.s.incidents.notify_later(
                    f"{who} replied {intent.value}; suppressed",
                    text[:300],
                    IncidentSeverity.INFO,
                    conversation_id=conv.id,
                )
                return
            if intent is ReplyIntent.NEUTRAL:
                self.s.incidents.notify_later(
                    f"New reply from {who}", text[:300], IncidentSeverity.INFO, conversation_id=conv.id
                )
                audit(
                    session,
                    self._now(),
                    actor="system",
                    kind="reply.handled",
                    subject=who,
                    summary=f"reply classified NEUTRAL ({why}) -> Rohit notified, nothing sent",
                    conversation_id=conv.id,
                    intent=intent,
                    reply=text[:300],
                )
                return
            facts = dict((lead.signals or {}).get("facts", {})) if lead else {}
            history = [
                ("us" if m.direction is MessageDirection.OUTBOUND else "prospect", m.text)
                for m in session.scalars(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
                )
            ]
        try:
            composed = await self.s.composer.compose_reply(facts=facts, history=history, inbound=text, recent_texts=[])
            suggestion = composed.text
        except CompositionFailed:
            composed = None
        if self.settings.replies.handoff_on_interest or not mode.prepares_outreach or composed is None:
            with self.s.db.session() as session:
                conv = session.get(Conversation, conv_id)
                if conv is None:
                    return
                self.s.ownership.take_over_by_human(session, conv, f"handed off: prospect replied ({intent.value})")
                audit(
                    session,
                    self._now(),
                    actor="system",
                    kind="reply.handled",
                    subject=f"@{conv.peer_username}",
                    summary=f"reply classified {intent.value} ({why}) -> handed off to Rohit"
                    + (" with a suggested reply" if suggestion else ""),
                    conversation_id=conv.id,
                    intent=intent,
                    reply=text[:300],
                    suggestion=suggestion,
                )
                self.s.incidents.notify_later(
                    f"Warm lead: @{conv.peer_username} replied ({intent.value}) — over to you",
                    f"They wrote: {text[:300]}" + (f"\nSuggested reply: {suggestion}" if suggestion else ""),
                    IncidentSeverity.WARNING,
                    conversation_id=conv.id,
                    intent=intent.value,
                )
            return
        with self.s.db.session() as session:
            conv = session.get(Conversation, conv_id)
            lead_id = conv.lead_id if conv else None
        self._queue_outbound(
            mode,
            ActionType.SEND_REPLY,
            Capability.SEND_DM_REPLY,
            lead_id,
            key=f"reply:{self._account}:{conv_id}:{message_id}",
            text=composed.text,
            kind="reply",
            facts_used=composed.facts_used,
            composer=composed.composer,
            conversation_id=conv_id,
            extra={"in_reply_to_message_id": message_id, "intent": intent.value},
        )

    # --------------------------------------------------------------- INBOX SYNC
    def plan_inbox_sync(self, force: bool = False) -> int:
        now = self._now()
        interval = timedelta(minutes=self.settings.schedule.inbox_sync_interval_minutes)
        if not force and self._last_inbox_plan and now - self._last_inbox_plan < interval:
            return 0
        self._last_inbox_plan = now
        slot = int(now.timestamp() // interval.total_seconds())
        with self.s.db.session() as session:
            waiting = session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.status.in_([LeadStatus.CONTACTED, LeadStatus.REPLIED]))
            )
            if not waiting:
                return 0
            _, created = self.s.actions.create(
                session,
                key=f"sync_inbox:{self._account}:{slot}",
                account_id=self._account,
                type=ActionType.SYNC_INBOX,
                capability=Capability.READ_INBOX,
                status=ActionStatus.APPROVED,
                mode=self.s.runtime.mode(),
                params={"limit": 30},
                approved_by=AUTO_APPROVER,
                max_attempts=1,
            )
            return int(created)

    def handle_inbox_result(self, session: Session, action: Action, result: ExecutionResult) -> int:
        """Queue thread syncs only for lead conversations that changed since last seen.

        API entries carry the conversation's ``updated_time``; browser entries
        only a preview, compared against the last stored message.
        """
        if not result.ok:
            return 0
        created = 0
        slot = action.id
        for entry in result.data.get("inbox", []):
            username = try_canonical_username(entry.get("peer_username"))
            conv = None
            if username:
                conv = self.s.ownership.find(session, self._account, peer_username=username)
            elif entry.get("thread_id"):  # browser inbox rows carry thread ids, not usernames
                conv = session.scalars(
                    select(Conversation).where(
                        Conversation.account_id == self._account, Conversation.browser_thread_id == entry["thread_id"]
                    )
                ).first()
                username = conv.peer_username if conv is not None else None
            if conv is None or not username or conv.lead_id is None or conv.automation_paused:
                continue  # not our lead, or the human owns it: never touched
            if entry.get("peer_igsid") and not conv.peer_igsid:
                conv.peer_igsid = entry["peer_igsid"]
            updated_raw = entry.get("updated_time")
            if updated_raw:
                updated = utc(datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00")))
                known = max(
                    filter(None, [conv.last_inbound_at, conv.last_outbound_at, conv.last_synced_at]), default=None
                )
                if updated is None or (known is not None and updated <= known):
                    continue
            else:
                preview = (entry.get("last_message_preview") or "").rstrip("…").strip()
                if not preview:
                    continue  # nothing to compare against: rely on webhooks
                last = session.scalars(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at.desc())
                ).first()
                if last is not None and last.text.strip().startswith(preview):
                    continue
            _, was_created = self.s.actions.create(
                session,
                key=f"sync_thread:{conv.id}:{slot}",
                account_id=self._account,
                type=ActionType.SYNC_THREAD,
                capability=Capability.READ_THREAD,
                status=ActionStatus.APPROVED,
                mode=self.s.runtime.mode(),
                lead_id=conv.lead_id,
                conversation_id=conv.id,
                target_username=username,
                approved_by=AUTO_APPROVER,
                max_attempts=2,
            )
            created += int(was_created)
        return created
