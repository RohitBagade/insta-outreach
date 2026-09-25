"""Persistent state. Every business fact and every executor attempt lands here.

Tables are written so that the database alone answers: who was discovered and
how, what we observed, why a lead was (dis)qualified, what was sent to whom,
when, through which lane, with what evidence, and who owns each conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    Channel,
    ConversationOwner,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
    LaneState,
    LeadStatus,
    MessageDirection,
    OperatingMode,
    ReplyIntent,
    SenderKind,
    SuppressionKind,
)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, always returns aware UTC (SQLite drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime column")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def enum_col(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=40,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ig_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    igsid: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str | None] = mapped_column(String(256))
    biography: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(128))
    is_business: Mapped[bool | None] = mapped_column(Boolean)
    is_private: Mapped[bool | None] = mapped_column(Boolean)
    is_verified: Mapped[bool | None] = mapped_column(Boolean)
    website: Mapped[str | None] = mapped_column(String(512))
    website_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    bio_links: Mapped[list[str]] = mapped_column(JSON, default=list)
    followers: Mapped[int | None] = mapped_column(Integer)
    following: Mapped[int | None] = mapped_column(Integer)
    posts_count: Mapped[int | None] = mapped_column(Integer)
    contact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recent_posts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    last_post_at: Mapped[datetime | None]
    posts_last_30d: Mapped[int | None] = mapped_column(Integer)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)
    niche: Mapped[str | None] = mapped_column(String(64))
    location_match: Mapped[str | None] = mapped_column(String(64))
    website_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opportunities: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    hooks: Mapped[list[str]] = mapped_column(JSON, default=list)
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    disqualify_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[LeadStatus] = mapped_column(enum_col(LeadStatus), default=LeadStatus.DISCOVERED, index=True)
    status_reason: Mapped[str | None] = mapped_column(String(512))
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    campaign_id: Mapped[str | None] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime | None]
    analyzed_at: Mapped[datetime | None]
    contacted_at: Mapped[datetime | None]
    last_outbound_at: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    replied_at: Mapped[datetime | None]
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    followups_blocked_reason: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class LeadSource(Base):
    """Provenance: every way a lead was discovered."""

    __tablename__ = "lead_sources"
    __table_args__ = (UniqueConstraint("lead_id", "strategy", "query", "seed", name="uq_lead_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(String(256), default="")
    seed: Mapped[str] = mapped_column(String(256), default="")
    post_url: Mapped[str | None] = mapped_column(String(512))
    action_id: Mapped[str | None] = mapped_column(String(40))
    hints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(default=_now)


class LeadIdentity(Base):
    """Identity keys used for entity-level dedupe (one business, many accounts)."""

    __tablename__ = "lead_identities"
    __table_args__ = (
        UniqueConstraint("lead_id", "kind", "value", name="uq_lead_identity"),
        Index("ix_lead_identity_lookup", "kind", "value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # IG_USER_ID / DOMAIN / PHONE / EMAIL
    value: Mapped[str] = mapped_column(String(255))


class ProfileSnapshot(Base):
    """Raw observations over time (audit + re-analysis without re-scraping)."""

    __tablename__ = "profile_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    channel: Mapped[Channel] = mapped_column(enum_col(Channel))
    observed_at: Mapped[datetime]
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (Index("ix_discovery_runs_key", "strategy", "query_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    query_key: Mapped[str] = mapped_column(String(256))
    action_id: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PLANNED")
    found: Mapped[int] = mapped_column(Integer, default=0)
    new_leads: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None]


class Conversation(Base):
    """One conversation, one current owner. Shared by API and browser lanes."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("account_id", "peer_username", name="uq_conv_username"),
        UniqueConstraint("account_id", "peer_igsid", name="uq_conv_igsid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    peer_username: Mapped[str | None] = mapped_column(String(64))
    peer_igsid: Mapped[str | None] = mapped_column(String(64))
    api_thread_id: Mapped[str | None] = mapped_column(String(128))
    browser_thread_id: Mapped[str | None] = mapped_column(String(128))
    owner: Mapped[ConversationOwner] = mapped_column(enum_col(ConversationOwner), default=ConversationOwner.NONE)
    lock_token: Mapped[str | None] = mapped_column(String(64))
    lock_expires_at: Mapped[datetime | None]
    automation_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(String(512))
    paused_at: Mapped[datetime | None]
    pause_until: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    last_outbound_at: Mapped[datetime | None]
    last_human_activity_at: Mapped[datetime | None]
    last_synced_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    direction: Mapped[MessageDirection] = mapped_column(enum_col(MessageDirection))
    sender_kind: Mapped[SenderKind] = mapped_column(enum_col(SenderKind))
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    platform_message_id: Mapped[str | None] = mapped_column(String(256), unique=True)
    action_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # PENDING (send in flight), SENT, FAILED for automation messages; SENT for observed ones.
    delivery_state: Mapped[str] = mapped_column(String(16), default="SENT")
    observed_via: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime | None]
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    intent: Mapped[ReplyIntent | None] = mapped_column(enum_col(ReplyIntent))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Action(Base):
    """The unit of work and the audit record of an execution request."""

    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_runnable", "status", "not_before", "priority"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[ActionType] = mapped_column(enum_col(ActionType), index=True)
    capability: Mapped[Capability] = mapped_column(enum_col(Capability))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    target_username: Mapped[str | None] = mapped_column(String(64), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    message_text: Mapped[str | None] = mapped_column(Text)
    message_sha256: Mapped[str | None] = mapped_column(String(64))
    message_kind: Mapped[str | None] = mapped_column(String(16))
    followup_number: Mapped[int] = mapped_column(Integer, default=0)
    facts_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    composer: Mapped[str | None] = mapped_column(String(32))  # llm / template / human
    status: Mapped[ActionStatus] = mapped_column(enum_col(ActionStatus), index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    gate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode_at_creation: Mapped[OperatingMode] = mapped_column(enum_col(OperatingMode))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None]
    priority: Mapped[int] = mapped_column(Integer, default=50)
    not_before: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None]
    executed_channel: Mapped[Channel | None] = mapped_column(enum_col(Channel))
    last_result_status: Mapped[ExecutionStatus | None] = mapped_column(enum_col(ExecutionStatus))
    last_result_code: Mapped[str | None] = mapped_column(String(64))
    last_result_detail: Mapped[str | None] = mapped_column(Text)
    result_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    completed_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]


