"""Read-only extraction of profile, post, list and thread data from pages.

Layered for resilience: long-stable signals first (URL shapes, <meta> tags,
hrefs, element geometry), visible-text parsing second. Pure string parsers
are separate functions so they can be unit-tested without a browser.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Page

from insta_outreach.domain.enums import MessageDirection
from insta_outreach.domain.models import ThreadMessage
from insta_outreach.util.text import try_canonical_username

_COUNT_RE = re.compile(r"([\d.,]+\s*[KkMm]?)\s+(Followers?|Following|Posts?)", re.I)
_OG_TITLE_RE = re.compile(r"^(?P<name>.*?)\s*\(@(?P<username>[A-Za-z0-9._]+)\)")
_POST_DESC_RE = re.compile(
    r"-\s*(?P<username>[A-Za-z0-9._]+)\s+on\s+(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?::\s*\"?(?P<caption>.*))?", re.S
)
_ALT_DATE_RE = re.compile(r"\bon ([A-Z][a-z]+ \d{1,2}, \d{4})")
_HEADER_NOISE = re.compile(
    r"^(follow|following|message|contact|email|call|directions|more|options|edit profile|"
    r"view archive|ad tools|share profile|similar accounts|follow back|requested|posts?|followers|"
    r"reels|tagged|saved|verified|\d[\d.,]*\s*[km]?\s*(posts?|followers|following))$",
    re.I,
)


def parse_count(raw: str) -> int | None:
    value = raw.strip().replace(",", "").lower()
    multiplier = 1
    if value.endswith("k"):
        multiplier, value = 1_000, value[:-1]
    elif value.endswith("m"):
        multiplier, value = 1_000_000, value[:-1]
    try:
        return int(float(value) * multiplier)
    except ValueError:
        return None


def parse_counts(description: str | None) -> dict[str, int]:
    """'1,234 Followers, 567 Following, 89 Posts - See Instagram photos ...'"""
    counts: dict[str, int] = {}
    for number, label in _COUNT_RE.findall(description or ""):
        key = label.lower().rstrip("s") if label.lower() != "following" else "following"
        key = {"follower": "followers", "post": "posts"}.get(key, key)
        parsed = parse_count(number)
        if parsed is not None and key not in counts:
            counts[key] = parsed
    return counts


def parse_og_title(title: str | None) -> tuple[str | None, str | None]:
    match = _OG_TITLE_RE.match((title or "").strip())
    if not match:
        return None, None
    name = match.group("name").strip() or None
    return name, match.group("username").lower()


def parse_date(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_post_description(description: str | None) -> tuple[str | None, datetime | None, str | None]:
    """'12 likes, 3 comments - username on September 20, 2026: "caption"'"""
    match = _POST_DESC_RE.search(description or "")
    if not match:
        return None, None, None
    caption = (match.group("caption") or "").strip().rstrip('".').strip() or None
    return match.group("username").lower(), parse_date(match.group("date")), caption


def parse_alt_date(alt: str | None) -> datetime | None:
    match = _ALT_DATE_RE.search(alt or "")
    return parse_date(match.group(1)) if match else None


def decode_external_link(href: str) -> str:
    """Instagram wraps outbound links as l.instagram.com/?u=<encoded>."""
    parsed = urlparse(href)
    if parsed.hostname and parsed.hostname.startswith("l.instagram.com"):
        target = parse_qs(parsed.query).get("u", [""])[0]
        return unquote(target) or href
    return href


def username_from_href(href: str | None) -> str | None:
    if not href:
        return None
    path = urlparse(href).path
    parts = [p for p in path.split("/") if p]
    if len(parts) != 1:
        return None
    return try_canonical_username(parts[0])


def split_header_lines(header_text: str, username: str, full_name: str | None) -> tuple[str | None, list[str]]:
    """Return (category, bio_lines) from the profile header's visible text."""
    lines = [ln.strip() for ln in header_text.splitlines() if ln.strip()]
    cleaned = [ln for ln in lines if ln.lower() != username.lower() and not _HEADER_NOISE.match(ln)]
    if full_name and cleaned and cleaned[0] == full_name:
        cleaned = cleaned[1:]
    category = None
    if cleaned and len(cleaned[0]) <= 40 and re.fullmatch(r"[A-Z][A-Za-z&/,' -]+", cleaned[0]) and len(cleaned) > 1:
        category, cleaned = cleaned[0], cleaned[1:]
    return category, cleaned


