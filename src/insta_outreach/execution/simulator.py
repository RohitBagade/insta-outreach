"""Simulated Instagram world for the LOCAL environment and tests.

Both simulated adapters (API-flavoured and browser-flavoured) implement the
same contracts and the same semantics as the real ones — pre-send thread
checks, ownership guard before sending, 24h API reply window, Business
Discovery only for professional accounts — plus fault injection for
checkpoints, rate limits, UI drift, login expiry and restrictions.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus, MessageDirection
from insta_outreach.domain.models import (
    ApprovedMessage,
    CandidateRef,
    ContactInfo,
    ExecutionResult,
    InboundEvent,
    InboxEntry,
    OperationRequest,
    PostObservation,
    ProfileObservation,
    ThreadMessage,
    ThreadSnapshot,
    text_sha256,
)
from insta_outreach.execution import sim_data
from insta_outreach.execution.base import PRIVATE_REPLY_MAX_AGE, OperationContext, private_reply_open, result
from insta_outreach.intelligence.website import WebsiteCheck
from insta_outreach.policy.usage import BudgetExhausted
from insta_outreach.util.clock import Clock
from insta_outreach.util.text import registrable_domain

_LOCATION_AREAS = {
    "mumbai": {
        "andheri",
        "bandra",
        "khar",
        "powai",
        "borivali",
        "dadar",
        "lower parel",
        "juhu",
        "malad",
        "chembur",
        "ghatkopar",
        "mulund",
        "kandivali",
        "worli",
        "vile parle",
        "mumbai",
    },
    "navi mumbai": {"vashi", "kharghar", "nerul", "panvel", "belapur"},
    "thane": {"thane"},
}
_NICHE_TERMS = {
    "dentist": ("dental", "dentist"),
    "dental": ("dental", "dentist"),
    "salon": ("salon", "hair", "barber", "nail"),
    "hair": ("hair", "salon"),
    "cafe": ("cafe", "coffee", "bakery", "cake", "cupcake"),
    "bakery": ("bakery", "cake", "cupcake"),
    "restaurant": ("restaurant", "food", "thali", "kitchen", "bao", "burger"),
    "pet": ("pet",),
    "boutique": ("boutique", "clothing", "saree", "lehenga", "ethnic"),
    "spa": ("spa", "massage", "wellness"),
    "gym": ("gym", "fitness", "yoga", "training"),
    "fitness": ("gym", "fitness", "yoga", "training"),
    "physio": ("physio",),
    "tattoo": ("tattoo",),
    "jewellery": ("jewel",),
    "clinic": ("clinic", "physio", "skin"),
}
_REPLY_CAPS = frozenset({Capability.SEND_NEW_DM, Capability.SEND_DM_REPLY})


@dataclass
class SimPost:
    shortcode: str
    posted_at: datetime
    caption: str
    media_type: str
    likes: int
    commenters: list[str] = field(default_factory=list)


@dataclass
class SimAccount:
    spec: sim_data.AccountSpec
    igsid: str
    ig_user_id: str
    posts: list[SimPost] = field(default_factory=list)
    exists: bool = True

    @property
    def username(self) -> str:
        return self.spec.username


@dataclass
class SimThread:
    peer: str
    thread_id: str
    messages: list[ThreadMessage] = field(default_factory=list)
    request_pending: bool = False
    first_outbound_at: datetime | None = None
    reply_sent: bool = False


@dataclass
class SimComment:
    comment_id: str
    username: str
    text: str
    media_id: str
    at: datetime
    private_replied: bool = False


@dataclass
class FaultPlan:
    login_expired: bool = False
    checkpoint_after_browser_ops: int | None = None
    rate_limit_after_sends: int | None = None
    restricted_after_sends: int | None = None
    ui_changed: set[Capability] = field(default_factory=set)
    transient_failures: dict[Capability, int] = field(default_factory=dict)
    api_token_expired: bool = False


class SimulatedWorld:
    our_username = "lemmedeliver"
    our_ig_id = "17840000000000001"

    def __init__(self, clock: Clock, seed: int = 7) -> None:
        self.clock = clock
        self.rng = random.Random(seed)
        self.accounts: dict[str, SimAccount] = {}
        self.threads: dict[str, SimThread] = {}
        self.posts: dict[str, tuple[str, SimPost]] = {}
        self.faults = FaultPlan()
        self.browser_ops = 0
        self.sends = 0
        self.events: list[InboundEvent] = []
        self.sent_log: list[dict[str, Any]] = []
        self.comments: dict[str, SimComment] = {}
        self._mid = itertools.count(1)
        self._cid = itertools.count(1)

    # -- construction -------------------------------------------------------------
    @classmethod
    def seeded(cls, clock: Clock, seed: int = 7) -> SimulatedWorld:
        world = cls(clock, seed)
        now = clock.now()
        for index, spec in enumerate(sim_data.ACCOUNTS):
            account = SimAccount(spec=spec, igsid=f"9{index:015d}", ig_user_id=f"178414{index:010d}")
            account.posts = world._generate_posts(spec, now)
            world.accounts[spec.username] = account
            for post in account.posts:
                world.posts[post.shortcode] = (spec.username, post)
            if spec.prior_human_thread:
                world.threads[spec.username] = SimThread(
                    peer=spec.username,
                    thread_id=f"t{index:06d}",
                    messages=[
                        ThreadMessage(
                            direction=MessageDirection.OUTBOUND,
                            text="Hi! Loved your book club evenings, do you host private events?",
                            sent_at=now - timedelta(days=10),
                        )
                    ],
                )
        return world

    def _generate_posts(self, spec: sim_data.AccountSpec, now: datetime) -> list[SimPost]:
        if spec.activity == "none":
            return []
        start = {"active": 1, "semi": 45, "inactive": 200}[spec.activity]
        gap = {"active": 3, "semi": 9, "inactive": 12}[spec.activity]
        themes = sim_data.CAPTION_THEMES.get(spec.niche or "", ("New post", "Weekend vibes", "Hello!"))
        posts = []
        for i in range(12):
            posted = now - timedelta(days=start + i * gap, hours=self.rng.randint(0, 20))
            tags = " ".join(f"#{t}" for t in spec.hashtags[:3])
            caption = f"{themes[i % len(themes)]} {tags}".strip()
            commenters = self.rng.sample(list(sim_data.FOLLOWER_POOL), k=2)
            posts.append(
                SimPost(
                    shortcode=f"{spec.username.replace('.', '')[:10]}{i:02d}",
                    posted_at=posted,
                    caption=caption,
                    media_type="REELS" if i % 3 == 0 else "IMAGE",
                    likes=self.rng.randint(10, 400),
                    commenters=commenters,
                )
            )
        return posts

    # -- queries used by adapters ---------------------------------------------------
    def account(self, username: str) -> SimAccount | None:
        account = self.accounts.get(username)
        return account if account and account.exists else None

    def profile(self, username: str, channel: Channel, with_category: bool = True) -> ProfileObservation | None:
        account = self.account(username)
        if account is None:
            return None
        spec = account.spec
        visible = not spec.is_private
        return ProfileObservation(
            username=spec.username,
            ig_user_id=account.ig_user_id,
            full_name=spec.full_name,
            biography=spec.bio,
            category=spec.category if with_category else None,
            is_business=spec.is_business,
            is_private=spec.is_private,
            is_verified=spec.followers > 100_000,
            website=spec.website,
            followers=spec.followers,
            following=self.rng.randint(80, 900),
            posts_count=len(account.posts),
            recent_posts=[
                PostObservation(
                    url=f"https://www.instagram.com/p/{p.shortcode}/",
                    shortcode=p.shortcode,
                    posted_at=p.posted_at,
                    caption=p.caption,
                    media_type=p.media_type,
                    like_count=p.likes,
                )
                for p in account.posts[:12]
            ]
            if visible
            else [],
            contact=ContactInfo(emails=[spec.email] if spec.email else [], phones=[spec.phone] if spec.phone else []),
            observed_via=channel,
            observed_at=self.clock.now(),
        )

    def search(self, query: str, limit: int) -> list[CandidateRef]:
        q = query.lower()
        niche_terms: set[str] = set()
        for key, terms in _NICHE_TERMS.items():
            if key in q:
                niche_terms.update(terms)
        areas: set[str] = set()
        for key in sorted(_LOCATION_AREAS, key=len, reverse=True):
            if key in q:
                areas |= _LOCATION_AREAS[key]
                q = q.replace(key, " ")
        scored = []
        for account in self.accounts.values():
            if not account.exists:
                continue
            spec = account.spec
            haystack = f"{spec.full_name} {spec.category or ''} {spec.bio} {spec.username}".lower()
            niche_hit = any(term in haystack for term in niche_terms) if niche_terms else True
            area_hit = bool(areas & {spec.area or ""}) or any(a in haystack for a in areas)
            if niche_hit and (area_hit or not areas):
                scored.append((2 if area_hit else 1, spec.followers, spec))
        scored.sort(key=lambda row: (-row[0], -row[1]))
        return [
            CandidateRef(
                username=s.username, full_name=s.full_name, snippet=f"{s.category or ''} • {s.followers} followers"
            )
            for _, _, s in scored[:limit]
        ]

    def hashtag_posts(self, tag: str, limit: int) -> list[CandidateRef]:
        tag = tag.lower().lstrip("#")
        hits = [(owner, post) for owner, post in self.posts.values() if f"#{tag}" in post.caption.lower()]
        hits.sort(key=lambda row: row[1].likes, reverse=True)
        return [
            CandidateRef(username=owner, source_post_url=f"https://www.instagram.com/p/{post.shortcode}/")
            for owner, post in hits[:limit]
        ]

    def suggested(self, username: str, limit: int) -> list[CandidateRef]:
        base = self.account(username)
        if base is None:
            return []
        same = [
            a for a in self.accounts.values() if a.exists and a.username != username and a.spec.niche == base.spec.niche
        ]
        return [CandidateRef(username=a.username, full_name=a.spec.full_name) for a in same[:limit]]

    def followers(self, username: str, limit: int) -> list[CandidateRef]:
        if self.account(username) is None:
            return []
        pool = [*[u for u in self.accounts if u != username][: limit - 1], "sim.deleted.account"]
        return [CandidateRef(username=u) for u in pool[:limit]]

    def engagers(self, post_url: str, limit: int) -> list[CandidateRef]:
        shortcode = post_url.rstrip("/").split("/")[-1]
        row = self.posts.get(shortcode)
        return [CandidateRef(username=u, source_post_url=post_url) for u in (row[1].commenters if row else [])][:limit]

    def snapshot(self, username: str) -> ThreadSnapshot:
        thread = self.threads.get(username)
        if thread is None:
            return ThreadSnapshot(peer_username=username, exists=False)
        return ThreadSnapshot(
            peer_username=username,
            thread_id=thread.thread_id,
            thread_url=f"https://www.instagram.com/direct/t/{thread.thread_id}/",
            exists=bool(thread.messages),
            messages=list(thread.messages),
            request_pending=thread.request_pending,
        )

    def website_check(self, url: str) -> WebsiteCheck:
        domain = registrable_domain(url)
        now = self.clock.now()
        spec = next(
            (
                a.spec
                for a in self.accounts.values()
                if a.spec.website and registrable_domain(a.spec.website) == domain and a.spec.site
            ),
            None,
        )
        if spec is None or spec.site is None:
            return WebsiteCheck(
                url=url,
                checked_at=now,
                reachable=True,
                status_code=200,
                final_url=url,
                https=url.startswith("https"),
                mobile_viewport=True,
                has_booking_widget=False,
            )
        site = spec.site
        if site.status >= 400:
            return WebsiteCheck(
                url=url,
                checked_at=now,
                reachable=False,
                status_code=site.status,
                final_url=url,
                error=f"HTTP {site.status}",
            )
        return WebsiteCheck(
            url=url,
            checked_at=now,
            reachable=True,
            status_code=site.status,
            final_url=url,
            https=site.https,
            builder=site.builder,
            mobile_viewport=site.viewport,
            has_booking_widget=site.booking_widget,
            title=spec.full_name,
        )

    # -- mutations (sending, prospect behaviour, the human) -------------------------------
    def deliver(self, username: str, text: str, channel: Channel) -> tuple[SimThread, str]:
        now = self.clock.now()
        thread = self.threads.get(username)
        if thread is None:
            thread = SimThread(peer=username, thread_id=f"t{len(self.threads) + 100:06d}", request_pending=True)
            self.threads[username] = thread
        mid = f"sim_mid_{next(self._mid)}"
        thread.messages.append(
            ThreadMessage(direction=MessageDirection.OUTBOUND, text=text, sent_at=now, platform_message_id=mid)
        )
        thread.first_outbound_at = thread.first_outbound_at or now
        self.sends += 1
        self.sent_log.append({"to": username, "text": text, "channel": channel.value, "at": now})
        # Meta echoes every outbound message (whoever sent it) to the webhook.
        account = self.account(username)
        self.events.append(
            InboundEvent(
                kind="echo",
                peer_igsid=account.igsid if account else None,
                peer_username=username,
                text=text,
                platform_message_id=mid,
                at=now,
                source="simulator",
            )
        )
        return thread, mid

    def prospect_reply(self, username: str, text: str) -> None:
        now = self.clock.now()
        thread = self.threads.setdefault(
            username, SimThread(peer=username, thread_id=f"t{len(self.threads) + 100:06d}")
        )
        mid = f"sim_mid_{next(self._mid)}"
        thread.messages.append(
            ThreadMessage(direction=MessageDirection.INBOUND, text=text, sent_at=now, platform_message_id=mid)
        )
        thread.request_pending = False
        thread.reply_sent = True
        account = self.account(username)
        self.events.append(
            InboundEvent(
                kind="message",
                peer_igsid=account.igsid if account else None,
                peer_username=username,
                text=text,
                platform_message_id=mid,
                at=now,
                source="simulator",
            )
        )

    def comment_on_our_post(self, username: str, text: str, media_id: str = "sim_media_1") -> str:
        """Someone comments on one of our own posts (delivered like the comments webhook)."""
        now = self.clock.now()
        comment_id = f"sim_comment_{next(self._cid)}"
        self.comments[comment_id] = SimComment(comment_id, username, text, media_id, now)
        account = self.account(username)
        self.events.append(
            InboundEvent(
                kind="comment",
                peer_igsid=account.igsid if account else None,
                peer_username=username,
                text=text,
                comment_id=comment_id,
                media_id=media_id,
                at=now,
                source="simulator",
            )
        )
        return comment_id

    def human_sends(self, username: str, text: str) -> None:
        """Rohit typing in the Instagram app himself."""
        self.deliver(username, text, Channel.BROWSER)
        self.sent_log[-1]["channel"] = "HUMAN"

    def run_prospect_behaviour(self) -> list[str]:
        """Scripted prospects reply some hours after our first message."""
        now = self.clock.now()
        replied = []
        for username, thread in self.threads.items():
            account = self.account(username)
            if account is None or thread.reply_sent or thread.first_outbound_at is None or not account.spec.reply:
                continue
            text, hours = account.spec.reply
            if now - thread.first_outbound_at >= timedelta(hours=hours):
                self.prospect_reply(username, text)
                replied.append(username)
        return replied

    def drain_events(self) -> list[InboundEvent]:
        events, self.events = self.events, []
        return events

    def igsid_to_username(self, igsid: str) -> str | None:
        return next((a.username for a in self.accounts.values() if a.igsid == igsid), None)


def _latest_iso(messages: list[ThreadMessage]) -> str | None:
    latest = max((m.sent_at for m in messages if m.sent_at), default=None)
    return latest.isoformat() if latest else None


class _SimAdapterBase:
    channel: Channel
    simulated = True

    def __init__(self, world: SimulatedWorld) -> None:
        self.world = world

    async def close(self) -> None:
        return None

    def _r(
        self, request: OperationRequest, status: ExecutionStatus, ctx: OperationContext, **kw: Any
    ) -> ExecutionResult:
        return result(request, self.channel, status, ctx=ctx, simulated=True, **kw)

    async def _unit(self, ctx: OperationContext) -> None:
        if ctx.pacer is not None:
            await ctx.pacer.unit()

    def _transient(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult | None:
        remaining = self.world.faults.transient_failures.get(request.capability, 0)
        if remaining > 0:
            self.world.faults.transient_failures[request.capability] = remaining - 1
            return self._r(
                request,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx,
                code="navigation_timeout",
                detail="simulated transient failure",
            )
        return None

    async def _send(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        """Shared send semantics (mirrors the real browser agent's send path)."""
        message: ApprovedMessage | None = request.message
        target = request.target_username or (request.conversation.peer_username if request.conversation else None)
        if message is None or not message.is_intact() or not target:
            return self._r(
                request,
                ExecutionStatus.PERMANENT_FAILURE,
                ctx,
                code="invalid_request",
                detail="missing or tampered approved message",
            )
        await self._unit(ctx)  # open profile / thread
        account = self.world.account(target)
        if account is None:
            return self._r(
                request,
                ExecutionStatus.TARGET_NOT_FOUND,
                ctx,
                code="profile_unavailable",
                detail=f"@{target} does not exist",
            )
        if not account.spec.accepts_messages:
            return self._r(
                request,
                ExecutionStatus.NOT_PERMITTED,
                ctx,
                code="cannot_message",
                detail="this account can't receive messages from you",
            )
        await self._unit(ctx)  # read thread
        snapshot = self.world.snapshot(target)
        thread_data = {"thread": snapshot.model_dump(mode="json")}
        outbound = [m for m in snapshot.messages if m.direction is MessageDirection.OUTBOUND]
        if any(text_sha256(m.text) == message.sha256 for m in outbound):
            return self._r(
                request,
                ExecutionStatus.SUCCESS,
                ctx,
                code="already_sent_idempotent",
                confirmed=True,
                data=thread_data | {"thread_id": snapshot.thread_id},
            )
        if message.kind == "initial" and snapshot.exists:
            return self._r(
                request,
                ExecutionStatus.ALREADY_CONTACTED,
                ctx,
                code="thread_has_history",
                detail="an existing conversation was found",
                data=thread_data,
            )
        unknown = [m for m in outbound if text_sha256(m.text) not in message.known_outbound_hashes]
        if unknown:
            return self._r(
                request,
                ExecutionStatus.OWNERSHIP_CONFLICT,
                ctx,
                code="human_message_in_thread",
                detail="thread contains outbound messages automation did not send",
                data=thread_data,
            )
        if message.kind == "followup" and account.spec.one_request_limit and snapshot.request_pending and outbound:
            return self._r(
                request,
                ExecutionStatus.NOT_PERMITTED,
                ctx,
                code="message_request_pending",
                detail="Instagram only allows more messages once the request is accepted",
                data=thread_data,
            )
        faults = self.world.faults
        if faults.restricted_after_sends is not None and self.world.sends >= faults.restricted_after_sends:
            return self._r(
                request,
                ExecutionStatus.ACCOUNT_RESTRICTED,
                ctx,
                code="action_blocked",
                detail="We restrict certain activity to protect our community",
            )
        if faults.rate_limit_after_sends is not None and self.world.sends >= faults.rate_limit_after_sends:
            return self._r(
                request,
                ExecutionStatus.RATE_LIMITED,
                ctx,
                code="try_again_later",
                detail="Try Again Later",
                retry_after=3600,
            )
        if not await ctx.guard():
            return self._r(request, ExecutionStatus.OWNERSHIP_CONFLICT, ctx, code="lease_lost_before_send")
        thread, mid = self.world.deliver(target, message.text, self.channel)
        return self._r(
            request,
            ExecutionStatus.SUCCESS,
            ctx,
            confirmed=True,
            data={
                "thread_id": thread.thread_id,
                "message_id": mid,
                "thread_url": f"https://www.instagram.com/direct/t/{thread.thread_id}/",
            },
        )


class SimulatedBrowserAdapter(_SimAdapterBase):
    channel = Channel.BROWSER

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.SEARCH_ACCOUNTS,
                Capability.HASHTAG_POSTS,
                Capability.LOCATION_POSTS,
                Capability.LIST_FOLLOWERS,
                Capability.LIST_FOLLOWING,
                Capability.SUGGESTED_ACCOUNTS,
                Capability.POST_ENGAGERS,
                Capability.INSPECT_PROFILE,
                Capability.READ_THREAD,
                Capability.READ_INBOX,
                Capability.SEND_NEW_DM,
                Capability.SEND_DM_REPLY,
            }
        )

    def supports(self, request: OperationRequest) -> bool:
        return request.capability in self.capabilities()

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        world, faults = self.world, self.world.faults
        world.browser_ops += 1
        if faults.login_expired:
            return self._r(
                request,
                ExecutionStatus.LOGIN_REQUIRED,
                ctx,
                code="login_page",
                page_url="https://www.instagram.com/accounts/login/",
            )
        if faults.checkpoint_after_browser_ops is not None and world.browser_ops > faults.checkpoint_after_browser_ops:
            return self._r(
                request,
                ExecutionStatus.CHECKPOINT_REQUIRED,
                ctx,
                code="challenge_url",
                page_url="https://www.instagram.com/challenge/",
                detail="Confirm it's you",
            )
        if request.capability in faults.ui_changed:
            return self._r(
                request,
                ExecutionStatus.UI_CHANGED,
                ctx,
                code="element_not_found",
                detail=f"no locator matched for {request.capability.value}",
            )
        transient = self._transient(request, ctx)
        if transient:
            return transient
        try:
            return await self._dispatch(request, ctx)
        except BudgetExhausted as exc:
            return self._r(request, ExecutionStatus.RETRYABLE_FAILURE, ctx, code="budget_exhausted", detail=str(exc))

    async def _dispatch(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        cap, params, world = request.capability, request.params, self.world
        limit = int(params.get("limit", 20))
        if cap in _REPLY_CAPS:
            return await self._send(request, ctx)
        if cap is Capability.INSPECT_PROFILE:
            await self._unit(ctx)
            profile = world.profile(request.target_username or "", Channel.BROWSER)
            if profile is None:
                return self._r(
                    request,
                    ExecutionStatus.TARGET_NOT_FOUND,
                    ctx,
                    code="profile_unavailable",
                    detail="Sorry, this page isn't available.",
                )
            return self._r(
                request, ExecutionStatus.SUCCESS, ctx, confirmed=True, data={"profile": profile.model_dump(mode="json")}
            )
        if cap is Capability.READ_THREAD:
            await self._unit(ctx)
            peer = request.target_username or (request.conversation.peer_username if request.conversation else "")
            return self._r(
                request,
                ExecutionStatus.SUCCESS,
                ctx,
                confirmed=True,
                data={"thread": world.snapshot(peer or "").model_dump(mode="json")},
            )
        if cap is Capability.READ_INBOX:
            await self._unit(ctx)
            entries = []
            for username, thread in world.threads.items():
                last = thread.messages[-1] if thread.messages else None
                entries.append(
                    InboxEntry(
                        peer_username=username,
                        thread_id=thread.thread_id,
                        last_message_preview=last.text[:60] if last else None,
                        last_message_outbound=(last.direction is MessageDirection.OUTBOUND) if last else None,
                    ).model_dump(mode="json")
                )
            return self._r(request, ExecutionStatus.SUCCESS, ctx, confirmed=True, data={"inbox": entries[:limit]})
        await self._unit(ctx)
        if cap is Capability.SEARCH_ACCOUNTS:
            found = world.search(str(params.get("query", "")), limit)
        elif cap is Capability.HASHTAG_POSTS:
            found = world.hashtag_posts(str(params.get("tag", "")), limit)
            for _ in found[: max(0, (ctx.pacer.remaining if ctx.pacer else 0))]:
                await self._unit(ctx)  # opening each post to read its owner costs a unit
        elif cap is Capability.LOCATION_POSTS:
            found = world.search(str(params.get("location", "")), limit)
        elif cap in (Capability.LIST_FOLLOWERS, Capability.LIST_FOLLOWING):
            found = world.followers(request.target_username or "", limit)
        elif cap is Capability.SUGGESTED_ACCOUNTS:
            found = world.suggested(request.target_username or "", limit)
        elif cap is Capability.POST_ENGAGERS:
            found = world.engagers(str(params.get("post_url", "")), limit)
        else:
            return self._r(request, ExecutionStatus.CAPABILITY_UNAVAILABLE, ctx, code="unsupported_capability")
        return self._r(
            request,
            ExecutionStatus.SUCCESS,
            ctx,
            confirmed=True,
            data={"candidates": [c.model_dump(mode="json") for c in found]},
        )


class SimulatedApiAdapter(_SimAdapterBase):
    channel = Channel.API

    def __init__(self, world: SimulatedWorld, business_discovery: bool = True) -> None:
        super().__init__(world)
        self._business_discovery = business_discovery

    def capabilities(self) -> frozenset[Capability]:
        caps = {
            Capability.SEND_DM_REPLY,
            Capability.PRIVATE_REPLY,
            Capability.REPLY_COMMENT,
            Capability.READ_THREAD,
            Capability.READ_INBOX,
        }
        if self._business_discovery:
            caps.add(Capability.INSPECT_PROFILE)
        return frozenset(caps)

    def supports(self, request: OperationRequest) -> bool:
        cap, conv = request.capability, request.conversation
        if cap not in self.capabilities():
            return False
        if cap is Capability.SEND_DM_REPLY:
            return bool(
                conv
                and conv.peer_igsid
                and conv.last_inbound_at
                and self.world.clock.now() - conv.last_inbound_at < timedelta(hours=23, minutes=30)
            )
        if cap is Capability.READ_THREAD:
            return bool(conv and conv.peer_igsid)
        if cap is Capability.PRIVATE_REPLY:
            return private_reply_open(request.params, self.world.clock.now())
        if cap is Capability.REPLY_COMMENT:
            return bool(request.params.get("comment_id"))
        return True

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        if self.world.faults.api_token_expired:
            return self._r(
                request,
                ExecutionStatus.LOGIN_REQUIRED,
                ctx,
                code="token_invalid_463",
                detail="Error validating access token: session has expired",
            )
        transient = self._transient(request, ctx)
        if transient:
            return transient
        try:
            cap = request.capability
            if cap is Capability.INSPECT_PROFILE:
                await self._unit(ctx)
                account = self.world.account(request.target_username or "")
                if account is None or not account.spec.is_business or account.spec.is_private:
                    return self._r(
                        request,
                        ExecutionStatus.TARGET_NOT_FOUND,
                        ctx,
                        code="not_found_or_not_professional",
                        detail="The user with username cannot be found (code 110/2207013)",
                    )
                # Business Discovery has no category field.
                profile = self.world.profile(account.username, Channel.API, with_category=False)
                assert profile is not None
                return self._r(
                    request,
                    ExecutionStatus.SUCCESS,
                    ctx,
                    confirmed=True,
                    data={"profile": profile.model_dump(mode="json")},
                )
            if cap is Capability.SEND_DM_REPLY:
                return await self._send(request, ctx)
            if cap is Capability.PRIVATE_REPLY:
                return await self._private_reply(request, ctx)
            if cap is Capability.READ_THREAD:
                await self._unit(ctx)
                conv = request.conversation
                username = self.world.igsid_to_username(conv.peer_igsid or "") if conv else None
                return self._r(
                    request,
                    ExecutionStatus.SUCCESS,
                    ctx,
                    confirmed=True,
                    data={"thread": self.world.snapshot(username or "").model_dump(mode="json")},
                )
            if cap is Capability.READ_INBOX:
                await self._unit(ctx)
                entries = [
                    {
                        "peer_username": u,
                        "thread_id": t.thread_id,
                        "peer_igsid": self.world.accounts[u].igsid if u in self.world.accounts else None,
                        "updated_time": _latest_iso(t.messages),
                    }
                    for u, t in self.world.threads.items()
                ]
                return self._r(request, ExecutionStatus.SUCCESS, ctx, confirmed=True, data={"inbox": entries})
            return self._r(request, ExecutionStatus.CAPABILITY_UNAVAILABLE, ctx, code="unsupported_capability")
        except BudgetExhausted as exc:
            return self._r(request, ExecutionStatus.RETRYABLE_FAILURE, ctx, code="budget_exhausted", detail=str(exc))

    async def _private_reply(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        """Mirrors the platform: one private reply per comment, within 7 days of it."""
        message = request.message
        if message is None or not message.is_intact():
            return self._r(request, ExecutionStatus.PERMANENT_FAILURE, ctx, code="invalid_request")
        comment = self.world.comments.get(str(request.params.get("comment_id", "")))
        snapshot = self.world.snapshot(comment.username) if comment else None
        if snapshot is not None and any(
            m.direction is MessageDirection.OUTBOUND and text_sha256(m.text) == message.sha256
            for m in snapshot.messages
        ):
            return self._r(request, ExecutionStatus.SUCCESS, ctx, code="already_delivered", confirmed=True)
        if comment is None or comment.private_replied:
            return self._r(
                request,
                ExecutionStatus.NOT_PERMITTED,
                ctx,
                code="comment_invalid_for_private_reply",
                detail="comment not found or already answered privately (code 100/2534025)",
            )
        if self.world.clock.now() - comment.at >= PRIVATE_REPLY_MAX_AGE:
            return self._r(request, ExecutionStatus.NOT_PERMITTED, ctx, code="outside_messaging_window")
        if not await ctx.guard():
            return self._r(request, ExecutionStatus.OWNERSHIP_CONFLICT, ctx, code="lease_lost_before_send")
        await self._unit(ctx)
        _, mid = self.world.deliver(comment.username, message.text, self.channel)
        comment.private_replied = True
        account = self.world.account(comment.username)
        return self._r(
            request,
            ExecutionStatus.SUCCESS,
            ctx,
            confirmed=True,
            data={"message_id": mid, "recipient_id": account.igsid if account else None},
        )


class SimulatedWebsiteChecker:
    def __init__(self, world: SimulatedWorld) -> None:
        self._world = world

    async def check(self, url: str) -> WebsiteCheck:
        return self._world.website_check(url)
