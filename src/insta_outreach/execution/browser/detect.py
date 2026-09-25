"""Page-state detection: is this the page we expected, or a barrier?

Order matters and is deliberately conservative:
  1. URL rules (checkpoint / login / suspended URLs are unambiguous)
  2. text of open dialogs (Instagram shows limits/blocks in dialogs)
  3. visible selectors (captcha iframes, password field)
  4. allowlisted benign dialogs (e.g. "Turn on notifications" -> "Not Now")
  5. expected page markers -> OK
  6. only if markers are missing: full-page text (not-found pages, forms)
Page text is only consulted when the expected page did not render, so a
business whose bio says "try again later" is never mistaken for a limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page

from insta_outreach.execution.browser.ui_map import UiMap

_DIALOG_TEXT_JS = """() => Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
  .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(el => el.innerText || '').join('\\n').slice(0, 5000)"""
_PAGE_TEXT_JS = "() => (document.body ? document.body.innerText : '').slice(0, 20000)"


@dataclass
class PageState:
    kind: str  # ok | benign_dialog | unknown | <DetectorRule.state>
    code: str = ""
    detail: str = ""
    url: str = ""
    dialog_button: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


class PageStateDetector:
    def __init__(self, ui: UiMap) -> None:
        self._ui = ui

    async def _visible(self, page: Page, selector: str) -> bool:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(min(count, 5)):
                if await locator.nth(i).is_visible():
                    return True
        except Exception:
            return False
        return False

    async def has_markers(self, page: Page, expect: str) -> bool:
        for selector in self._ui.page_markers.get(expect, []):
            if await self._visible(page, selector):
                return True
        return False

    async def detect(self, page: Page, expect: str | None) -> PageState:
        url = page.url
        try:
            dialog_text = (await page.evaluate(_DIALOG_TEXT_JS) or "").lower()
        except Exception:
            dialog_text = ""
        for rule in self._ui.detectors:
            for pattern in rule.url_patterns:
                if re.search(pattern, url):
                    return PageState(rule.state, rule.code, f"url matched {pattern}", url)
            for pattern in rule.dialog_text:
                match = re.search(pattern, dialog_text)
                if match:
                    return PageState(rule.state, rule.code, _excerpt(dialog_text, match), url)
            for selector in rule.selectors:
                if await self._visible(page, selector):
                    return PageState(rule.state, rule.code, f"visible {selector}", url)
        for benign in self._ui.benign_dialogs:
            if re.search(benign.text, dialog_text):
                return PageState("benign_dialog", "benign_dialog", benign.text, url, benign.button)
        if expect is None or await self.has_markers(page, expect):
            return PageState("ok", url=url)
        try:
            page_text = (await page.evaluate(_PAGE_TEXT_JS) or "").lower()
        except Exception:
            page_text = ""
        for rule in self._ui.detectors:
            for pattern in rule.page_text:
                match = re.search(pattern, page_text)
                if match:
                    return PageState(rule.state, rule.code, _excerpt(page_text, match), url)
        return PageState("unknown", "expected_markers_missing", f"no marker for '{expect}'", url)

    async def page_text_matches(self, page: Page, state: str) -> PageState | None:
        """Check page-level rules for one state even when markers are present
        (e.g. 'can't receive messages' banners inside a normal thread view)."""
        try:
            page_text = (await page.evaluate(_PAGE_TEXT_JS) or "").lower()
        except Exception:
            return None
        for rule in self._ui.detectors:
            if rule.state != state:
                continue
            for pattern in rule.page_text + rule.dialog_text:
                match = re.search(pattern, page_text)
                if match:
                    return PageState(rule.state, rule.code, _excerpt(page_text, match), page.url)
        return None


def _excerpt(text: str, match: re.Match[str], radius: int = 80) -> str:
    start, end = max(0, match.start() - radius), min(len(text), match.end() + radius)
    return " ".join(text[start:end].split())
