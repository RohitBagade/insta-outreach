"""Checks a prospect's own website (plain HTTP, never through the IG session).

Truthfulness rule: a connection problem on *our* side (proxy, DNS, timeout)
is recorded as ``reachable=None`` (unknown), never as "their site is broken".
Only an actual HTTP error response from their server counts as broken,
because an outreach message may end up saying so.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from insta_outreach.util.clock import Clock

USER_AGENT = "Mozilla/5.0 (compatible; LemmeDeliver-SiteCheck/0.1; +https://lemmedeliver.com)"

_BUILDER_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("Wix", ("static.wixstatic.com", "wix.com website builder", "_wixcss", "wixsite.com")),
    ("Squarespace", ("static1.squarespace.com", "squarespace.com", "<!-- this is squarespace")),
    ("Shopify", ("cdn.shopify.com", "shopify.theme")),
    ("Webflow", ("assets.website-files.com", "webflow.com", 'content="webflow')),
    ("GoDaddy Website Builder", ("img1.wsimg.com", "go daddy website builder", "godaddysites.com")),
    ("Weebly", ("weebly.com", "editmysite.com")),
    ("Blogger", ("blogger.com", "blogspot.com")),
    ("Google Sites", ("sites.google.com", "google sites")),
    ("Zoho Sites", ("zohositescontent", "zohosites")),
    ("Dukaan", ("mydukaan", "dukaan.app")),
    ("WordPress", ("wp-content/", "wp-includes/", 'content="wordpress')),
]
_BOOKING_MARKERS = (
    "calendly.com",
    "cal.com/",
    "fresha.com",
    "setmore.com",
    "booksy.com",
    "simplybook",
    "acuityscheduling",
    "zohobookings",
    "bookings.zoho",
    "squareup.com/appointments",
    "picktime.com",
    "appointy.com",
    "youcanbook.me",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class WebsiteCheck(BaseModel):
    url: str
    checked_at: datetime
    reachable: bool | None  # None = could not determine (our network problem)
    status_code: int | None = None
    final_url: str | None = None
    https: bool | None = None
    builder: str | None = None
    mobile_viewport: bool | None = None
    has_booking_widget: bool | None = None
    title: str | None = None
    error: str | None = None

    @property
    def broken(self) -> bool:
        return self.reachable is False

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WebsiteChecker(Protocol):
    async def check(self, url: str) -> WebsiteCheck: ...


def analyze_html(html: str) -> tuple[str | None, bool, bool, str | None]:
    lowered = html.lower()
    builder = next((name for name, marks in _BUILDER_MARKERS if any(m in lowered for m in marks)), None)
    viewport = '<meta name="viewport"' in lowered or "name='viewport'" in lowered or "name=viewport" in lowered
    booking = any(marker in lowered for marker in _BOOKING_MARKERS)
    match = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", match.group(1)).strip()[:200] if match else None
    return builder, viewport, booking, title


class HttpWebsiteChecker:
    def __init__(self, clock: Clock, timeout: float = 8.0, max_bytes: int = 400_000) -> None:
        self._clock = clock
        self._timeout = timeout
        self._max_bytes = max_bytes

    async def check(self, url: str) -> WebsiteCheck:
        target = url if "://" in url else f"https://{url}"
        now = self._clock.now()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                response = await client.get(target)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as exc:
            return WebsiteCheck(url=url, checked_at=now, reachable=None, error=f"{type(exc).__name__}: {exc}"[:300])
        except httpx.HTTPError as exc:
            return WebsiteCheck(url=url, checked_at=now, reachable=None, error=f"{type(exc).__name__}: {exc}"[:300])
        final_url = str(response.url)
        status = response.status_code
        if status in (404, 410) or status >= 500:
            return WebsiteCheck(
                url=url,
                checked_at=now,
                reachable=False,
                status_code=status,
                final_url=final_url,
                https=final_url.startswith("https://"),
                error=f"HTTP {status}",
            )
        if status >= 400:  # 401/403/429: likely bot protection -> unknown, not broken
            return WebsiteCheck(
                url=url, checked_at=now, reachable=None, status_code=status, final_url=final_url, error=f"HTTP {status}"
            )
        builder, viewport, booking, title = analyze_html(response.text[: self._max_bytes])
        return WebsiteCheck(
            url=url,
            checked_at=now,
            reachable=True,
            status_code=status,
            final_url=final_url,
            https=final_url.startswith("https://"),
            builder=builder,
            mobile_viewport=viewport,
            has_booking_widget=booking,
            title=title,
        )
