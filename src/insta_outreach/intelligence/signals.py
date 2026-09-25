"""Deterministic signal extraction from observed profile data.

Only observable facts are extracted. Nothing here guesses; anything unknown
stays ``None`` so scoring can treat "unknown" differently from "absent".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlparse

from insta_outreach.config import NicheRule
from insta_outreach.domain.models import ProfileObservation


class LinkKind(StrEnum):
    CUSTOM_DOMAIN = "CUSTOM_DOMAIN"
    BUILDER = "BUILDER"
    LINK_AGGREGATOR = "LINK_AGGREGATOR"
    MESSAGING = "MESSAGING"
    DIRECTORY = "DIRECTORY"  # marketplace / listing page they don't own
    BOOKING_PLATFORM = "BOOKING_PLATFORM"
    SOCIAL = "SOCIAL"
    STOREFRONT = "STOREFRONT"


_HOST_KINDS: list[tuple[LinkKind, tuple[str, ...]]] = [
    (
        LinkKind.LINK_AGGREGATOR,
        (
            "linktr.ee",
            "beacons.ai",
            "bio.link",
            "linkin.bio",
            "taplink.cc",
            "lnk.bio",
            "msha.ke",
            "campsite.bio",
            "linkr.bio",
            "solo.to",
            "hoo.be",
            "tap.bio",
            "allmylinks.com",
            "koji.to",
            "snipfeed.co",
            "linkbio.co",
            "direct.me",
        ),
    ),
    (LinkKind.MESSAGING, ("wa.me", "api.whatsapp.com", "wa.link", "whatsapp.com", "t.me", "m.me", "ig.me")),
    (
        LinkKind.BOOKING_PLATFORM,
        (
            "calendly.com",
            "cal.com",
            "fresha.com",
            "setmore.com",
            "booksy.com",
            "simplybook.me",
            "acuityscheduling.com",
            "zohobookings.com",
            "bookings.zoho.in",
            "youcanbook.me",
            "picktime.com",
            "squareup.com/appointments",
            "appointy.com",
        ),
    ),
    (
        LinkKind.DIRECTORY,
        (
            "zomato.com",
            "swiggy.com",
            "practo.com",
            "justdial.com",
            "magicpin.in",
            "dineout.co.in",
            "eazydiner.com",
            "sulekha.com",
            "lybrate.com",
            "urbancompany.com",
            "maps.app.goo.gl",
            "goo.gl",
            "g.page",
            "g.co",
            "google.com",
            "tripadvisor.",
            "yelp.com",
            "zeomart.com",
            "indiamart.com",
        ),
    ),
    (
        LinkKind.BUILDER,
        (
            "wixsite.com",
            "wix.com",
            "wordpress.com",
            "blogspot.com",
            "business.site",
            "sites.google.com",
            "godaddysites.com",
            "weebly.com",
            "webflow.io",
            "carrd.co",
            "mystrikingly.com",
            "strikingly.com",
            "squarespace.com",
            "jimdosite.com",
            "site123.me",
            "webnode.page",
            "yolasite.com",
            "dukaan.app",
            "mydukaan.io",
            "zohosites.in",
            "zohosites.com",
        ),
    ),
    (
        LinkKind.STOREFRONT,
        ("myshopify.com", "instamojo.com", "amazon.in", "amazon.com", "etsy.com", "meesho.com", "flipkart.com"),
    ),
    (
        LinkKind.SOCIAL,
        (
            "facebook.com",
            "fb.com",
            "youtube.com",
            "youtu.be",
            "twitter.com",
            "x.com",
            "linkedin.com",
            "threads.net",
            "pinterest.com",
            "instagram.com",
        ),
    ),
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Indian mobile/landline numbers as typically written in bios.
_PHONE_RE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?(?:[6-9]\d{4}[\s-]?\d{5}|\d{2,4}[\s-]\d{6,8})(?!\d)")
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s,;]+|\b[a-z0-9-]+\.(?:com|in|co\.in|net|org|me|ee|io|app|site)(?:/[^\s,;]*)?\b", re.I
)
_HASHTAG_RE = re.compile(r"#(\w{3,40})")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'&-]{3,}")

_BOOKING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "dm",
        re.compile(
            r"\bdm\b[^.\n]{0,20}\b(book|order|appointment|enquir|inquir|reserv|price|details)"
            r"|\b(book|order|appointments?|enquir\w*|reserv\w*)\b[^.\n]{0,20}\b(dm|direct message|inbox)\b",
            re.I,
        ),
    ),
    ("whatsapp", re.compile(r"whats\s?app|\bwa\b|wa\.me", re.I)),
    ("phone", re.compile(r"\b(call|contact|ring)\b[^.\n]{0,15}\b(us|now|for|on|at)\b|📞|☎", re.I)),
    ("walk_in", re.compile(r"walk[\s-]?ins?\b", re.I)),
    ("booking_intent", re.compile(r"\b(book (now|your|an?|a slot)|appointments?|reservations?|slots?)\b", re.I)),
]

_STOPWORDS = frozenset(
    (
        "this that with from have your will just like more what when they them their there here were been into "
        "about over only also than then some very much many most such well make made best good great love follow "
        "link page post posts today now new day days time come visit shop store order orders call book available "
        "open daily mumbai india"
    ).split()
)


_PLATFORM_NAMES = {
    "linktr.ee": "Linktree",
    "beacons.ai": "Beacons",
    "bio.link": "bio.link page",
    "taplink.cc": "Taplink",
    "wa.me": "WhatsApp",
    "api.whatsapp.com": "WhatsApp",
    "wa.link": "WhatsApp",
    "whatsapp.com": "WhatsApp",
    "t.me": "Telegram",
    "m.me": "Messenger",
    "zomato.com": "Zomato",
    "swiggy.com": "Swiggy",
    "practo.com": "Practo",
    "justdial.com": "Justdial",
    "magicpin.in": "magicpin",
    "instamojo.com": "Instamojo",
    "calendly.com": "Calendly",
    "cal.com": "Cal.com",
    "fresha.com": "Fresha",
    "setmore.com": "Setmore",
    "booksy.com": "Booksy",
    "wixsite.com": "Wix",
    "wix.com": "Wix",
    "wordpress.com": "WordPress.com",
    "blogspot.com": "Blogger",
    "business.site": "Google Business Sites",
    "sites.google.com": "Google Sites",
    "godaddysites.com": "GoDaddy",
    "carrd.co": "Carrd",
    "webflow.io": "Webflow",
    "weebly.com": "Weebly",
    "squarespace.com": "Squarespace",
    "mystrikingly.com": "Strikingly",
    "myshopify.com": "Shopify",
    "facebook.com": "Facebook",
    "youtube.com": "YouTube",
    "google.com": "Google Maps",
    "maps.app.goo.gl": "Google Maps",
    "g.page": "Google Maps",
    "dukaan.app": "Dukaan",
}


def platform_name(url: str) -> str | None:
    """Human name of a known third-party platform ('WhatsApp', 'Linktree'...)."""
    raw = url.strip()
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower().removeprefix("www.")
    for marker, name in _PLATFORM_NAMES.items():
        if host == marker or host.endswith("." + marker):
            return name
    return None


def classify_link(url: str) -> LinkKind:
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    target = f"{host}{parsed.path.lower()}"
    for kind, markers in _HOST_KINDS:
        for marker in markers:
            if "/" in marker:
                if target.startswith(marker):
                    return kind
            elif host == marker or host.endswith("." + marker) or (marker.endswith(".") and marker in host):
                return kind
    return LinkKind.CUSTOM_DOMAIN


def extract_links(obs: ProfileObservation) -> list[str]:
    links: list[str] = []
    for candidate in [obs.website, *obs.bio_links]:
        if candidate and candidate not in links:
            links.append(candidate)
    for match in _URL_RE.findall(obs.biography or ""):
        cleaned = match.rstrip(".)")
        if cleaned not in links and "@" not in cleaned:
            links.append(cleaned)
    return links


def extract_emails(text: str) -> list[str]:
    return sorted({m.lower().rstrip(".") for m in _EMAIL_RE.findall(text or "")})


def extract_phones(text: str) -> list[str]:
    found = []
    for match in _PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= 10 and digits[-10:] not in found:
            found.append(digits[-10:])
    return found


def booking_mechanisms(bio: str | None, link_kinds: list[LinkKind]) -> list[str]:
    text = bio or ""
    found = [name for name, pattern in _BOOKING_PATTERNS if pattern.search(text)]
    if LinkKind.MESSAGING in link_kinds and "whatsapp" not in found:
        found.append("whatsapp")
    if LinkKind.BOOKING_PLATFORM in link_kinds:
        found.append("booking_link")
    return found


def match_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower().strip()
    if not keyword:
        return False
    if len(keyword) <= 4 or " " in keyword:
        return re.search(rf"(?<![a-z]){re.escape(keyword)}", text) is not None
    return keyword in text


def detect_niche(obs: ProfileObservation, rules: list[NicheRule]) -> tuple[NicheRule, str] | None:
    username_words = obs.username.replace(".", " ").replace("_", " ")
    haystack = " ".join(filter(None, [obs.category, obs.full_name, obs.biography, username_words])).lower()
    best: tuple[NicheRule, str] | None = None
    for rule in rules:
        for keyword in rule.keywords:
            if match_keyword(haystack, keyword) and (best is None or rule.weight > best[0].weight):
                best = (rule, keyword)
                break
    return best


def detect_location(obs: ProfileObservation, locations: list[str]) -> str | None:
    haystack = " ".join(
        filter(
            None,
            [
                obs.biography,
                obs.category,
                obs.full_name,
                obs.contact.address,
                obs.username.replace("_", " ").replace(".", " "),
            ],
        )
    ).lower()
    # Prefer the most specific (longest) location name that matches.
    for location in sorted(locations, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(location.lower())}(?![a-z])", haystack):
            return location
    return None


def detect_competitor(obs: ProfileObservation, keywords: list[str]) -> str | None:
    haystack = " ".join(filter(None, [obs.category, obs.full_name, obs.biography])).lower()
    return next((k for k in keywords if k.lower() in haystack), None)


@dataclass
class Activity:
    last_post_at: datetime | None
    posts_last_30d: int | None
    posts_last_90d: int | None
    reels_share: float | None


def activity(obs: ProfileObservation, now: datetime) -> Activity:
    dated = [p.posted_at for p in obs.recent_posts if p.posted_at is not None]
    if not dated:
        return Activity(None, None, None, None)
    types = [p.media_type for p in obs.recent_posts if p.media_type]
    reels = sum(1 for t in types if t and t.upper() in ("VIDEO", "REEL", "REELS"))
    return Activity(
        last_post_at=max(dated),
        posts_last_30d=sum(1 for d in dated if d >= now - timedelta(days=30)),
        posts_last_90d=sum(1 for d in dated if d >= now - timedelta(days=90)),
        reels_share=(reels / len(types)) if types else None,
    )


def themes(obs: ProfileObservation, limit: int = 6) -> list[str]:
    tags: Counter[str] = Counter()
    words: Counter[str] = Counter()
    for post in obs.recent_posts:
        text = f"{post.caption or ''} {post.alt_text or ''}"
        tags.update(t.lower() for t in _HASHTAG_RE.findall(text))
        words.update(
            w.lower() for w in _WORD_RE.findall(_HASHTAG_RE.sub(" ", post.caption or "")) if w.lower() not in _STOPWORDS
        )
    ranked = [f"#{t}" for t, _ in tags.most_common(limit)]
    ranked += [w for w, c in words.most_common(limit * 2) if c >= 2 and f"#{w}" not in ranked]
    return ranked[:limit]


@dataclass
class Signals:
    links: list[str] = field(default_factory=list)
    link_kinds: dict[str, str] = field(default_factory=dict)
    own_website: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    booking: list[str] = field(default_factory=list)
    niche: str | None = None
    niche_keyword: str | None = None
    niche_weight: float = 0.0
    location: str | None = None
    competitor: str | None = None
    activity: Activity | None = None
    themes: list[str] = field(default_factory=list)
    business_like: bool | None = None

    def as_dict(self) -> dict[str, object]:
        act = self.activity
        return {
            "links": self.links,
            "link_kinds": self.link_kinds,
            "own_website": self.own_website,
            "booking": self.booking,
            "niche": self.niche,
            "niche_keyword": self.niche_keyword,
            "location": self.location,
            "competitor": self.competitor,
            "business_like": self.business_like,
            "posts_last_30d": act.posts_last_30d if act else None,
            "posts_last_90d": act.posts_last_90d if act else None,
            "reels_share": act.reels_share if act else None,
        }


def extract_signals(
    obs: ProfileObservation,
    now: datetime,
    niches: list[NicheRule],
    locations: list[str],
    competitor_keywords: list[str],
) -> Signals:
    links = extract_links(obs)
    kinds = {link: classify_link(link) for link in links}
    own = next((u for u, k in kinds.items() if k in (LinkKind.CUSTOM_DOMAIN, LinkKind.BUILDER)), None)
    bio = obs.biography or ""
    emails = sorted({*extract_emails(bio), *(e.lower() for e in obs.contact.emails)})
    phones = extract_phones(bio)
    for phone in obs.contact.phones:
        for digits in extract_phones(phone) or [re.sub(r"\D", "", phone)[-10:]]:
            if digits and digits not in phones:
                phones.append(digits)
    niche = detect_niche(obs, niches)
    signals = Signals(
        links=links,
        link_kinds={u: k.value for u, k in kinds.items()},
        own_website=own,
        emails=emails,
        phones=phones,
        booking=booking_mechanisms(bio, list(kinds.values())),
        niche=niche[0].name if niche else None,
        niche_keyword=niche[1] if niche else None,
        niche_weight=niche[0].weight if niche else 0.0,
        location=detect_location(obs, locations),
        competitor=detect_competitor(obs, competitor_keywords),
        activity=activity(obs, now),
        themes=themes(obs),
    )
    if phones and "phone" not in signals.booking:
        signals.booking.append("phone")
    signals.business_like = _business_like(obs, signals)
    return signals


def _business_like(obs: ProfileObservation, s: Signals) -> bool | None:
    if obs.is_business is True or obs.category:
        return True
    evidence = sum(
        [
            bool(s.niche),
            bool(s.emails or s.phones or obs.contact.address),
            bool(s.links),
            any(b in s.booking for b in ("dm", "whatsapp", "booking_intent", "booking_link")),
        ]
    )
    if evidence >= 2:
        return True
    if obs.is_business is False and evidence == 0:
        return False
    if obs.biography is None and obs.is_business is None:
        return None
    return evidence >= 1 if obs.is_business is None else False