# -- page-level extraction (needs a browser) ----------------------------------------------
_META_JS = """() => {
  const q = s => { const el = document.querySelector(s); return el ? el.getAttribute('content') : null; };
  return {og_desc: q('meta[property="og:description"]'), desc: q('meta[name="description"]'),
          og_title: q('meta[property="og:title"]'), title: document.title, og_url: q('meta[property="og:url"]')};
}"""

_HEADER_JS = """() => {
  const header = document.querySelector('header');
  if (!header) return null;
  const links = Array.from(header.querySelectorAll('a[href]'))
    .map(a => ({href: a.getAttribute('href'), text: (a.innerText || '').trim()}));
  const buttons = Array.from(header.querySelectorAll('button, [role="button"]'))
    .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim());
  // Read the bio text without links, buttons, the username heading and the stats list:
  // hide them for the duration of one layout-aware innerText read, then restore.
  const hidden = [];
  header.querySelectorAll('a, button, [role="button"], ul, h1, h2, svg').forEach(el => {
    hidden.push([el, el.style.display]); el.style.display = 'none'; });
  const text = header.innerText || '';
  hidden.forEach(([el, display]) => { el.style.display = display; });
  return {text, links, buttons, verified: !!header.querySelector('svg[aria-label="Verified"]')};
}"""

_GRID_JS = """(limit) => Array.from(document.querySelectorAll('main a[href*="/p/"], main a[href*="/reel/"]'))
  .slice(0, limit).map(a => { const img = a.querySelector('img');
    return {href: a.getAttribute('href'), alt: img ? img.getAttribute('alt') : null,
            reel: a.getAttribute('href').includes('/reel/')}; })"""

_THREAD_JS = """() => {
  const rows = Array.from(document.querySelectorAll('[role="row"]'));
  const container = document.querySelector('[role="grid"], [aria-label^="Messages in conversation"]') || document.body;
  const box = container.getBoundingClientRect();
  const mid = box.left + box.width / 2;
  const out = [];
  for (const row of rows) {
    const bubble = row.querySelector('[data-message-text], [dir="auto"]') || row;
    const text = (bubble.innerText || '').trim();
    if (!text) continue;
    const r = bubble.getBoundingClientRect();
    if (r.width === 0) continue;
    // Our own messages render on the right-hand side of the conversation.
    out.push({text, outbound: (r.left + r.width / 2) > mid});
  }
  return out;
}"""


async def page_meta(page: Page) -> dict[str, Any]:
    try:
        return dict(await page.evaluate(_META_JS) or {})
    except Exception:
        return {}


async def profile_header(page: Page) -> dict[str, Any] | None:
    try:
        return await page.evaluate(_HEADER_JS)
    except Exception:
        return None


async def post_grid(page: Page, limit: int) -> list[dict[str, Any]]:
    try:
        return list(await page.evaluate(_GRID_JS, limit) or [])
    except Exception:
        return []


async def thread_messages(page: Page) -> list[ThreadMessage]:
    """Messages in the open thread; direction from bubble geometry (right side = ours)."""
    try:
        rows = await page.evaluate(_THREAD_JS) or []
    except Exception:
        return []
    return [
        ThreadMessage(
            direction=MessageDirection.OUTBOUND if r["outbound"] else MessageDirection.INBOUND, text=r["text"]
        )
        for r in rows
    ]


async def link_usernames(page: Page, scope_selector: str, limit: int, exclude: set[str]) -> list[str]:
    try:
        # Only rendered links count (hidden sections exist in the DOM before they are revealed).
        hrefs = await page.evaluate(
            """([sel, limit]) => { const root = document.querySelector(sel) || document;
               return Array.from(root.querySelectorAll('a[href]')).filter(a => a.getClientRects().length > 0)
                 .map(a => a.getAttribute('href')).slice(0, limit * 4); }""",
            [scope_selector, limit],
        )
    except Exception:
        return []
    seen: list[str] = []
    for href in hrefs or []:
        username = username_from_href(href)
        if username and username not in exclude and username not in seen:
            seen.append(username)
        if len(seen) >= limit:
            break
    return seen
