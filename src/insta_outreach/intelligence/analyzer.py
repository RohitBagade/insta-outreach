"""Lead analysis: opportunity classification + explainable scoring.

Everything is deterministic and explained: each score component and each
disqualifier carries a reason, and each opportunity has a fact-based
rationale. The resulting *facts* dict is the only material personalization
is allowed to use, which is how "no fabricated claims" is enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from insta_outreach.config import OfferSettings, ScoringSettings
from insta_outreach.domain.enums import OpportunityType
from insta_outreach.domain.models import ProfileObservation
from insta_outreach.intelligence.signals import LinkKind, Signals, extract_signals, platform_name
from insta_outreach.intelligence.website import WebsiteCheck
from insta_outreach.util.text import collapse_whitespace, truncate

_LINK_LABEL = {
    LinkKind.LINK_AGGREGATOR: "a link-in-bio page",
    LinkKind.MESSAGING: "a WhatsApp/chat link",
    LinkKind.DIRECTORY: "a listing on another platform",
    LinkKind.SOCIAL: "another social profile",
    LinkKind.STOREFRONT: "a marketplace storefront",
    LinkKind.BOOKING_PLATFORM: "a booking page",
}
_BOOKING_LABEL = {
    "dm": "asks customers to DM to book or enquire",
    "whatsapp": "takes enquiries/bookings over WhatsApp",
    "phone": "lists a phone number for bookings/enquiries",
}


@dataclass
class SourceHints:
    """What discovery already implies (e.g. found by searching 'cafe Thane')."""

    query_locations: list[str] = field(default_factory=list)
    query_niches: list[str] = field(default_factory=list)


@dataclass
class Opportunity:
    type: OpportunityType
    rationale: str
    weight: int

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "rationale": self.rationale, "weight": self.weight}


@dataclass
class LeadAnalysis:
    signals: Signals
    website: WebsiteCheck | None
    opportunities: list[Opportunity]
    facts: dict[str, str]
    score: int
    breakdown: dict[str, Any]
    disqualify_reasons: list[str]
    min_score: int

    @property
    def disqualified(self) -> bool:
        return bool(self.disqualify_reasons)

    @property
    def qualified(self) -> bool:
        # A good-looking business with nothing concrete to offer is not a lead:
        # outreach always needs at least one observed LemmeDeliver opportunity.
        return not self.disqualified and bool(self.opportunities) and self.score >= self.min_score

    @property
    def not_qualified_reason(self) -> str:
        if not self.opportunities:
            return f"score {self.score} but no concrete LemmeDeliver opportunity observed"
        return f"score {self.score} below {self.min_score}"

    @property
    def hooks(self) -> list[str]:
        return [f"{k}: {v}" for k, v in self.facts.items()]

    @property
    def top_opportunity(self) -> Opportunity | None:
        return max(self.opportunities, key=lambda o: o.weight, default=None)


def _host(url: str) -> str:
    raw = url if "://" in url else f"https://{url}"
    return (urlparse(raw).hostname or url).lower().removeprefix("www.")


class LeadAnalyzer:
    def __init__(self, scoring: ScoringSettings, offer: OfferSettings) -> None:
        self._scoring = scoring
        self._offer = offer

    def signals(self, obs: ProfileObservation, now: datetime) -> Signals:
        return extract_signals(
            obs,
            now,
            self._scoring.niches,
            self._scoring.target_locations,
            self._scoring.competitor_keywords,
        )

    def analyze(
        self,
        obs: ProfileObservation,
        now: datetime,
        website: WebsiteCheck | None = None,
        hints: SourceHints | None = None,
        min_score: int | None = None,
    ) -> LeadAnalysis:
        hints = hints or SourceHints()
        s = self.signals(obs, now)
        opportunities = self._opportunities(obs, s, website)
        score, breakdown = self._score(obs, s, opportunities, hints, now)
        return LeadAnalysis(
            signals=s,
            website=website,
            opportunities=opportunities,
            facts=self._facts(obs, s, website),
            score=score,
            breakdown=breakdown,
            disqualify_reasons=self._disqualifiers(obs, s, now),
            min_score=self._scoring.min_score_to_contact if min_score is None else min_score,
        )

    # -- opportunities -------------------------------------------------------
    def _opportunities(self, obs: ProfileObservation, s: Signals, website: WebsiteCheck | None) -> list[Opportunity]:
        w = self._scoring.opportunity_weights
        found: list[Opportunity] = []
        kinds = {url: LinkKind(kind) for url, kind in s.link_kinds.items()}
        if s.own_website is None:
            other = [(u, k) for u, k in kinds.items() if k is not LinkKind.CUSTOM_DOMAIN]
            if other:
                url, kind = other[0]
                name = platform_name(url) or _LINK_LABEL.get(kind, "another site")
                rationale = f"bio links to {name} ({_LINK_LABEL.get(kind, 'another site')}) rather than an own website"
            else:
                rationale = "no website is linked on the profile"
            found.append(Opportunity(OpportunityType.NEW_WEBSITE, rationale, w.get("NEW_WEBSITE", 30)))
        else:
            host = _host(s.own_website)
            if website is not None and website.broken:
                found.append(
                    Opportunity(
                        OpportunityType.BROKEN_WEBSITE,
                        f"the linked site {host} returned {website.error or 'an error'} when checked",
                        w.get("BROKEN_WEBSITE", 28),
                    )
                )
            builder = None
            if kinds.get(s.own_website) is LinkKind.BUILDER:
                builder = platform_name(s.own_website) or host
            if website is not None and website.builder:
                builder = website.builder
            if builder:
                found.append(
                    Opportunity(
                        OpportunityType.WEBSITE_REBUILD,
                        f"the site {host} is built on {builder}",
                        w.get("WEBSITE_REBUILD", 18),
                    )
                )
            elif website is not None and website.reachable and website.mobile_viewport is False:
                found.append(
                    Opportunity(
                        OpportunityType.WEBSITE_REBUILD,
                        f"the site {host} has no mobile viewport set (not mobile-optimised)",
                        w.get("WEBSITE_REBUILD", 18),
                    )
                )

        has_booking_link = "booking_link" in s.booking or bool(website and website.has_booking_widget)
        manual = [m for m in ("dm", "whatsapp", "phone") if m in s.booking]
        if manual and not has_booking_link and ("booking_intent" in s.booking or "dm" in manual or s.niche is not None):
            found.append(
                Opportunity(
                    OpportunityType.ONLINE_BOOKING,
                    f"the profile {_BOOKING_LABEL[manual[0]]}; no online booking link found",
                    w.get("ONLINE_BOOKING", 22),
                )
            )

        act = s.activity
        weak_web = s.own_website is None or any(
            o.type in (OpportunityType.BROKEN_WEBSITE, OpportunityType.WEBSITE_REBUILD) for o in found
        )
        if act and act.posts_last_30d and act.posts_last_30d >= 4 and weak_web:
            found.append(
                Opportunity(
                    OpportunityType.VISUAL_SHOWCASE,
                    f"posts actively ({act.posts_last_30d} posts in the last 30 days) "
                    "with no strong site to showcase the work",
                    w.get("VISUAL_SHOWCASE", 10),
                )
            )
        return found

    # -- scoring ---------------------------------------------------------------
    def _score(
        self,
        obs: ProfileObservation,
        s: Signals,
        opportunities: list[Opportunity],
        hints: SourceHints,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        cfg = self._scoring
        b: dict[str, Any] = {}
        b["niche_fit"] = min(25, round(25 * s.niche_weight)) if s.niche else 0
        if s.location:
            b["location_fit"] = 15
        elif hints.query_locations:
            b["location_fit"] = 8  # found via a location-targeted query, unconfirmed on profile
        else:
            b["location_fit"] = 0
        if opportunities:
            best = max(o.weight for o in opportunities)
            b["opportunity"] = min(30, best + (5 if len(opportunities) > 1 else 0))
        else:
            b["opportunity"] = 0
        act = s.activity
        if act is None or act.last_post_at is None:
            b["activity"] = 5
        else:
            age = now - act.last_post_at
            b["activity"] = (
                15
                if age <= timedelta(days=30)
                else 10
                if age <= timedelta(days=60)
                else 5
                if age <= timedelta(days=cfg.inactive_after_days)
                else 0
            )
        followers = obs.followers
        low, high = cfg.sweet_spot_followers
        if followers is None:
            b["size_fit"] = 3
        elif low <= followers <= high:
            b["size_fit"] = 10
        elif cfg.min_followers <= followers <= cfg.max_followers:
            b["size_fit"] = 5
        else:
            b["size_fit"] = 0
        b["business"] = 5 if s.business_like else 0
        total = sum(v for v in b.values() if isinstance(v, int))
        b["niche"] = s.niche
        b["location"] = s.location
        return max(0, min(100, total)), b

    def _disqualifiers(self, obs: ProfileObservation, s: Signals, now: datetime) -> list[str]:
        cfg = self._scoring
        reasons: list[str] = []
        if obs.is_private:
            reasons.append("private account")
        if s.competitor:
            reasons.append(f"looks like a competitor ('{s.competitor}')")
        if cfg.require_business_signals and s.business_like is False:
            reasons.append("personal profile: no business signals")
        if obs.followers is not None and obs.followers < cfg.min_followers:
            reasons.append(f"too small ({obs.followers} followers < {cfg.min_followers})")
        if obs.followers is not None and obs.followers > cfg.max_followers:
            reasons.append(f"too large ({obs.followers} followers): likely a chain or brand")
        act = s.activity
        if act and act.last_post_at and now - act.last_post_at > timedelta(days=cfg.inactive_after_days):
            reasons.append(f"inactive: last post {act.last_post_at:%Y-%m-%d}")
        if obs.posts_count == 0:
            reasons.append("no posts")
        return reasons

    # -- facts for personalization ------------------------------------------------
    def _facts(self, obs: ProfileObservation, s: Signals, website: WebsiteCheck | None) -> dict[str, str]:
        facts: dict[str, str] = {
            "business_name": collapse_whitespace(obs.full_name) or obs.username,
            "username": f"@{obs.username}",
        }
        if obs.category:
            facts["category"] = obs.category
        if s.niche:
            facts["niche"] = s.niche
        if s.location:
            facts["location"] = s.location.title()
        if obs.biography:
            facts["bio_excerpt"] = truncate(collapse_whitespace(obs.biography), 160)
        # Platform names, not hostnames: messages must not contain link-like text.
        if s.own_website is None:
            other = [(u, LinkKind(k)) for u, k in s.link_kinds.items() if k != LinkKind.CUSTOM_DOMAIN.value]
            if other:
                url, kind = other[0]
                facts["website_status"] = (
                    f"no own website; bio link goes to {platform_name(url) or _LINK_LABEL.get(kind, 'another site')}"
                )
            else:
                facts["website_status"] = "no website linked in bio"
        else:
            builder = (
                website.builder
                if website is not None and website.builder
                else platform_name(s.own_website)
                if s.link_kinds.get(s.own_website) == LinkKind.BUILDER.value
                else None
            )
            if website is not None and website.broken:
                facts["website_status"] = f"the linked website returned {website.error} when checked"
            elif builder:
                facts["website_status"] = f"has a website built on {builder}"
            else:
                facts["website_status"] = "has its own website"
            if website is not None and website.reachable and website.has_booking_widget is False:
                facts["online_booking"] = "no online booking widget found on the website"
        manual = [m for m in ("dm", "whatsapp", "phone") if m in s.booking]
        if manual:
            facts["booking_method"] = _BOOKING_LABEL[manual[0]]
        if s.themes:
            facts["recent_post_themes"] = ", ".join(s.themes[:4])
        if s.activity and s.activity.posts_last_30d:
            facts["posting_activity"] = f"{s.activity.posts_last_30d} posts in the last 30 days"
        return facts