class ActionAttempt(Base):
    __tablename__ = "action_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str] = mapped_column(String(64))
    target_username: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[Channel | None] = mapped_column(enum_col(Channel))
    status: Mapped[ExecutionStatus] = mapped_column(enum_col(ExecutionStatus))
    code: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    message_text: Mapped[str | None] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(String(1024))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    units_used: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SuppressionKind] = mapped_column(enum_col(SuppressionKind))
    value: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32))  # OPT_OUT / MANUAL / NEGATIVE_REPLY / POLICY
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Incident(Base):
    """A barrier or anomaly that needs (or needed) a human."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[Channel | None] = mapped_column(enum_col(Channel))
    severity: Mapped[IncidentSeverity] = mapped_column(enum_col(IncidentSeverity))
    status: Mapped[IncidentStatus] = mapped_column(enum_col(IncidentStatus), default=IncidentStatus.OPEN, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    detail: Mapped[str | None] = mapped_column(Text)
    action_id: Mapped[str | None] = mapped_column(String(40))
    page_url: Mapped[str | None] = mapped_column(String(1024))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class Lane(Base):
    """Execution lane state: (account, channel). Stop-on-warning lives here."""

    __tablename__ = "lanes"
    __table_args__ = (UniqueConstraint("account_id", "channel", name="uq_lane"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[Channel] = mapped_column(enum_col(Channel))
    state: Mapped[LaneState] = mapped_column(enum_col(LaneState), default=LaneState.ACTIVE)
    until: Mapped[datetime | None]
    reason: Mapped[str | None] = mapped_column(Text)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"))
    rate_limit_events: Mapped[list[str]] = mapped_column(JSON, default=list)
    consecutive_ui_changed: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class UsageEvent(Base):
    """Rolling-window counters for hourly/daily caps (page views, sends, calls)."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_window", "account_id", "channel", "kind", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[Channel] = mapped_column(enum_col(Channel))
    kind: Mapped[str] = mapped_column(String(32))
    units: Mapped[int] = mapped_column(Integer, default=1)
    action_id: Mapped[str | None] = mapped_column(String(40))
    at: Mapped[datetime]


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class LearnedLocator(Base):
    """Locators the constrained LLM resolver derived after the UI changed."""

    __tablename__ = "learned_locators"
    __table_args__ = (UniqueConstraint("intent", "spec_key", name="uq_learned_locator"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    intent: Mapped[str] = mapped_column(String(64), index=True)
    spec_key: Mapped[str] = mapped_column(String(255))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    misses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    last_used_at: Mapped[datetime | None]


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(default=_now)
    object: Mapped[str | None] = mapped_column(String(32))
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
