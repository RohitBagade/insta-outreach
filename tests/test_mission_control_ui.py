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
            assert await text(page, "#node-discover .value") == str(overview["pipeline"]["discover"]["total"])

            # The hostile note is shown literally and never executed.
            await page.wait_for_selector("#feed li[data-kind='lead.added']")
            assert HOSTILE in await text(page, "#feed li[data-kind='lead.added']")
            assert await page.locator("#feed img").count() == 0
            assert await page.evaluate("window.__pwned === undefined")

            # Approve the first message from the Approvals tab.
            await page.wait_for_selector("#tab .item button.primary")
            assert await page.locator("#tab .item").count() == len(pending)
            await page.locator("#tab .item button.primary").first.click()
            await expect(page.locator("#toast")).to_have_text("Approved")
            approved = app.control.list_actions([ActionStatus.APPROVED], limit=500)
            assert len(approved) == 1 and approved[0]["approved_by"] == "human:local"

            # Halt the browser lane, see it flagged, resume it.
            await page.locator("#lanes .lane", has_text="Browser agent").locator("button.danger").click()
            await page.wait_for_selector("#attention .item.critical")
            assert "BROWSER lane halted" in await text(page, "#attention")
            assert lane_state(app, "BROWSER") == "HALTED"
            await page.locator("#attention button", has_text="Resume").click()
            await expect(page.locator("#toast")).to_have_text("BROWSER lane resumed")
            await expect(page.locator("#attention")).not_to_contain_text("halted")
            assert lane_state(app, "BROWSER") == "ACTIVE"

            # A lead's full decision trail opens in the drawer.
            await page.locator("#tab .item a.link").first.click()
            await page.wait_for_selector("#drawer pre.trail")
            assert "HOW IT WAS FOUND" in await text(page, "#drawer pre.trail")
        finally:
            await browser.close()
            await server.stop()
    assert problems == []  # includes Content-Security-Policy violations
