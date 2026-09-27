"""Mission Control in a real Chromium: renders, stays in sync, and its controls work."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from playwright.async_api import Page, async_playwright, expect

from insta_outreach.api.server import BackgroundServer
from insta_outreach.domain.enums import ActionStatus, OperatingMode
from tests.conftest import run_ticks

pytestmark = pytest.mark.browser
HOSTILE = '<img src=x onerror="window.__pwned=1">'


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def lane_state(app: Any, channel: str) -> str:
    return str(next(lane["state"] for lane in app.control.status()["lanes"] if lane["channel"] == channel))


async def text(page: Page, selector: str) -> str:
    return (await page.locator(selector).first.inner_text()).strip()


async def go(page: Page, name: str) -> None:
    await page.locator(f"#navLinks a[data-page='{name}']").click()
    await expect(page.locator(f"#page-{name}")).to_be_visible()


async def test_dashboard_renders_and_drives_the_control_plane(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 40)
    # External text must render as text: a note is echoed into the audit feed.
    app.control.add_lead("sim.new.prospect", by="test", note=HOSTILE)
    pending = app.control.list_actions([ActionStatus.PENDING_APPROVAL], limit=500)
    assert pending

    server = await BackgroundServer.start(app, port=free_port())
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("dialog", lambda d: d.accept())  # prompts for audit notes / confirmations
        try:
            await page.goto(server.url)
            await expect(page.locator("#conn")).to_contain_text("live")  # no eval: the page's CSP forbids it
            assert "SIMULATION" in await text(page, "#env")
            assert await text(page, "#modes button.on") == "Approval"
            overview = app.monitor.overview()
            await expect(page.locator("#kpi-found .value")).to_have_text(str(overview["funnel"]["found"]))
            assert await text(page, "#nowText") == "Sending only the messages you approve."
            await expect(page.locator("#funnel .fstep")).to_have_count(6)
            await expect(page.locator("#categories svg.chart")).to_be_visible()

            # Activity: plain sentences by default, the agent's routine chores hidden.
            await go(page, "activity")
            chore = page.locator("#feed li[data-important='0']").first
            await expect(chore).to_be_hidden()
            await page.locator("#techToggle").check()
            await expect(chore).to_be_visible()
            await page.locator("#techToggle").uncheck()

            # The hostile note is shown literally and never executed.
            await page.wait_for_selector("#feed li[data-kind='lead.added']")
            assert HOSTILE in await text(page, "#feed li[data-kind='lead.added']")
            assert await page.locator("img").count() == 0
            assert await page.evaluate("window.__pwned === undefined")

            # Approve the first message on the Approvals page.
            await go(page, "approvals")
            await page.wait_for_selector("#approvals .approval button.primary")
            assert await page.locator("#approvals .approval").count() == len(pending)
            await expect(page.locator("#b-approvals")).to_have_text(str(len(pending)))
            await page.locator("#approvals .approval button.primary").first.click()
            await expect(page.locator("#toast")).to_have_text("Approved")
            approved = app.control.list_actions([ActionStatus.APPROVED], limit=500)
            assert len(approved) == 1 and approved[0]["approved_by"] == "human:local"
            await expect(page.locator("#feed li[data-kind='action.approved']")).to_contain_text(
                f"You approved the message to @{approved[0]['target_username']}"
            )

            # Stop the browser from Account health, see it flagged, resume it.
            await go(page, "dashboard")
            await page.locator("#lanes .lane", has_text="Browser").locator("button.danger").click()
            await page.wait_for_selector("#attention .item.critical")
            assert "the browser is stopped" in await text(page, "#attention")
            await expect(page.locator("#nowMark")).to_have_text("Needs you")
            assert lane_state(app, "BROWSER") == "HALTED"
            await page.locator("#attention button", has_text="Resume").click()
            await expect(page.locator("#toast")).to_have_text("Browser resumed")
            await expect(page.locator("#attention")).not_to_contain_text("stopped")
            assert lane_state(app, "BROWSER") == "ACTIVE"

            # A business's details: its sales stage and the full decision trail.
            await go(page, "leads")
            await page.locator("#leadsTable tbody tr").first.click()
            await page.locator("#drawer select[aria-label='Sales stage']").select_option("MEETING")
            await page.locator("#drawer button", has_text="Save").click()
            await expect(page.locator("#toast")).to_have_text("Stage saved")
            handle = (await text(page, "#drawerTitle")).lstrip("@")
            assert app.control.lead_detail(handle)["stage"] == "MEETING"
            await page.locator("#drawer details.trail-box summary").click()
            await expect(page.locator("#drawer pre.trail")).to_contain_text("HOW IT WAS FOUND")
            await page.locator("#drawerClose").click()

            # Limits are changed on the Settings page, not in a terminal.
            await go(page, "settings")
            field = page.locator("#set-limits label", has_text="First messages per day").locator("input")
            await field.fill("7")
            await page.locator("#set-limits button", has_text="Save limits").click()
            await expect(page.locator("#toast")).to_contain_text("Limits saved")
            assert app.runtime.limits().outreach_per_day == 7
            # This app was built in code (like the demo): its settings file can't be changed.
            await expect(page.locator("#set-where button", has_text="real Instagram")).to_be_disabled()

            # The other pages draw without errors.
            await go(page, "analytics")
            await expect(page.locator("#daily svg.chart")).to_have_count(3)
            await go(page, "campaigns")
            await expect(page.locator("#campaigns .camp")).to_have_count(len(app.settings.campaigns))
            await go(page, "chats")
            await expect(page.locator("#chatList .chat-item").first).to_be_visible()
        finally:
            await browser.close()
            await server.stop()
    assert problems == []  # includes Content-Security-Policy violations


async def test_phone_layout_has_a_menu_and_no_sideways_scrolling(make_app, clock) -> None:
    app = make_app()
    app.runtime.set_mode(OperatingMode.APPROVAL, "test")
    await run_ticks(app, clock, 20)
    server = await BackgroundServer.start(app, port=free_port())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.goto(server.url)
            await expect(page.locator("#conn")).to_contain_text("live")
            await expect(page.locator("#nav")).not_to_be_in_viewport()
            for name in ("dashboard", "approvals", "leads", "settings"):
                await page.locator("#menuBtn").click()
                await expect(page.locator("#nav")).to_be_in_viewport()
                await page.locator(f"#navLinks a[data-page='{name}']").click()
                await expect(page.locator(f"#page-{name}")).to_be_visible()
                await expect(page.locator("#nav")).not_to_be_in_viewport()
                await page.wait_for_timeout(300)
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), name
        finally:
            await browser.close()
            await server.stop()
