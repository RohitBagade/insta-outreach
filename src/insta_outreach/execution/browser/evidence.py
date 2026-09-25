"""Evidence capture: screenshots, page text excerpts and Playwright traces."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from insta_outreach.domain.models import Evidence
from insta_outreach.util.clock import Clock

log = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class EvidenceRecorder:
    def __init__(self, base_dir: Path, clock: Clock, action_id: str) -> None:
        now = clock.now()
        self._dir = Path(base_dir) / now.strftime("%Y-%m-%d")
        self._clock = clock
        self._action_id = _SAFE.sub("_", action_id)
        self._step = 0
        self.items: list[Evidence] = []

    def _path(self, label: str, suffix: str) -> Path:
        self._step += 1
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / f"{self._action_id}-{self._step:02d}-{_SAFE.sub('_', label)[:40]}{suffix}"

    async def screenshot(self, page: Page, label: str, note: str | None = None) -> Evidence | None:
        path = self._path(label, ".png")
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception as exc:
            log.warning("screenshot failed: %s", exc)
            return None
        item = Evidence(kind="screenshot", path=str(path), note=note or label, captured_at=self._clock.now())
        self.items.append(item)
        return item

    def text(self, content: str, note: str) -> Evidence:
        item = Evidence(kind="text", content=content[:2000], note=note, captured_at=self._clock.now())
        self.items.append(item)
        return item

    async def start_trace(self, context: BrowserContext) -> bool:
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            return True
        except Exception as exc:  # tracing already running etc.
            log.debug("tracing not started: %s", exc)
            return False

    async def stop_trace(self, context: BrowserContext, keep: bool) -> None:
        try:
            if keep:
                path = self._path("trace", ".zip")
                await context.tracing.stop(path=str(path))
                self.items.append(
                    Evidence(
                        kind="trace", path=str(path), note="playwright show-trace <path>", captured_at=self._clock.now()
                    )
                )
            else:
                await context.tracing.stop()
        except Exception as exc:
            log.debug("tracing stop failed: %s", exc)
