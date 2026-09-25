from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from insta_outreach.config import OfferSettings, ScoringSettings
from insta_outreach.domain.enums import Channel, OpportunityType
from insta_outreach.domain.models import ContactInfo, PostObservation, ProfileObservation
from insta_outreach.intelligence.analyzer import LeadAnalyzer, SourceHints
from insta_outreach.intelligence.signals import (
    LinkKind,
    booking_mechanisms,
    classify_link,
    extract_emails,
    extract_phones,
    platform_name,
)
from insta_outreach.intelligence.website import WebsiteCheck, analyze_html
from insta_outreach.util.text import InvalidUsername, canonical_username, registrable_domain

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)


def profile(**kw: object) -> ProfileObservation:
    base: dict[str, object] = {
        "username": "brew.room",
        "full_name": "The Brew Room",
        "category": "Cafe",
        "biography": "Specialty coffee in Thane. Reservations: DM us",
        "followers": 2400,
        "posts_count": 80,
        "recent_posts": [
            PostObservation(posted_at=NOW - timedelta(days=d), caption="Cold brew #thanecafe")
            for d in (1, 4, 8, 12, 20)
        ],
        "observed_via": Channel.BROWSER,
        "observed_at": NOW,
    }
    base.update(kw)
    return ProfileObservation.model_validate(base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Brew.Room", "brew.room"),
        ("https://www.instagram.com/brew.room/", "brew.room"),
        ("instagram.com/brew_room?igsh=x", "brew_room"),
    ],
)
def test_canonical_username(raw: str, expected: str) -> None:
    assert canonical_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "https://www.instagram.com/p/ABC/", "bad name", ".dot", "a..b", "x" * 31])
def test_canonical_username_rejects(raw: str) -> None:
    with pytest.raises(InvalidUsername):
        canonical_username(raw)


def test_registrable_domain() -> None:
    assert registrable_domain("https://www.shop.brewroom.co.in/menu") == "brewroom.co.in"
    assert registrable_domain("brewroom.com") == "brewroom.com"


@pytest.mark.parametrize(
    ("url", "kind", "name"),
    [
        ("linktr.ee/brewroom", LinkKind.LINK_AGGREGATOR, "Linktree"),
        ("https://wa.me/919000000011", LinkKind.MESSAGING, "WhatsApp"),
        ("zomato.com/mumbai/x", LinkKind.DIRECTORY, "Zomato"),
        ("brew.wixsite.com/home", LinkKind.BUILDER, "Wix"),
        ("calendly.com/x", LinkKind.BOOKING_PLATFORM, "Calendly"),
        ("https://brewroom.example", LinkKind.CUSTOM_DOMAIN, None),
    ],
)
def test_link_classification(url: str, kind: LinkKind, name: str | None) -> None:
    assert classify_link(url) is kind
    assert platform_name(url) == name


def test_contact_extraction() -> None:
    bio = "Call 90000 00011 or +91-9000000012 · hello@BrewRoom.example"
    assert extract_phones(bio) == ["9000000011", "9000000012"]
    assert extract_emails(bio) == ["hello@brewroom.example"]


def test_booking_mechanisms() -> None:
    assert "dm" in booking_mechanisms("DM to book your appointment", [])
    assert "whatsapp" in booking_mechanisms("", [LinkKind.MESSAGING])
    assert "booking_link" in booking_mechanisms("", [LinkKind.BOOKING_PLATFORM])


def test_analyze_html_detects_builder_and_booking() -> None:
    builder, viewport, booking, title = analyze_html(
        '<html><head><title> Brew </title><meta name="viewport" content="width=device-width">'
        '<link href="https://static.wixstatic.com/x.css"></head><iframe src="https://calendly.com/x"></iframe>'
    )
    assert (builder, viewport, booking, title) == ("Wix", True, True, "Brew")


def analyzer() -> LeadAnalyzer:
    return LeadAnalyzer(ScoringSettings(), OfferSettings())


def test_no_website_with_dm_booking_is_qualified() -> None:
    result = analyzer().analyze(profile(), NOW, None, SourceHints(query_locations=["Thane"]))
    types = {o.type for o in result.opportunities}
    assert OpportunityType.NEW_WEBSITE in types and OpportunityType.ONLINE_BOOKING in types
    assert result.qualified and result.score >= 60
    assert result.facts["location"] == "Thane"
    assert result.facts["website_status"] == "no website linked in bio"


def test_facts_name_platforms_not_hostnames() -> None:
    result = analyzer().analyze(profile(website="https://linktr.ee/brewroom"), NOW)
    assert result.facts["website_status"] == "no own website; bio link goes to Linktree"
    assert "linktr.ee" not in " ".join(result.facts.values())


def test_business_with_modern_booking_site_is_not_contacted() -> None:
    site = WebsiteCheck(
        url="https://brewroom.example", checked_at=NOW, reachable=True, mobile_viewport=True, has_booking_widget=True
    )
    result = analyzer().analyze(
        profile(website="https://brewroom.example", biography="Specialty coffee in Thane. Book online below"), NOW, site
    )
    assert result.opportunities == []
    assert not result.qualified
    assert "no concrete LemmeDeliver opportunity" in result.not_qualified_reason


def test_broken_site_only_from_real_http_errors() -> None:
    broken = WebsiteCheck(
        url="https://brewroom.example", checked_at=NOW, reachable=False, status_code=404, error="HTTP 404"
    )
    unknown = WebsiteCheck(url="https://brewroom.example", checked_at=NOW, reachable=None, error="ProxyError")
    a = analyzer()
    assert OpportunityType.BROKEN_WEBSITE in {
        o.type for o in a.analyze(profile(website="https://brewroom.example"), NOW, broken).opportunities
    }
    assert OpportunityType.BROKEN_WEBSITE not in {
        o.type for o in a.analyze(profile(website="https://brewroom.example"), NOW, unknown).opportunities
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_private": True}, "private account"),
        ({"biography": "Website design & digital marketing agency"}, "competitor"),
        ({"followers": 50}, "too small"),
        ({"followers": 900_000}, "too large"),
        ({"category": None, "is_business": False, "biography": "Wanderlust | coffee addict"}, "personal profile"),
        ({"recent_posts": [PostObservation(posted_at=NOW - timedelta(days=400))]}, "inactive"),
    ],
)
def test_disqualifiers(overrides: dict[str, object], reason: str) -> None:
    result = analyzer().analyze(profile(**overrides), NOW)
    assert result.disqualified and not result.qualified
    assert any(reason in r for r in result.disqualify_reasons)


def test_contact_info_merged_into_signals() -> None:
    result = analyzer().analyze(profile(contact=ContactInfo(phones=["+91 90000 00013"])), NOW)
    assert "9000000013" in result.signals.phones
