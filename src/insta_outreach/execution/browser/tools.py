"""Operator tools for the browser lane: manual login and read-only probes."""

from __future__ import annotations

import asyncio
from typing import Any

from insta_outreach.config import Settings
from insta_outreach.domain.enums import Capability
from insta_outreach.domain.models import OperationRequest
from insta_outreach.execution.base import OperationContext
from insta_outreach.execution.browser.adapter import PlaywrightBrowserAdapter
from insta_outreach.execution.browser.detect import PageStateDetector
from insta_outreach.execution.browser.session import BrowserSession
from insta_outreach.execution.browser.ui_map import load_ui_map
from insta_outreach.llm import StructuredLLM
from insta_outreach.storage.db import Database
from insta_outreach.util.clock import Clock


async def interactive_login(settings: Settings, timeout_seconds: int = 900) -> tuple[bool, str]:
    """Open a visible browser on the persistent profile and wait for Rohit to
    log in (including any 2FA / checkpoint) himself. Nothing is typed for him."""
    ui = load_ui_map(settings.browser.ui_map_path)
    session = BrowserSession(settings.browser, settings.account.id, headless=False)
    detector = PageStateDetector(ui)
    try:
        page = await session.goto(ui.url("login", settings.browser.base_url))
        print(
            f"A browser window is open at {page.url}.\n"
            "Log in to @%s yourself (complete any security checks there). "
            "This command finishes once the home feed loads." % settings.account.username
        )
        waited = 0
        while waited < timeout_seconds:
            await asyncio.sleep(3)
            waited += 3
            state = await detector.detect(page, "home")
            if state.ok and "/accounts/" not in page.url and "/challenge/" not in page.url:
                return True, f"logged in; session saved in {session.profile_dir}"
        return False, "timed out waiting for login; run the command again when ready"
    finally:
        await session.close()


async def probe(
    settings: Settings,
    clock: Clock,
    llm: StructuredLLM | None,
    db: Database | None,
    target: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Read-only diagnostics: session state, profile extraction, intent resolution.

    Performs no writes (never opens a message thread or types anything).
    """
    adapter = PlaywrightBrowserAdapter(settings.browser, settings.account, clock, llm, db)
    report: dict[str, Any] = {"base_url": settings.browser.base_url}
    ctx = OperationContext(clock=clock, evidence_dir=settings.browser.evidence_dir, started_at=clock.now())
    try:
        page = await adapter.session.goto(adapter.ui.url("home", settings.browser.base_url))
        state = await adapter._settle("home")
        report["session"] = {"state": state.kind, "code": state.code, "url": page.url}
        if not state.ok:
            return report
        own = await adapter.execute(
            OperationRequest(
                action_id="probe-own",
                account_id=settings.account.id,
                capability=Capability.INSPECT_PROFILE,
                target_username=settings.account.username,
                params={"open_posts": 0},
            ),
            ctx,
        )
        report["own_profile"] = {"status": own.status.value, "code": own.code, "profile": own.data.get("profile")}
        if target:
            other = await adapter.execute(
                OperationRequest(
                    action_id="probe-target",
                    account_id=settings.account.id,
                    capability=Capability.INSPECT_PROFILE,
                    target_username=target,
                    params={"open_posts": 1},
                ),
                ctx,
            )
            report["target_profile"] = {
                "status": other.status.value,
                "code": other.code,
                "profile": other.data.get("profile"),
            }
            page = await adapter.session.goto(adapter.ui.url("profile", settings.browser.base_url, username=target))
            await adapter._settle("profile")
            intents = {}
            for intent in ("profile.message_button", "profile.followers_link", "profile.similar_accounts"):
                resolved = await adapter.resolver.resolve(page, intent)
                intents[intent] = {
                    "resolved": resolved is not None,
                    "via": resolved.provenance if resolved else None,
                    "trace": adapter.resolver.last_trace,
                }
            report["intents"] = intents
        if query:
            search = await adapter.execute(
                OperationRequest(
                    action_id="probe-search",
                    account_id=settings.account.id,
                    capability=Capability.SEARCH_ACCOUNTS,
                    params={"query": query, "limit": 5},
                ),
                ctx,
            )
            report["search"] = {
                "status": search.status.value,
                "code": search.code,
                "candidates": search.data.get("candidates"),
            }
        return report
    finally:
        await adapter.close()
