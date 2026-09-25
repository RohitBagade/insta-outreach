"""The real Playwright browser agent, driving Chromium against the mock site."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from insta_outreach.config import AccountSettings, BrowserSettings
from insta_outreach.devtools.mock_instagram import STATE
from insta_outreach.domain.enums import Capability, ExecutionStatus
from insta_outreach.domain.models import ApprovedMessage, ConversationRef, OperationRequest, text_sha256
from insta_outreach.execution.base import OperationContext
from insta_outreach.execution.browser.adapter import PlaywrightBrowserAdapter
from insta_outreach.execution.browser.resolver import ElementChoice
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import LearnedLocator
from insta_outreach.util.clock import SystemClock
from tests.conftest import ScriptedLLM, candidates_from

pytestmark = pytest.mark.browser
BREW = "sim.the.brew.room"


def browser_settings(base: str, tmp_path: Path, **kw: Any) -> BrowserSettings:
    return BrowserSettings(
        enabled=True,
        base_url=base,
        allowed_hosts=["127.0.0.1"],
        profiles_dir=tmp_path / "prof",
        evidence_dir=tmp_path / "evidence",
        headless=True,
        typing_delay_ms=2,
        navigation_timeout_ms=15_000,
        action_timeout_ms=4_000,
        trace_on_failure=False,
        llm_fallback=kw.pop("llm_fallback", False),
        **kw,
    )


@pytest.fixture
async def agent(mock_site: str, tmp_path: Path) -> AsyncIterator[PlaywrightBrowserAdapter]:
    STATE.reset()
    adapter = PlaywrightBrowserAdapter(
        browser_settings(mock_site, tmp_path), AccountSettings(), SystemClock(), None, None
    )
    yield adapter
    await adapter.close()


def ctx(tmp_path: Path, guard: Any = None) -> OperationContext:
    context = OperationContext(clock=SystemClock(), evidence_dir=tmp_path / "evidence")
    if guard is not None:
        context.guard = guard
    return context


def req(cap: Capability, target: str | None = None, **kw: Any) -> OperationRequest:
    return OperationRequest(
        action_id="act_test", account_id="lemmedeliver", capability=cap, target_username=target, **kw
    )


def message(text: str, kind: str = "initial", known: list[str] | None = None) -> ApprovedMessage:
    return ApprovedMessage(
        action_id="act_test",
        text=text,
        sha256=text_sha256(text),
        kind=kind,  # type: ignore[arg-type]
        known_outbound_hashes=known or [],
    )


# ------------------------------------------------------------------------ read-only operations
async def test_inspect_profile_extracts_observable_facts(agent, tmp_path) -> None:
    result = await agent.execute(req(Capability.INSPECT_PROFILE, "sim.smileline.dental"), ctx(tmp_path))
    assert result.ok, result.detail
    profile = result.data["profile"]
    assert profile["full_name"] == "Smileline Dental Studio" and profile["category"] == "Dentist"
    assert profile["biography"] == "Family & cosmetic dentistry in Andheri West\nDM to book your appointment"
    assert profile["website"] == "https://smileline.example/"  # unwrapped from l.instagram.com
    assert (profile["followers"], profile["following"], profile["posts_count"]) == (2400, 180, 1)
    assert profile["recent_posts"][0]["caption"] == "Smile makeover #mumbaidentist"


async def test_inspect_private_and_missing_profiles(agent, tmp_path) -> None:
    private = await agent.execute(req(Capability.INSPECT_PROFILE, "sim.private.nails"), ctx(tmp_path))
    assert private.ok and private.data["profile"]["is_private"] is True
    missing = await agent.execute(req(Capability.INSPECT_PROFILE, "sim.does.not.exist"), ctx(tmp_path))
    assert missing.status is ExecutionStatus.TARGET_NOT_FOUND


async def test_search_returns_results_not_feed_noise(agent, tmp_path) -> None:
    result = await agent.execute(
        req(Capability.SEARCH_ACCOUNTS, params={"query": "cafe thane", "limit": 5}), ctx(tmp_path)
    )
    names = [c["username"] for c in result.data["candidates"]]
    assert result.ok and BREW in names and "sim.feed.noise" not in names and "lemmedeliver" not in names


async def test_hashtag_posts_resolve_owners(agent, tmp_path) -> None:
    result = await agent.execute(req(Capability.HASHTAG_POSTS, params={"tag": "thanecafe", "limit": 8}), ctx(tmp_path))
    owners = {c["username"] for c in result.data["candidates"]}
    assert result.ok and BREW in owners and "sim.chai.and.chapters" in owners


async def test_followers_and_similar_accounts(agent, tmp_path) -> None:
    followers = await agent.execute(req(Capability.LIST_FOLLOWERS, BREW, params={"limit": 10}), ctx(tmp_path))
    assert {c["username"] for c in followers.data["candidates"]} >= {"sim.priya.travels", "sim.tiny.bakes.dadar"}
    similar = await agent.execute(req(Capability.SUGGESTED_ACCOUNTS, BREW, params={"limit": 5}), ctx(tmp_path))
    assert {c["username"] for c in similar.data["candidates"]} == {"sim.chai.and.chapters", "sim.cake.canvas.powai"}


# ------------------------------------------------------------------------------------ sending
async def test_send_types_exactly_confirms_and_is_idempotent(agent, tmp_path) -> None:
    text = "Hi The Brew Room team! I'm Rohit from LemmeDeliver.\nWould you like to see a quick concept?"
    first = await agent.execute(req(Capability.SEND_NEW_DM, BREW, message=message(text)), ctx(tmp_path))
    assert first.ok and first.confirmed and first.data["thread_id"]
    assert STATE.sent == [{"to": BREW, "text": text}]
    assert any(e.kind == "screenshot" and Path(e.path or "").exists() for e in first.evidence)
    replay = await agent.execute(req(Capability.SEND_NEW_DM, BREW, message=message(text)), ctx(tmp_path))
    assert replay.ok and replay.code == "already_sent_idempotent" and len(STATE.sent) == 1


async def test_existing_conversation_is_never_cold_messaged(agent, tmp_path) -> None:
    result = await agent.execute(
        req(Capability.SEND_NEW_DM, "sim.chai.and.chapters", message=message("Hi! Quick idea for your site?")),
        ctx(tmp_path),
    )
    assert result.status is ExecutionStatus.ALREADY_CONTACTED and STATE.sent == []
    history = result.data["thread"]["messages"]
    assert history[0]["direction"] == "OUTBOUND"  # read from bubble geometry, not markup hints


async def test_followup_blocked_when_human_wrote_in_thread(agent, tmp_path) -> None:
    ours = "Hi! I'm Rohit from LemmeDeliver. Would you like a quick concept?"
    await agent.execute(req(Capability.SEND_NEW_DM, BREW, message=message(ours)), ctx(tmp_path))
    thread = STATE.thread_for(BREW)
    assert thread is not None
    thread.messages.append(("out", "Rohit again, calling you tomorrow!"))  # typed by the human
    follow = message("Hi again! Just checking in on my note?", kind="followup", known=[text_sha256(ours)])
    result = await agent.execute(req(Capability.SEND_DM_REPLY, BREW, message=follow), ctx(tmp_path))
    assert result.status is ExecutionStatus.OWNERSHIP_CONFLICT and len(STATE.sent) == 1


async def test_lost_lease_before_send_clears_text_and_sends_nothing(agent, tmp_path) -> None:
    async def lost() -> bool:
        return False

    result = await agent.execute(
        req(Capability.SEND_NEW_DM, BREW, message=message("Hi from LemmeDeliver?")), ctx(tmp_path, guard=lost)
    )
    assert result.status is ExecutionStatus.OWNERSHIP_CONFLICT and result.code == "lease_lost_before_send"
    assert STATE.sent == []
    page = await agent.session.page()
    assert (await page.locator("#composer").inner_text()).strip() == ""


async def test_unconfirmed_send_is_reported_as_unknown_outcome(agent, tmp_path) -> None:
    STATE.faults.add("send_fails")
    result = await agent.execute(
        req(Capability.SEND_NEW_DM, BREW, message=message("Hi from LemmeDeliver?")), ctx(tmp_path)
    )
    assert result.status is ExecutionStatus.RETRYABLE_FAILURE and result.code == "send_unconfirmed"


async def test_recipient_that_cannot_be_messaged(agent, tmp_path) -> None:
    result = await agent.execute(
        req(Capability.SEND_NEW_DM, "sim.cake.canvas.powai", message=message("Hi from LemmeDeliver?")), ctx(tmp_path)
    )
    assert result.status is ExecutionStatus.NOT_PERMITTED and STATE.sent == []


async def test_tampered_message_is_refused(agent, tmp_path) -> None:
    bad = message("Approved text?").model_copy(update={"text": "Something else entirely?"})
    result = await agent.execute(req(Capability.SEND_NEW_DM, BREW, message=bad), ctx(tmp_path))
    assert result.status is ExecutionStatus.PERMANENT_FAILURE and STATE.sent == []


# ------------------------------------------------------------------------------------ barriers
@pytest.mark.parametrize(
    ("fault", "status", "url_part"),
    [
        ("login", ExecutionStatus.LOGIN_REQUIRED, "/accounts/login/"),
        ("checkpoint", ExecutionStatus.CHECKPOINT_REQUIRED, "/challenge/"),
        ("rate_limit_dialog", ExecutionStatus.RATE_LIMITED, ""),
        ("restricted_dialog", ExecutionStatus.ACCOUNT_RESTRICTED, ""),
    ],
)
async def test_barriers_stop_with_structured_state_and_evidence(agent, tmp_path, fault, status, url_part) -> None:
    STATE.faults.add(fault)
    result = await agent.execute(
        req(Capability.SEND_NEW_DM, BREW, message=message("Hi from LemmeDeliver?")), ctx(tmp_path)
    )
    assert result.status is status
    assert url_part in (result.page_url or "")
    assert any(e.kind == "screenshot" for e in result.evidence)
    assert STATE.sent == []  # nothing is ever typed past a barrier


async def test_benign_dialog_is_dismissed_with_allowlisted_button(agent, tmp_path) -> None:
    STATE.faults.add("notifications_dialog")
    result = await agent.execute(req(Capability.INSPECT_PROFILE, BREW), ctx(tmp_path))
    assert result.ok and STATE.notifications_dismissed


async def test_navigation_outside_allowlist_is_blocked(agent, tmp_path) -> None:
    result = await agent.execute(
        req(Capability.POST_ENGAGERS, params={"post_url": "https://example.com/p/x/"}), ctx(tmp_path)
    )
    assert result.status is ExecutionStatus.PERMANENT_FAILURE and result.code == "navigation_blocked"


# ---------------------------------------------------------------------------------- UI drift
async def test_ui_drift_without_llm_fails_safe(agent, tmp_path) -> None:
    STATE.faults.add("ui_changed")
    search = await agent.execute(req(Capability.SEARCH_ACCOUNTS, params={"query": "cafe", "limit": 3}), ctx(tmp_path))
    assert search.status is ExecutionStatus.UI_CHANGED and search.code == "search_nav_not_found"
    assert any(e.kind == "screenshot" for e in search.evidence)
    # A renamed Message button looks like "no message option" on one profile; the lane
    # halts when it repeats (see test_policy.test_missing_message_option_repeated_halts_lane).
    send = await agent.execute(
        req(Capability.SEND_NEW_DM, BREW, message=message("Hi from LemmeDeliver?")), ctx(tmp_path)
    )
    assert send.status is ExecutionStatus.NOT_PERMITTED and send.code == "no_message_button"
    assert STATE.sent == []


def healing_llm(labels: dict[str, str], wrong_first: str | None = None) -> ScriptedLLM:
    """Picks the candidate whose visible label matches the intent's new name."""
    attempts: dict[str, int] = {}

    def handler(purpose: str, content: list[dict[str, Any]], model: type) -> Any:
        if not purpose.startswith("resolve:"):
            return None
        intent = purpose.split(":", 1)[1]
        attempts[intent] = attempts.get(intent, 0) + 1
        wanted = wrong_first if (wrong_first and attempts[intent] == 1) else labels.get(intent)
        for cand in candidates_from(content):
            if wanted and wanted in (cand["name"], cand["text"]):
                return ElementChoice(index=cand["i"], confidence=0.93, reason=f"label '{wanted}'")
        return ElementChoice(index=None, confidence=0.0, reason="no match")

    return ScriptedLLM(handler)


