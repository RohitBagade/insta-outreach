"""Quality and truthfulness checks every outbound text must pass.

Mirrors the Web Foundry "truth" standard: no fabricated numbers, prices or
claims, no generic filler, and every first message genuinely written for
that business rather than a mass template.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from insta_outreach.config import OfferSettings

INSTAGRAM_MAX_BYTES = 1000  # Instagram messaging API limit (UTF-8 bytes)

_URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|in|co|io|me|net|org|app|site)\b", re.I)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_MONEY_RE = re.compile(r"₹|\brs\.?\s?\d|\binr\b|\$\s?\d|\bprice[sd]?\s*(?:starts?|from|at)\b|%", re.I)
_PLACEHOLDER_RE = re.compile(r"[{}\[\]<>]|\b(?:TODO|TBD|XXX|lorem|ipsum|INSERT|PLACEHOLDER)\b|\bN/A\b")
_WORD_RE = re.compile(r"[a-z0-9']+")


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" or 0x1F300 <= ord(ch) <= 0x1FAFF


def _shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    return {tuple(words[i : i + size]) for i in range(max(0, len(words) - size + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class MessageValidator:
    def __init__(self, offer: OfferSettings, max_similarity: float = 0.8) -> None:
        self._offer = offer
        self._max_similarity = max_similarity

    def validate(
        self,
        text: str,
        *,
        kind: str,
        facts: dict[str, str],
        recent_texts: Iterable[str] = (),
    ) -> list[str]:
        problems: list[str] = []
        stripped = (text or "").strip()
        if not stripped:
            return ["message is empty"]
        limit = self._offer.max_message_chars if kind != "reply" else max(600, self._offer.max_message_chars)
        if len(stripped) > limit:
            problems.append(f"too long ({len(stripped)} > {limit} chars)")
        if len(stripped.encode("utf-8")) > INSTAGRAM_MAX_BYTES:
            problems.append(f"exceeds Instagram's {INSTAGRAM_MAX_BYTES}-byte message limit")
        lowered = stripped.lower()
        for phrase in self._offer.banned_phrases:
            if phrase.lower() in lowered:
                problems.append(f"banned phrase: {phrase!r}")
        if _PLACEHOLDER_RE.search(stripped):
            problems.append("contains placeholder/template syntax")
        if _MONEY_RE.search(stripped):
            problems.append("mentions prices/percentages (never quote numbers in outreach)")
        if kind == "initial" and not self._offer.include_link_in_first_message and _URL_RE.search(stripped):
            problems.append("contains a link (links are disabled for first messages)")
        allowed_numbers = set()
        for source in [*facts.values(), self._offer.pitch, *self._offer.services]:
            allowed_numbers.update(_NUMBER_RE.findall(source))
        for number in _NUMBER_RE.findall(stripped):
            if number not in allowed_numbers:
                problems.append(f"number {number!r} does not come from the observed facts")
                break
        if "#" in stripped:
            problems.append("contains a hashtag")
        emojis = sum(1 for ch in stripped if _is_emoji(ch))
        if emojis > 1:
            problems.append(f"too many emojis ({emojis})")
        if stripped.count("!") > 2:
            problems.append("too many exclamation marks")
        if stripped.count("\n") > 4:
            problems.append("too many line breaks")
        shouting = [w for w in re.findall(r"\b[A-Z]{4,}\b", stripped) if w not in ("SEO", "HTTPS")]
        if len(shouting) > 1:
            problems.append("all-caps shouting")
        if kind == "initial":
            if self._offer.brand.lower() not in lowered:
                problems.append(f"does not introduce {self._offer.brand}")
            if "?" not in stripped:
                problems.append("does not end with a question inviting a reply")
        for previous in recent_texts:
            score = similarity(stripped, previous)
            if score >= self._max_similarity:
                problems.append(f"too similar ({score:.0%}) to a recently sent message: not personalized")
                break
        return problems
