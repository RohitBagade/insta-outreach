"""Shared vocabulary for every layer of the system."""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    """Where executors point.

    LOCAL: everything runs against the in-process simulated Instagram world.
    LIVE: the real Graph API adapter and the real Playwright browser agent.
    """

    LOCAL = "local"
    LIVE = "live"


class OperatingMode(StrEnum):
    """Runtime autonomy level. One implementation, switched at runtime."""

    OBSERVE = "OBSERVE"  # discover + analyze only
    DRAFT = "DRAFT"  # + prepare outreach, never send
    APPROVAL = "APPROVAL"  # + queue outreach for human approval, send approved items
    AUTONOMOUS = "AUTONOMOUS"  # + send eligible outreach automatically within limits

    @property
    def prepares_outreach(self) -> bool:
        return self is not OperatingMode.OBSERVE

    @property
    def may_send(self) -> bool:
        return self in (OperatingMode.APPROVAL, OperatingMode.AUTONOMOUS)


class Channel(StrEnum):
    """An execution lane technology. Lanes are (account, channel) pairs."""

    API = "API"
    BROWSER = "BROWSER"


class ExecutionStatus(StrEnum):
    """Structured outcome every executor operation must return.

    Executors never decide what happens next; the orchestrator maps these to
    retries, cooldowns, lane halts, lead updates and incidents.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CHECKPOINT_REQUIRED = "CHECKPOINT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
    UI_CHANGED = "UI_CHANGED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    ALREADY_CONTACTED = "ALREADY_CONTACTED"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    # Someone else (normally Rohit) owns the conversation: abort, do not act.
    OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
    # The platform refuses this operation for this target (messaging window
    # closed, recipient cannot be messaged, message request pending).
    NOT_PERMITTED = "NOT_PERMITTED"
    # No adapter could perform the capability (not configured, lane closed).
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    # Non-retryable error not covered above (bad input, unexpected API error).
    PERMANENT_FAILURE = "PERMANENT_FAILURE"

    @property
    def is_barrier(self) -> bool:
        """Security/anti-abuse barriers: stop the lane and surface to a human."""
        return self in _BARRIERS


_BARRIERS = frozenset(
    {
        ExecutionStatus.LOGIN_REQUIRED,
        ExecutionStatus.CHECKPOINT_REQUIRED,
        ExecutionStatus.ACCOUNT_RESTRICTED,
        ExecutionStatus.HUMAN_ACTION_REQUIRED,
    }
)


class Capability(StrEnum):
    """Bounded operations the orchestrator can ask an executor to perform."""

    # discovery (read-only)
    SEARCH_ACCOUNTS = "search_accounts"
    HASHTAG_POSTS = "hashtag_posts"
    LOCATION_POSTS = "location_posts"
    LIST_FOLLOWERS = "list_followers"
    LIST_FOLLOWING = "list_following"
    SUGGESTED_ACCOUNTS = "suggested_accounts"
    POST_ENGAGERS = "post_engagers"
    # analysis (read-only)
    INSPECT_PROFILE = "inspect_profile"
    # conversation state (read-only)
    READ_THREAD = "read_thread"
    READ_INBOX = "read_inbox"
    # outbound (writes)
    SEND_NEW_DM = "send_new_dm"
    SEND_DM_REPLY = "send_dm_reply"
    PRIVATE_REPLY = "private_reply"
    REPLY_COMMENT = "reply_comment"

    @property
    def is_outbound(self) -> bool:
        return self in _OUTBOUND

    @property
    def is_discovery(self) -> bool:
        return self in _DISCOVERY


_OUTBOUND = frozenset(
    {
        Capability.SEND_NEW_DM,
        Capability.SEND_DM_REPLY,
        Capability.PRIVATE_REPLY,
        Capability.REPLY_COMMENT,
    }
)
_DISCOVERY = frozenset(
    {
        Capability.SEARCH_ACCOUNTS,
        Capability.HASHTAG_POSTS,
        Capability.LOCATION_POSTS,
        Capability.LIST_FOLLOWERS,
        Capability.LIST_FOLLOWING,
        Capability.SUGGESTED_ACCOUNTS,
        Capability.POST_ENGAGERS,
    }
)


class ActionType(StrEnum):
    """Business intent of a queued action (one action -> one executor call)."""

    DISCOVER = "DISCOVER"
    INSPECT_PROFILE = "INSPECT_PROFILE"
    SEND_OUTREACH = "SEND_OUTREACH"
    SEND_FOLLOW_UP = "SEND_FOLLOW_UP"
    SEND_REPLY = "SEND_REPLY"
    SYNC_INBOX = "SYNC_INBOX"
    SYNC_THREAD = "SYNC_THREAD"

    @property
    def is_outbound(self) -> bool:
        return self in (
            ActionType.SEND_OUTREACH,
            ActionType.SEND_FOLLOW_UP,
            ActionType.SEND_REPLY,
        )


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    BLOCKED = "BLOCKED"  # gate denied at proposal time
    DRAFTED = "DRAFTED"  # DRAFT mode: prepared, never sent unless promoted
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"  # runnable once not_before passes and gates allow
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"  # human rejected in the approval queue
    NEEDS_HUMAN = "NEEDS_HUMAN"  # parked behind a lane barrier (checkpoint etc.)
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ActionStatus.BLOCKED,
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
            ActionStatus.EXPIRED,
        )


OPEN_ACTION_STATUSES = (
    ActionStatus.PROPOSED,
    ActionStatus.DRAFTED,
    ActionStatus.PENDING_APPROVAL,
    ActionStatus.APPROVED,
    ActionStatus.EXECUTING,
    ActionStatus.NEEDS_HUMAN,
)


class LeadStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    DUPLICATE = "DUPLICATE"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    HANDED_OFF = "HANDED_OFF"
    CLOSED = "CLOSED"
    UNREACHABLE = "UNREACHABLE"


class ConversationOwner(StrEnum):
    NONE = "NONE"
    HUMAN = "HUMAN"
    API_AGENT = "API_AGENT"
    BROWSER_AGENT = "BROWSER_AGENT"

    @staticmethod
    def for_channel(channel: Channel) -> ConversationOwner:
        return ConversationOwner.API_AGENT if channel is Channel.API else ConversationOwner.BROWSER_AGENT


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class SenderKind(StrEnum):
    PROSPECT = "PROSPECT"
    HUMAN = "HUMAN"  # Rohit, typing in the Instagram app/web himself
    API_AGENT = "API_AGENT"
    BROWSER_AGENT = "BROWSER_AGENT"
    UNKNOWN = "UNKNOWN"


class LaneState(StrEnum):
    ACTIVE = "ACTIVE"
    COOLDOWN = "COOLDOWN"  # time-boxed pause, resumes automatically
    HALTED = "HALTED"  # stopped until a human resumes it


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class IncidentSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SuppressionKind(StrEnum):
    USERNAME = "USERNAME"
    IGSID = "IGSID"
    DOMAIN = "DOMAIN"
    EMAIL = "EMAIL"
    PHONE = "PHONE"


class OpportunityType(StrEnum):
    """What LemmeDeliver could concretely offer this business."""

    NEW_WEBSITE = "NEW_WEBSITE"  # no website, or only a link-in-bio/marketplace page
    BROKEN_WEBSITE = "BROKEN_WEBSITE"  # linked site does not load
    WEBSITE_REBUILD = "WEBSITE_REBUILD"  # site on a DIY builder / dated platform
    ONLINE_BOOKING = "ONLINE_BOOKING"  # bookings happen over DM / WhatsApp / phone
    VISUAL_SHOWCASE = "VISUAL_SHOWCASE"  # strong active visual content, nowhere to showcase it


class ReplyIntent(StrEnum):
    INTERESTED = "INTERESTED"
    QUESTION = "QUESTION"
    NOT_INTERESTED = "NOT_INTERESTED"
    OPT_OUT = "OPT_OUT"
    NEUTRAL = "NEUTRAL"


class GateOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"  # permanent for this action: cancel it
    DEFER = "DEFER"  # temporary: retry at not_before (or when a lane reopens)