async def test_llm_heals_changed_ui_and_learns_locators(mock_site, tmp_path) -> None:
    STATE.reset()
    STATE.faults.add("ui_changed")
    db = Database(f"sqlite:///{tmp_path / 'learned.db'}")
    db.create_all()
    llm = healing_llm(
        {"profile.message_button": "Chat", "thread.composer": "Write something...", "thread.send": "Submit"}
    )
    agent = PlaywrightBrowserAdapter(
        browser_settings(mock_site, tmp_path, llm_fallback=True), AccountSettings(), SystemClock(), llm, db
    )
    try:
        text = "Hi The Brew Room team! I'm Rohit from LemmeDeliver. Would you like a quick concept?"
        result = await agent.execute(req(Capability.SEND_NEW_DM, BREW, message=message(text)), ctx(tmp_path))
        assert result.ok, (result.status, result.code, result.detail)
        assert STATE.sent == [{"to": BREW, "text": text}]
        with db.session() as s:
            learned = {row.intent for row in s.query(LearnedLocator)}
        # The composer is still found by its role-agnostic contenteditable locator;
        # only the renamed buttons needed the model, and were learned.
        assert learned == {"profile.message_button", "thread.send"}
        calls = len(llm.calls)
        second = "Following up from LemmeDeliver, would a concept help?"
        again = await agent.execute(
            req(
                Capability.SEND_DM_REPLY,
                BREW,
                message=message(second, kind="followup", known=[text_sha256(text)]),
                conversation=ConversationRef(
                    conversation_id=1, peer_username=BREW, browser_thread_id=result.data["thread_id"]
                ),
            ),
            ctx(tmp_path),
        )
        assert again.ok and len(llm.calls) == calls  # learned locators: no new LLM calls
    finally:
        await agent.close()


async def test_llm_can_never_click_a_denylisted_control(mock_site, tmp_path) -> None:
    STATE.reset()
    STATE.faults.add("ui_changed")
    llm = healing_llm({"profile.message_button": "Chat"}, wrong_first="Follow")
    agent = PlaywrightBrowserAdapter(
        browser_settings(mock_site, tmp_path, llm_fallback=True), AccountSettings(), SystemClock(), llm, None
    )
    try:
        page = await agent.session.goto(f"{mock_site}/{BREW}/")
        # The model points at "Follow" for the message intent: validation must refuse it.
        refused = await agent.resolver.resolve(page, "profile.message_button")
        assert refused is None
        assert any("deny-listed" in line for line in agent.resolver.last_trace), agent.resolver.last_trace
        # Asked again, it identifies the renamed button, which passes validation.
        healed = await agent.resolver.resolve(page, "profile.message_button")
        assert healed is not None and healed.provenance == "llm"
        assert (await healed.locator.inner_text()).strip() == "Chat"
        assert STATE.sent == []
    finally:
        await agent.close()
