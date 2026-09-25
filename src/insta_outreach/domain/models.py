"""Data transfer objects crossing the orchestrator <-> executor boundary.

Executors receive an :class:`OperationRequest` and return an
:class:`ExecutionResult`. Nothing else crosses the boundary: an executor
cannot pick targets or compose text, it can only carry out the request.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from insta_outreach.domain.enums import (
    Capability,
    Channel,
    ExecutionStatus,
    MessageDirection,
)
from insta_outreach.util.text import normalize_message_text


class PostObservation(BaseModel):
    url: str | None = None
    shortcode: str | None = None
    posted_at: datetime | None = None
    caption: str | None = None
    media_type: str | None = None  # IMAGE / VIDEO / CAROUSEL_ALBUM / REEL
    like_count: int | None = None
    comment_count: int | None = None
    alt_text: str | None = None


class ContactInfo(BaseModel):
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    whatsapp: list[str] = Field(default_factory=list)
    address: str | None = None


class ProfileObservation(BaseModel):
    """Publicly observable facts about one account, as seen by an executor."""

    username: str
    ig_user_id: str | None = None
    full_name: str | None = None
    biography: str | None = None
    category: str | None = None
    is_business: bool | None = None
    is_private: bool | None = None
    is_verified: bool | None = None
    website: str | None = None
    bio_links: list[str] = Field(default_factory=list)
    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None
    recent_posts: list[PostObservation] = Field(default_factory=list)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    observed_via: Channel
    observed_at: datetime


class CandidateRef(BaseModel):
    """A possible lead surfaced by a discovery operation (not yet analyzed)."""

    username: str
    full_name: str | None = None
    snippet: str | None = None
    source_post_url: str | None = None


class ThreadMessage(BaseModel):
    direction: MessageDirection
    text: str
    sent_at: datetime | None = None
    platform_message_id: str | None = None


class ThreadSnapshot(BaseModel):
    peer_username: str
    thread_id: str | None = None
    thread_url: str | None = None
    exists: bool = False  # any history at all between us and the peer
    messages: list[ThreadMessage] = Field(default_factory=list)
    request_pending: bool = False  # our message request not yet accepted


class InboundEvent(BaseModel):
    """Normalized platform event (Meta webhook, simulator, or inbox sync)."""

    kind: Literal["message", "echo", "comment", "mention", "read", "reaction", "referral", "edit"]
    peer_igsid: str | None = None
    peer_username: str | None = None
    text: str | None = None
    platform_message_id: str | None = None
    comment_id: str | None = None
    media_id: str | None = None
    at: datetime
    source: Literal["webhook", "simulator", "inbox_sync"] = "webhook"
    raw: dict[str, Any] = Field(default_factory=dict)


class InboxEntry(BaseModel):
    peer_username: str
    thread_id: str | None = None
    thread_url: str | None = None
    last_message_preview: str | None = None
    last_message_outbound: bool | None = None
    unread: bool | None = None


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_message_text(text).encode("utf-8")).hexdigest()


class ApprovedMessage(BaseModel):
    """The only way message text reaches an executor.

    Built by the execution worker from an APPROVED action row. Executors
    type exactly ``text`` and verify ``sha256`` before sending.
    """

    action_id: str
    text: str
    sha256: str
    kind: Literal["initial", "followup", "reply"]
    followup_number: int = 0
    # Hashes of messages our automation already sent in this conversation:
    # used for idempotent replays and to spot outbound messages we did NOT send
    # (i.e. the human is active in the thread).
    known_outbound_hashes: list[str] = Field(default_factory=list)

    def is_intact(self) -> bool:
        return text_sha256(self.text) == self.sha256


class ConversationRef(BaseModel):
    conversation_id: int
    peer_username: str
    peer_igsid: str | None = None
    api_thread_id: str | None = None
    browser_thread_id: str | None = None
    last_inbound_at: datetime | None = None
    lock_token: str | None = None


class OperationRequest(BaseModel):
    action_id: str
    account_id: str
    capability: Capability
    target_username: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    message: ApprovedMessage | None = None
    conversation: ConversationRef | None = None
    # Upper bound on page views / API calls this single operation may consume.
    max_units: int = 25


class Evidence(BaseModel):
    kind: Literal["screenshot", "html", "trace", "text", "api_response"]
    path: str | None = None
    content: str | None = None
    note: str | None = None
    captured_at: datetime


class ExecutionResult(BaseModel):
    status: ExecutionStatus
    capability: Capability
    channel: Channel
    code: str | None = None  # machine-readable sub-reason, e.g. "challenge_url"
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    page_url: str | None = None
    retry_after_seconds: float | None = None
    confirmed: bool = False  # outcome independently verified (e.g. bubble visible)
    units_used: int = 0
    simulated: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS
