from __future__ import annotations

from typing import Any

import pytest

from insta_outreach.config import LLMSettings, OfferSettings
from insta_outreach.domain.enums import OpportunityType, ReplyIntent
from insta_outreach.llm import NullLLM
from insta_outreach.personalization.composer import LLMDraft, MessageComposer, keyword_intent
from insta_outreach.personalization.validator import MessageValidator
from tests.conftest import ScriptedLLM

FACTS = {
    "business_name": "The Brew Room",
    "username": "@brew.room",
    "niche": "cafe",
    "location": "Thane",
    "website_status": "no website linked in bio",
    "booking_method": "asks customers to DM to book or enquire",
    "posting_activity": "5 posts in the last 30 days",
}
OK = (
    "Hi The Brew Room team! I'm Rohit from LemmeDeliver. I noticed bookings at your café currently happen "
    "over DM. Would you like to see a quick concept?"
)


def validator() -> MessageValidator:
    return MessageValidator(OfferSettings())


def test_valid_message_passes() -> None:
    assert validator().validate(OK, kind="initial", facts=FACTS) == []


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (OK.replace("Would you", "We have 10 years of experience. Would you"), "number '10'"),
        (OK + " Visit lemmedeliver.com", "contains a link"),
        (OK.replace("I noticed", "Elevate your brand! I noticed"), "banned phrase"),
        (OK.replace("DM.", "DM. Plans start at ₹4999."), "prices"),
        (OK.replace("team!", "{name} team!"), "placeholder"),
        (OK.replace("LemmeDeliver", "our studio"), "does not introduce"),
        (OK.replace("?", "."), "question"),
        (OK + " #websites", "hashtag"),
        ("Hi " + "é" * 600 + " LemmeDeliver?", "byte"),
    ],
)
def test_validator_rejects(text: str, problem: str) -> None:
    problems = validator().validate(text, kind="initial", facts=FACTS)
    assert any(problem in p for p in problems), problems


def test_numbers_from_facts_are_allowed() -> None:
    text = OK.replace("Would you", "You post regularly (5 posts in the last 30 days). Would you")
    assert validator().validate(text, kind="initial", facts=FACTS) == []


def test_near_duplicate_of_recent_message_rejected() -> None:
    problems = validator().validate(OK, kind="initial", facts=FACTS, recent_texts=[OK.replace("Hi", "Hey")])
    assert any("too similar" in p for p in problems)


def composer(llm: Any) -> MessageComposer:
    return MessageComposer(OfferSettings(), llm, validator(), LLMSettings())


@pytest.mark.parametrize("opportunity", list(OpportunityType))
async def test_templates_pass_validation_for_every_opportunity(opportunity: OpportunityType) -> None:
    facts = (
        FACTS | {"website_status": "has a website built on Wix"}
        if opportunity is OpportunityType.WEBSITE_REBUILD
        else FACTS | {"website_status": "the linked website returned HTTP 404 when checked"}
        if opportunity is OpportunityType.BROKEN_WEBSITE
        else FACTS
    )
    message = await composer(NullLLM()).compose_initial(facts=facts, opportunity=(opportunity, "x"), recent_texts=[])
    assert message.composer == "template"
    assert "LemmeDeliver" in message.text and message.text.endswith("?")


async def test_template_never_claims_missing_website_when_site_exists() -> None:
    facts = {"business_name": "Brew", "username": "@brew", "website_status": "has its own website"}
    message = await composer(NullLLM()).compose_initial(facts=facts, opportunity=None, recent_texts=[])
    assert "no website" not in message.text


async def test_llm_draft_retried_with_feedback_then_accepted() -> None:
    drafts = iter(
        [
            LLMDraft(message=OK.replace("?", "? Plans start at ₹4999."), facts_used=["booking_method"]),
            LLMDraft(message=OK, facts_used=["booking_method", "business_name"]),
        ]
    )
    prompts: list[str] = []

    def handler(purpose: str, content: list[dict[str, Any]], model: type) -> LLMDraft:
        prompts.append(content[0]["text"])
        return next(drafts)

    message = await composer(ScriptedLLM(handler)).compose_initial(
        facts=FACTS, opportunity=(OpportunityType.ONLINE_BOOKING, "x"), recent_texts=[]
    )
    assert message.composer == "llm" and message.text == OK
    assert "prices" in str(message.rejected_drafts[0]["problems"])
    assert "rejected for" in prompts[1]  # the retry told the model what was wrong


async def test_llm_citing_unknown_facts_falls_back_to_template() -> None:
    llm = ScriptedLLM(lambda p, c, m: LLMDraft(message=OK, facts_used=["google_rating"]))
    message = await composer(llm).compose_initial(
        facts=FACTS, opportunity=(OpportunityType.NEW_WEBSITE, "x"), recent_texts=[]
    )
    assert message.composer == "template"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Please stop messaging me", ReplyIntent.OPT_OUT),
        ("don't send me these", ReplyIntent.OPT_OUT),
        ("Not interested, thanks", ReplyIntent.NOT_INTERESTED),
        ("no", ReplyIntent.NOT_INTERESTED),
        ("no problem, send details", ReplyIntent.INTERESTED),
        ("How much does it cost?", ReplyIntent.QUESTION),
        ("yes please", ReplyIntent.INTERESTED),
    ],
)
def test_keyword_intent(text: str, intent: ReplyIntent) -> None:
    assert keyword_intent(text) is intent


def test_ambiguous_reply_left_to_classifier() -> None:
    assert keyword_intent("ok") is None
