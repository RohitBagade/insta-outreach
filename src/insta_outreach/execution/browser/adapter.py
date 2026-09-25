"""Tier 2 adapter: Playwright browser agent on the normal Instagram web UI.

Execution worker only. Every operation is bounded (unit budget from the
pacer), checks page state after each navigation, and returns a structured
ExecutionResult. The send path is pure deterministic code: verify the thread
belongs to the approved target, check history (idempotency / ownership),
type exactly the approved text, verify the composer, re-validate the lease,
press send, confirm the bubble. The LLM only ever helps *find* an element
for a fixed intent or classify an unexpected page.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from insta_outreach.config import AccountSettings, BrowserSettings
from insta_outreach.domain.enums import Capability, Channel, ExecutionStatus, MessageDirection
from insta_outreach.domain.models import (
    CandidateRef,
    ExecutionResult,
    InboxEntry,
    OperationRequest,
    PostObservation,
    ProfileObservation,
    ThreadSnapshot,
    text_sha256,
)
from insta_outreach.execution.base import OperationContext, result
from insta_outreach.execution.browser import extract
from insta_outreach.execution.browser.detect import PageState, PageStateDetector
from insta_outreach.execution.browser.evidence import EvidenceRecorder
from insta_outreach.execution.browser.resolver import ElementResolver, Resolved
from insta_outreach.execution.browser.session import BrowserSession, NavigationBlocked
from insta_outreach.execution.browser.ui_map import UiMap, load_ui_map
from insta_outreach.execution.browser.vision import classify_page
from insta_outreach.llm import StructuredLLM
from insta_outreach.policy.usage import BudgetExhausted
from insta_outreach.storage.db import Database
from insta_outreach.util.clock import Clock
from insta_outreach.util.text import normalize_message_text

log = logging.getLogger(__name__)

STATE_STATUS = {
    "login_required": ExecutionStatus.LOGIN_REQUIRED,
    "checkpoint": ExecutionStatus.CHECKPOINT_REQUIRED,
    "rate_limited": ExecutionStatus.RATE_LIMITED,
    "restricted": ExecutionStatus.ACCOUNT_RESTRICTED,
    "not_found": ExecutionStatus.TARGET_NOT_FOUND,
    "cannot_message": ExecutionStatus.NOT_PERMITTED,
    "request_pending": ExecutionStatus.NOT_PERMITTED,
    "human_action": ExecutionStatus.HUMAN_ACTION_REQUIRED,
    "unknown": ExecutionStatus.UI_CHANGED,
}
_LLM_STATE = {
    "checkpoint": ("checkpoint", 0.3),
    "account_restricted": ("restricted", 0.3),
    "login_required": ("login_required", 0.3),
    "rate_limited": ("rate_limited", 0.5),
    "not_found": ("not_found", 0.6),
}
_SCROLL_DIALOG_JS = """() => {
  const d = document.querySelector('[role="dialog"]');
  if (!d) return;
  const s = Array.from(d.querySelectorAll('div')).find(e => e.scrollHeight > e.clientHeight + 20);
  if (s) s.scrollTop = s.scrollHeight;
}"""
_HISTORY_HINTS = re.compile(
    r"\b(today|yesterday|seen|active \d+|\d{1,2}:\d{2}\s?(am|pm)?|[a-z]{3} \d{1,2}, \d{4})\b", re.I
)


class _Fail(Exception):
    def __init__(self, status: ExecutionStatus, code: str, detail: str = "", data: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.data = status, code, detail, data or {}


class PlaywrightBrowserAdapter:
    channel = Channel.BROWSER
    simulated = False

    def __init__(
        self,
        settings: BrowserSettings,
        account: AccountSettings,
        clock: Clock,
        llm: StructuredLLM | None,
        db: Database | None,
        *,
        session: BrowserSession | None = None,
        ui: UiMap | None = None,
    ) -> None:
        self._settings = settings
        self._account = account
        self._clock = clock
        self._llm = llm if settings.llm_fallback else None
        self.ui = ui or load_ui_map(settings.ui_map_path)
        self.session = session or BrowserSession(settings, account.id)
        self.detector = PageStateDetector(self.ui)
        self.resolver = ElementResolver(self.ui, self._llm, db, clock, llm_enabled=settings.llm_fallback)
        self._base = settings.base_url.rstrip("/")

    # -- contract ---------------------------------------------------------------------
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
        cap = request.capability
        if cap not in self.capabilities():
            return False
        if cap in (Capability.SEND_NEW_DM, Capability.SEND_DM_REPLY):
            return request.message is not None and bool(
                request.target_username or (request.conversation and request.conversation.peer_username)
            )
        return True

    async def close(self) -> None:
        await self.session.close()

    async def execute(self, request: OperationRequest, ctx: OperationContext) -> ExecutionResult:
        evidence = EvidenceRecorder(ctx.evidence_dir, self._clock, request.action_id)
        handlers = {
            Capability.INSPECT_PROFILE: self._inspect,
            Capability.SEARCH_ACCOUNTS: self._search,
            Capability.HASHTAG_POSTS: self._posts_owners,
            Capability.LOCATION_POSTS: self._posts_owners,
            Capability.LIST_FOLLOWERS: self._follow_list,
            Capability.LIST_FOLLOWING: self._follow_list,
            Capability.SUGGESTED_ACCOUNTS: self._suggested,
            Capability.POST_ENGAGERS: self._engagers,
            Capability.READ_THREAD: self._read_thread,
            Capability.READ_INBOX: self._read_inbox,
            Capability.SEND_NEW_DM: self._send,
            Capability.SEND_DM_REPLY: self._send,
        }
        try:
            page = await self.session.page()
        except PlaywrightError as exc:
            # Not transient: missing browser / broken profile needs a human.
            return result(
                request,
                self.channel,
                ExecutionStatus.HUMAN_ACTION_REQUIRED,
                ctx=ctx,
                code="browser_launch_failed",
                detail=str(exc).splitlines()[0][:400],
            )
        context = self.session.context
        tracing = bool(self._settings.trace_on_failure and context is not None and await evidence.start_trace(context))
        throttled_before = self.session.throttled_responses
        outcome: ExecutionResult
        try:
            outcome = await handlers[request.capability](request, ctx, evidence)
        except _Fail as fail:
            outcome = result(
                request,
                self.channel,
                fail.status,
                ctx=ctx,
                code=fail.code,
                detail=fail.detail,
                data=fail.data,
                page_url=page.url,
            )
        except BudgetExhausted as exc:
            outcome = result(
                request,
                self.channel,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx=ctx,
                code="budget_exhausted",
                detail=str(exc),
                page_url=page.url,
            )
        except NavigationBlocked as exc:
            outcome = result(
                request,
                self.channel,
                ExecutionStatus.PERMANENT_FAILURE,
                ctx=ctx,
                code="navigation_blocked",
                detail=str(exc),
            )
        except PlaywrightTimeout as exc:
            outcome = result(
                request,
                self.channel,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx=ctx,
                code="timeout",
                detail=str(exc).splitlines()[0][:300],
                page_url=page.url,
            )
        except PlaywrightError as exc:
            log.warning("browser error, restarting session: %s", exc)
            outcome = result(
                request,
                self.channel,
                ExecutionStatus.RETRYABLE_FAILURE,
                ctx=ctx,
                code="browser_error",
                detail=str(exc).splitlines()[0][:300],
            )
            await self.session.restart()
        if (
            not outcome.ok
            and self.session.throttled_responses > throttled_before
            and outcome.status in (ExecutionStatus.RETRYABLE_FAILURE, ExecutionStatus.UI_CHANGED)
        ):
            outcome.status, outcome.code = ExecutionStatus.RATE_LIMITED, "http_429"
        if not outcome.ok and not page.is_closed():
            await evidence.screenshot(page, f"{outcome.status.value.lower()}", outcome.detail[:200] or None)
            evidence.text(f"url={page.url}\ntrace={' | '.join(self.resolver.last_trace)}", "context")
        if tracing and context is not None:
            await evidence.stop_trace(context, keep=not outcome.ok)
        outcome.evidence = [*evidence.items, *outcome.evidence]
        outcome.page_url = outcome.page_url or (page.url if not page.is_closed() else None)
        return outcome

    # -- navigation & state ------------------------------------------------------------------
    async def _goto(self, ctx: OperationContext, url: str) -> Page:
        if ctx.pacer is not None:
            await ctx.pacer.unit()
        return await self.session.goto(url)

    async def _wait_markers(self, page: Page, expect: str, timeout_ms: int = 6000) -> None:
        markers = self.ui.page_markers.get(expect)
        if not markers:
            return
        with contextlib.suppress(PlaywrightTimeout):
            await page.wait_for_selector(", ".join(markers), state="visible", timeout=timeout_ms)

    async def _settle(self, expect: str) -> PageState:
        page = await self.session.page()
        state = PageState("unknown")
        for _ in range(4):
            await self._wait_markers(page, expect)
            state = await self.detector.detect(page, expect)
            if state.kind == "benign_dialog" and state.dialog_button:
                if await self._dismiss(page, state.dialog_button):
                    continue
                return PageState("unknown", "benign_dialog_not_dismissed", state.detail, page.url)
            if state.kind == "unknown" and self._llm is not None:
                verdict = await classify_page(self._llm, page, expect)
                if verdict is not None:
                    mapped = _LLM_STATE.get(verdict.state)
                    if mapped and verdict.confidence >= mapped[1]:
                        return PageState(mapped[0], f"llm_{verdict.state}", verdict.evidence, page.url)
                    if (
                        verdict.state == "benign_dialog"
                        and verdict.dismiss_button in self.ui.dismiss_labels
                        and verdict.confidence >= 0.6
                        and await self._dismiss(page, verdict.dismiss_button)
                    ):
                        continue
                    if verdict.state == "expected_page" and verdict.confidence >= 0.7:
                        return PageState("ok", "llm_expected_page", "markers missing; UI likely changed", page.url)
            return state
        return state

    async def _dismiss(self, page: Page, label: str) -> bool:
        if label not in self.ui.dismiss_labels and not any(b.button == label for b in self.ui.benign_dialogs):
            return False
        button = page.locator("[role='dialog'], [role='alertdialog']").get_by_role("button", name=label, exact=True)
        try:
            if await button.count() == 0:
                return False
            await button.first.click()
            await page.wait_for_timeout(600)
            return True
        except PlaywrightError:
            return False

    def _check(self, state: PageState, *, allow: tuple[str, ...] = ()) -> None:
        if state.ok or state.kind in allow:
            return
        raise _Fail(STATE_STATUS.get(state.kind, ExecutionStatus.UI_CHANGED), state.code or state.kind, state.detail)

    async def _resolve(self, page: Page, intent: str, context: str = "") -> Resolved | None:
        return await self.resolver.resolve(page, intent, context)

    def _url(self, key: str, **values: str) -> str:
        return self.ui.url(key, self._base, **values)

    # -- analysis ------------------------------------------------------------------------------
    async def _inspect(self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder) -> ExecutionResult:
        username = request.target_username or ""
        page = await self._goto(ctx, self._url("profile", username=username))
        state = await self._settle("profile")
        self._check(state, allow=("private",))
        meta = await extract.page_meta(page)
        counts = extract.parse_counts(meta.get("og_desc") or meta.get("desc"))
        full_name, _ = extract.parse_og_title(meta.get("og_title") or meta.get("title"))
        header = await extract.profile_header(page)
        if header is None and not counts:
            raise _Fail(ExecutionStatus.UI_CHANGED, "profile_header_missing", "no header and no count metadata")
        category, bio_lines = extract.split_header_lines((header or {}).get("text", ""), username, full_name)
        links: list[str] = []
        for link in (header or {}).get("links", []):
            href = link.get("href") or ""
            if href.startswith("http") and "instagram.com/" not in href.replace("l.instagram.com", ""):
                decoded = extract.decode_external_link(href)
                if decoded not in links:
                    links.append(decoded)
        buttons = " ".join((header or {}).get("buttons", [])).lower()
        # A private profile still renders its header (so the page counts as "ok"):
        # check the private-account notice explicitly.
        is_private = state.kind == "private" or await self.detector.page_text_matches(page, "private") is not None
        posts = []
        for item in await extract.post_grid(page, 12):
            href = item.get("href") or ""
            shortcode = href.rstrip("/").split("/")[-1] if href else None
            posts.append(
                PostObservation(
                    url=f"{self._base}{href}" if href.startswith("/") else href,
                    shortcode=shortcode,
                    posted_at=extract.parse_alt_date(item.get("alt")),
                    alt_text=item.get("alt"),
                    media_type="REELS" if item.get("reel") else None,
                )
            )
        open_posts = int(request.params.get("open_posts", 2))
        for post in posts[:open_posts]:
            if ctx.pacer is not None and ctx.pacer.remaining <= 1:
                break
            if post.url and self.session.host_allowed(post.url):
                await self._goto(ctx, post.url)
                if (await self._settle("post")).ok:
                    post_meta = await extract.page_meta(await self.session.page())
                    _, posted_at, caption = extract.parse_post_description(
                        post_meta.get("og_desc") or post_meta.get("desc")
                    )
                    post.caption = caption or post.caption
                    post.posted_at = posted_at or post.posted_at
        profile = ProfileObservation(
            username=username,
            full_name=full_name,
            biography="\n".join(bio_lines) or None,
            category=category,
            is_business=True
            if (category or any(w in buttons for w in ("contact", "email", "call", "directions")))
            else None,
            is_private=is_private,
            is_verified=bool((header or {}).get("verified")),
            website=links[0] if links else None,
            bio_links=links,
            followers=counts.get("followers"),
            following=counts.get("following"),
            posts_count=counts.get("posts"),
            recent_posts=posts,
            observed_via=self.channel,
            observed_at=self._clock.now(),
        )
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"profile": profile.model_dump(mode="json")},
        )

    # -- discovery -----------------------------------------------------------------------------
    async def _usernames_on_page(self, page: Page, scope: str = "body", limit: int = 400) -> set[str]:
        return set(await extract.link_usernames(page, scope, limit, exclude={self._account.username.lower()}))

    async def _search(self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder) -> ExecutionResult:
        query, limit = str(request.params.get("query", "")), int(request.params.get("limit", 20))
        page = await self._goto(ctx, self._url("home"))
        self._check(await self._settle("home"))
        nav = await self._resolve(page, "nav.search")
        if nav is None:
            raise _Fail(ExecutionStatus.UI_CHANGED, "search_nav_not_found", " | ".join(self.resolver.last_trace))
        before = await self._usernames_on_page(page)
        await nav.locator.click()
        box = await self._resolve(page, "search.input")
        if box is None:
            raise _Fail(ExecutionStatus.UI_CHANGED, "search_input_not_found", " | ".join(self.resolver.last_trace))
        await box.locator.fill(query)
        if ctx.pacer is not None:
            await ctx.pacer.unit()
        found: list[str] = []
        for _ in range(10):  # results stream in
            await page.wait_for_timeout(500)
            now_seen = await extract.link_usernames(page, "body", 400, exclude={self._account.username.lower()})
            found = [u for u in now_seen if u not in before]
            if len(found) >= limit:
                break
        self._check(await self.detector.detect(page, None))
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"candidates": [CandidateRef(username=u).model_dump(mode="json") for u in found[:limit]]},
        )

    async def _posts_owners(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        limit = int(request.params.get("limit", 9))
        if request.capability is Capability.HASHTAG_POSTS:
            url = self._url("tag", tag=str(request.params.get("tag", "")).lstrip("#"))
        else:
            url = str(request.params.get("location_url") or "")
            if not url:
                raise _Fail(
                    ExecutionStatus.PERMANENT_FAILURE,
                    "location_url_required",
                    "configure location strategy entries with the location page URL",
                )
        page = await self._goto(ctx, url)
        self._check(await self._settle("tag"))
        grid = await extract.post_grid(page, limit)
        candidates: list[CandidateRef] = []
        truncated = False
        for item in grid:
            href = item.get("href") or ""
            post_url = f"{self._base}{href}" if href.startswith("/") else href
            try:
                await self._goto(ctx, post_url)
            except BudgetExhausted:
                truncated = True
                break
            state = await self._settle("post")
            if not state.ok:
                self._check(state, allow=("not_found",))
                continue
            post_page = await self.session.page()
            meta = await extract.page_meta(post_page)
            owner, _, _ = extract.parse_post_description(meta.get("og_desc") or meta.get("desc"))
            if owner is None:
                owners = await extract.link_usernames(post_page, "article header", 1, set())
                owner = owners[0] if owners else None
            if owner and owner != self._account.username.lower() and all(c.username != owner for c in candidates):
                candidates.append(CandidateRef(username=owner, source_post_url=post_url))
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"candidates": [c.model_dump(mode="json") for c in candidates], "truncated": truncated},
        )

    async def _follow_list(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        username, limit = request.target_username or "", int(request.params.get("limit", 40))
        page = await self._goto(ctx, self._url("profile", username=username))
        state = await self._settle("profile")
        self._check(state, allow=("private",))
        if state.kind == "private":
            raise _Fail(
                ExecutionStatus.NOT_PERMITTED, "private_account", "follower lists of private accounts are hidden"
            )
        intent = (
            "profile.followers_link" if request.capability is Capability.LIST_FOLLOWERS else "profile.following_link"
        )
        link = await self._resolve(page, intent)
        if link is None:
            raise _Fail(ExecutionStatus.UI_CHANGED, f"{intent}_not_found", " | ".join(self.resolver.last_trace))
        await link.locator.click()
        self._check(await self._settle("dialog"))
        seen: list[str] = []
        exclude = {self._account.username.lower(), username}
        for _ in range(max(1, limit // 10)):
            batch = await extract.link_usernames(page, "[role='dialog']", 400, exclude)
            seen.extend(u for u in batch if u not in seen)
            if len(seen) >= limit or ctx.pacer is None or ctx.pacer.remaining <= 1:
                break
            await ctx.pacer.unit()
            await page.evaluate(_SCROLL_DIALOG_JS)
            await page.wait_for_timeout(1200)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"candidates": [CandidateRef(username=u).model_dump(mode="json") for u in seen[:limit]]},
        )

    async def _suggested(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        username, limit = request.target_username or "", int(request.params.get("limit", 15))
        page = await self._goto(ctx, self._url("profile", username=username))
        self._check(await self._settle("profile"), allow=("private",))
        button = await self._resolve(page, "profile.similar_accounts")
        if button is None:
            return result(
                request,
                self.channel,
                ExecutionStatus.SUCCESS,
                ctx=ctx,
                code="no_similar_accounts",
                data={"candidates": []},
            )
        before = await self._usernames_on_page(page)
        await button.locator.click()
        await page.wait_for_timeout(1500)
        after = await extract.link_usernames(page, "body", 400, exclude={self._account.username.lower(), username})
        found = [u for u in after if u not in before]
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"candidates": [CandidateRef(username=u).model_dump(mode="json") for u in found[:limit]]},
        )

    async def _engagers(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        post_url, limit = str(request.params.get("post_url", "")), int(request.params.get("limit", 30))
        page = await self._goto(ctx, post_url)
        self._check(await self._settle("post"))
        meta = await extract.page_meta(page)
        owner, _, _ = extract.parse_post_description(meta.get("og_desc") or meta.get("desc"))
        exclude = {self._account.username.lower(), *([owner] if owner else [])}
        found = await extract.link_usernames(page, "article, main", limit, exclude)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={
                "candidates": [
                    CandidateRef(username=u, source_post_url=post_url).model_dump(mode="json") for u in found
                ]
            },
        )

    # -- conversations -----------------------------------------------------------------------------
    async def _open_thread(
        self, request: OperationRequest, ctx: OperationContext, username: str
    ) -> tuple[Page, str | None]:
        conv = request.conversation
        page = await self.session.page()
        if conv is not None and conv.browser_thread_id:
            page = await self._goto(ctx, self._url("thread", thread_id=conv.browser_thread_id))
        else:
            page = await self._goto(ctx, self._url("profile", username=username))
            self._check(await self._settle("profile"), allow=("private",))
            button = await self._resolve(page, "profile.message_button", f"Target account: @{username}")
            if button is None:
                options = await self._resolve(page, "profile.options_button")
                if options is not None:
                    await options.locator.click()
                    button = await self._resolve(page, "menu.send_message")
            if button is None:
                blocked = await self.detector.page_text_matches(page, "cannot_message")
                if blocked is not None:
                    raise _Fail(ExecutionStatus.NOT_PERMITTED, blocked.code, blocked.detail)
                if await self._header_actions_rendered(page):
                    # The header's buttons rendered but Instagram offers no way to message
                    # this account. Repeated occurrences are treated as UI drift by the lane.
                    raise _Fail(ExecutionStatus.NOT_PERMITTED, "no_message_button", "profile offers no message option")
                raise _Fail(
                    ExecutionStatus.UI_CHANGED, "message_button_not_found", " | ".join(self.resolver.last_trace)
                )
            if ctx.pacer is not None:
                await ctx.pacer.unit()
            await button.locator.click()
            with contextlib.suppress(PlaywrightTimeout):
                await page.wait_for_url(re.compile(r"/direct/t/"), timeout=self._settings.navigation_timeout_ms)
        self._check(await self._settle("thread"))
        if not await self._thread_is_for(page, username):
            raise _Fail(
                ExecutionStatus.UI_CHANGED,
                "thread_identity_unverified",
                f"could not verify the open thread belongs to @{username}",
            )
        match = re.search(r"/direct/t/([^/?#]+)", page.url)
        return page, match.group(1) if match else None

    @staticmethod
    async def _header_actions_rendered(page: Page) -> bool:
        """True when the profile header's action buttons (Follow/Following) rendered."""
        try:
            header = page.locator("header")
            follow = header.get_by_role("button", name=re.compile(r"^(follow|following|follow back|requested)$", re.I))
            return await follow.count() > 0
        except PlaywrightError:
            return False

    @staticmethod
    async def _thread_is_for(page: Page, username: str) -> bool:
        """The open conversation must visibly belong to the approved target."""
        try:
            if await page.locator(f"a[href='/{username}/'], a[href='/{username}']").count() > 0:
                return True
            return await page.get_by_text(username, exact=True).count() > 0
        except PlaywrightError:
            return False

    async def _snapshot(self, page: Page, username: str, thread_id: str | None) -> ThreadSnapshot:
        messages = await extract.thread_messages(page)
        pending = await self.detector.page_text_matches(page, "request_pending")
        return ThreadSnapshot(
            peer_username=username,
            thread_id=thread_id,
            thread_url=page.url,
            exists=bool(messages),
            messages=messages,
            request_pending=pending is not None,
        )

    async def _read_thread(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        username = request.target_username or (request.conversation.peer_username if request.conversation else "")
        page, thread_id = await self._open_thread(request, ctx, username)
        snapshot = await self._snapshot(page, username, thread_id)
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"thread": snapshot.model_dump(mode="json"), "thread_id": thread_id},
        )

    async def _read_inbox(
        self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder
    ) -> ExecutionResult:
        limit = int(request.params.get("limit", 30))
        page = await self._goto(ctx, self._url("inbox"))
        self._check(await self._settle("inbox"))
        # Inbox rows show display names, not usernames: entries are matched to
        # conversations by thread id (recorded when we first messaged them).
        rows = await page.evaluate(
            """(limit) => Array.from(document.querySelectorAll('a[href^="/direct/t/"]')).slice(0, limit).map(a => ({
                 href: a.getAttribute('href'), text: (a.innerText || '').trim()}))""",
            limit,
        )
        entries = []
        for row in rows or []:
            match = re.search(r"/direct/t/([^/?#]+)", row.get("href") or "")
            lines = [ln for ln in (row.get("text") or "").splitlines() if ln.strip()]
            preview = lines[1] if len(lines) > 1 else None
            outbound = bool(preview and preview.lower().startswith("you:"))
            entries.append(
                InboxEntry(
                    peer_username="",
                    thread_id=match.group(1) if match else None,
                    last_message_preview=preview.split(":", 1)[1].strip() if outbound and preview else preview,
                    last_message_outbound=outbound if preview else None,
                ).model_dump(mode="json")
            )
        return result(request, self.channel, ExecutionStatus.SUCCESS, ctx=ctx, confirmed=True, data={"inbox": entries})

    # -- sending -------------------------------------------------------------------------------------
    async def _send(self, request: OperationRequest, ctx: OperationContext, ev: EvidenceRecorder) -> ExecutionResult:
        message = request.message
        username = request.target_username or (request.conversation.peer_username if request.conversation else "")
        if message is None or not message.is_intact() or not username:
            raise _Fail(ExecutionStatus.PERMANENT_FAILURE, "invalid_request", "missing or tampered approved message")
        page, thread_id = await self._open_thread(request, ctx, username)
        snapshot = await self._snapshot(page, username, thread_id)
        thread_data = {"thread": snapshot.model_dump(mode="json"), "thread_id": thread_id}
        outbound = [m for m in snapshot.messages if m.direction is MessageDirection.OUTBOUND]
        if any(text_sha256(m.text) == message.sha256 for m in outbound):
            return result(
                request,
                self.channel,
                ExecutionStatus.SUCCESS,
                ctx=ctx,
                confirmed=True,
                code="already_sent_idempotent",
                data=thread_data | {"thread_url": page.url},
            )
        if message.kind == "initial" and snapshot.messages:
            raise _Fail(
                ExecutionStatus.ALREADY_CONTACTED, "thread_has_history", "existing conversation found", thread_data
            )
        unknown = [m for m in outbound if text_sha256(m.text) not in message.known_outbound_hashes]
        if unknown:
            raise _Fail(
                ExecutionStatus.OWNERSHIP_CONFLICT,
                "human_message_in_thread",
                "thread contains outbound messages automation did not send",
                thread_data,
            )
        if not snapshot.messages:
            await self._assert_really_empty(page, message.kind)
        if snapshot.request_pending and message.kind != "initial":
            raise _Fail(
                ExecutionStatus.NOT_PERMITTED,
                "message_request_pending",
                "Instagram allows more messages only after the request is accepted",
                thread_data,
            )
        blocked = await self.detector.page_text_matches(page, "cannot_message")
        if blocked is not None:
            raise _Fail(ExecutionStatus.NOT_PERMITTED, blocked.code, blocked.detail, thread_data)

        composer = await self._resolve(page, "thread.composer", f"Conversation with @{username}")
        if composer is None:
            raise _Fail(ExecutionStatus.UI_CHANGED, "composer_not_found", " | ".join(self.resolver.last_trace))
        if not await self._type_exactly(page, composer.locator, message.text):
            await self._clear(page, composer.locator)
            raise _Fail(
                ExecutionStatus.UI_CHANGED,
                "composer_text_mismatch",
                "composer content did not match the approved text; nothing sent",
            )
        if not await ctx.guard():
            await self._clear(page, composer.locator)
            raise _Fail(
                ExecutionStatus.OWNERSHIP_CONFLICT,
                "lease_lost_before_send",
                "conversation ownership changed; typed text cleared, nothing sent",
            )

        # ---- irreversible step: from here the outcome is unknown until confirmed ----
        send = await self._resolve(page, "thread.send")
        if ctx.pacer is not None:
            await ctx.pacer.unit()
        if send is not None:
            await send.locator.click()
        else:
            await composer.locator.press("Enter")
        confirmed = await self._confirm_sent(page, message.sha256)
        after = await self.detector.detect(page, "thread")
        if after.kind in ("rate_limited", "restricted", "checkpoint", "login_required"):
            raise _Fail(STATE_STATUS[after.kind], after.code, f"after send: {after.detail}", thread_data)
        if not confirmed:
            raise _Fail(
                ExecutionStatus.RETRYABLE_FAILURE,
                "send_unconfirmed",
                "message bubble not observed; retry will re-check the thread before resending",
                thread_data,
            )
        if self._settings.screenshot_on_success:
            await ev.screenshot(page, "sent", f"message sent to @{username}")
        return result(
            request,
            self.channel,
            ExecutionStatus.SUCCESS,
            ctx=ctx,
            confirmed=True,
            data={"thread_id": thread_id, "thread_url": page.url},
        )

    async def _assert_really_empty(self, page: Page, kind: str) -> None:
        """No messages parsed. Make sure that is not a parsing failure hiding history."""
        if kind != "initial":
            raise _Fail(
                ExecutionStatus.UI_CHANGED,
                "thread_unreadable",
                "expected earlier messages in this thread but could not read any",
            )
        try:
            text = await page.evaluate("() => (document.querySelector('main') || document.body).innerText")
        except PlaywrightError:
            text = ""
        if _HISTORY_HINTS.search(text or ""):
            raise _Fail(
                ExecutionStatus.UI_CHANGED,
                "thread_history_unparseable",
                "thread shows signs of earlier messages that could not be parsed; not sending",
            )

    async def _type_exactly(self, page: Page, composer: Locator, text: str) -> bool:
        for _ in range(2):
            await composer.click()
            await self._clear(page, composer)
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if line:
                    await composer.press_sequentially(line, delay=self._settings.typing_delay_ms)
                if index < len(lines) - 1:
                    await page.keyboard.press("Shift+Enter")
            if normalize_message_text(await self._composer_text(composer)) == normalize_message_text(text):
                return True
        return False

    @staticmethod
    async def _composer_text(composer: Locator) -> str:
        tag = await composer.evaluate("el => el.tagName.toLowerCase()")
        if tag in ("textarea", "input"):
            return await composer.input_value()
        return await composer.inner_text()

    @staticmethod
    async def _clear(page: Page, composer: Locator) -> None:
        try:
            await composer.click()
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Backspace")
        except PlaywrightError:
            pass

    async def _confirm_sent(self, page: Page, sha256: str, timeout_ms: int = 10_000) -> bool:
        waited = 0
        while waited <= timeout_ms:
            for message in await extract.thread_messages(page):
                if message.direction is MessageDirection.OUTBOUND and text_sha256(message.text) == sha256:
                    return True
            await page.wait_for_timeout(500)
            waited += 500
        return False
