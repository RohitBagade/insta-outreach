"""Text normalization helpers shared across layers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_RESERVED_PATHS = frozenset(
    {
        "p",
        "reel",
        "reels",
        "explore",
        "stories",
        "direct",
        "accounts",
        "about",
        "developer",
        "legal",
        "web",
        "tv",
    }
)
_WS_RE = re.compile(r"\s+")


class InvalidUsername(ValueError):
    pass


def canonical_username(raw: str) -> str:
    """Return the canonical (lowercase, bare) form of an Instagram username.

    Accepts ``@name``, ``name``, or a profile URL. Raises InvalidUsername when
    the input cannot be a username (post URLs, reserved paths, bad chars).
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidUsername("empty username")
    if "/" in value or value.startswith(("http:", "https:")):
        url = value if "://" in value else f"https://{value}"
        parts = [p for p in urlparse(url).path.split("/") if p]
        if not parts:
            raise InvalidUsername(f"no username in {raw!r}")
        value = parts[0]
    value = value.lstrip("@").lower()
    if value in _RESERVED_PATHS or not _USERNAME_RE.match(value):
        raise InvalidUsername(f"not a valid Instagram username: {raw!r}")
    if value.startswith(".") or value.endswith(".") or ".." in value:
        raise InvalidUsername(f"not a valid Instagram username: {raw!r}")
    return value


def try_canonical_username(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return canonical_username(raw)
    except InvalidUsername:
        return None


def normalize_message_text(text: str) -> str:
    """Normalization used for hashing/matching message bodies across channels.

    Instagram may re-render whitespace and some Unicode forms differently in the
    web UI vs. the API, so compare on NFC + collapsed whitespace + casefold.
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text or "")).strip().casefold()


def collapse_whitespace(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def registrable_domain(url_or_host: str | None) -> str | None:
    """Best-effort 'example.co.in' from a URL or host (no PSL dependency)."""
    if not url_or_host:
        return None
    raw = url_or_host.strip()
    host = urlparse(raw if "://" in raw else f"https://{raw}").hostname
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    # Handle common two-level public suffixes (co.in, co.uk, com.au, ...).
    if labels[-2] in {"co", "com", "net", "org", "gov", "edu", "ac"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
