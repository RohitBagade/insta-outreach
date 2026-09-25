"""Persistent browser session per Instagram account.

A persistent Chromium profile (``browser.profiles_dir/<account>``) keeps the
login across restarts — Rohit logs in once, by hand, via ``insta-outreach
browser login``; the agent never types credentials or answers security
prompts. Navigation is restricted to an allowlist of hosts, popups are
closed, and HTTP 429 responses are counted as a rate-limit signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Playwright, Response, async_playwright

from insta_outreach.config import BrowserSettings

log = logging.getLogger(__name__)


class NavigationBlocked(Exception):
    pass


class BrowserSession:
    def __init__(self, settings: BrowserSettings, account_id: str, headless: bool | None = None) -> None:
        self._settings = settings
        self._account_id = account_id
        self._headless = settings.headless if headless is None else headless
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self.throttled_responses = 0
        self._background: set[asyncio.Task[None]] = set()
        allowed = {h.lower() for h in settings.allowed_hosts}
        base_host = urlparse(settings.base_url).hostname
        if base_host:
            allowed.add(base_host.lower())
        self._allowed_hosts = allowed

    @property
    def profile_dir(self) -> Path:
        return Path(self._settings.profiles_dir) / self._account_id

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    def host_allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in self._allowed_hosts

    async def page(self) -> Page:
        async with self._lock:
            if self._page is None or self._page.is_closed() or self._context is None:
                await self._start()
            assert self._page is not None
            return self._page

    async def _start(self) -> None:
        await self._shutdown()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, object] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self._headless,
            "locale": self._settings.locale,
            "timezone_id": self._settings.timezone_id,
            "viewport": {"width": self._settings.viewport_width, "height": self._settings.viewport_height},
        }
        if self._settings.channel:
            launch_kwargs["channel"] = self._settings.channel
        if self._settings.executable_path:
            launch_kwargs["executable_path"] = self._settings.executable_path
        self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)  # type: ignore[arg-type]
        self._context.set_default_timeout(self._settings.action_timeout_ms)
        self._context.set_default_navigation_timeout(self._settings.navigation_timeout_ms)
        self._context.on("page", self._on_new_page)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        for extra in self._context.pages[1:]:
            await extra.close()
        self._page.on("response", self._on_response)
        log.info(
            "browser session started for %s (profile %s, headless=%s)",
            self._account_id,
            self.profile_dir,
            self._headless,
        )

    def _on_new_page(self, page: Page) -> None:
        if self._page is not None and page != self._page:
            # Popups / target=_blank tabs are never part of an operation.
            task = asyncio.ensure_future(page.close())
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def _on_response(self, response: Response) -> None:
        if response.status == 429 and self.host_allowed(response.url):
            self.throttled_responses += 1

    async def goto(self, url: str) -> Page:
        if not self.host_allowed(url):
            raise NavigationBlocked(f"navigation to {url} is outside the allowlist")
        page = await self.page()
        await page.goto(url, wait_until="domcontentloaded")
        # SPA pages may never reach 'load' quickly; page markers decide readiness.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=self._settings.navigation_timeout_ms // 2)
        return page

    async def ensure_on_allowed_host(self) -> bool:
        page = await self.page()
        return self.host_allowed(page.url) or page.url in ("about:blank", "")

    async def _shutdown(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._context, self._page, self._pw = None, None, None

    async def restart(self) -> None:
        async with self._lock:
            await self._start()

    async def close(self) -> None:
        async with self._lock:
            await self._shutdown()
