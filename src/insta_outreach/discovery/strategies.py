"""Pluggable lead-discovery strategies.

A strategy decides *what* to look at (queries, tags, seeds) from campaign
config; the executor (browser lane) only carries out the resulting bounded
operation. Add a strategy by subclassing :class:`DiscoveryStrategy` and
decorating it with ``@register``; enable it per campaign in settings.yaml.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from insta_outreach.config import CampaignSettings, StrategySpec
from insta_outreach.domain.enums import Capability
from insta_outreach.domain.models import CandidateRef, ExecutionResult
from insta_outreach.util.text import try_canonical_username

log = logging.getLogger(__name__)


@dataclass
class DiscoveryTask:
    campaign_id: str
    strategy: str
    capability: Capability
    query_key: str  # stable identity of this run, used for cooldowns
    target_username: str | None = None
    seed: str | None = None  # provenance id when there is no executor target (e.g. a comment id)
    params: dict[str, Any] = field(default_factory=dict)
    hints: dict[str, list[str]] = field(default_factory=dict)
    priority: int = 70


@dataclass
class PlanningContext:
    now: datetime
    recently_run: set[str]  # query keys that ran within their cooldown
    qualified_seeds: list[str]  # qualified lead usernames (for network expansion)


class DiscoveryStrategy(ABC):
    name: ClassVar[str]
    capability: ClassVar[Capability]
    default_cooldown_days: ClassVar[int] = 14

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self.params = spec.params

    def cooldown_days(self) -> int:
        return int(self.params.get("cooldown_days", self.default_cooldown_days))

    @abstractmethod
    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]: ...

    def interpret(self, task: DiscoveryTask, result: ExecutionResult) -> list[CandidateRef]:
        candidates = []
        for raw in result.data.get("candidates", []):
            ref = CandidateRef.model_validate(raw)
            username = try_canonical_username(ref.username)
            if username:
                candidates.append(ref.model_copy(update={"username": username}))
        return candidates

    def _task(self, campaign: CampaignSettings, key: str, **kwargs: Any) -> DiscoveryTask:
        return DiscoveryTask(
            campaign_id=campaign.id, strategy=self.name, capability=self.capability, query_key=key, **kwargs
        )


REGISTRY: dict[str, type[DiscoveryStrategy]] = {}


def register(cls: type[DiscoveryStrategy]) -> type[DiscoveryStrategy]:
    REGISTRY[cls.name] = cls
    return cls


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _fresh(keys: list[tuple[str, Any]], ctx: PlanningContext, limit: int) -> list[tuple[str, Any]]:
    return [(k, v) for k, v in keys if k not in ctx.recently_run][:limit]


@register
class KeywordSearch(DiscoveryStrategy):
    """Instagram search for '<niche> <location>' (e.g. 'dentist Thane')."""

    name = "keyword_search"
    capability = Capability.SEARCH_ACCOUNTS

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        queries: list[tuple[str, tuple[str | None, str | None]]] = [
            (f"{niche} {location}", (niche, location)) for niche in campaign.niches for location in campaign.locations
        ]
        queries += [(q, (None, None)) for q in self.params.get("extra_queries", [])]
        keyed = [(f"{self.name}|{q.lower()}", (q, meta)) for q, meta in queries]
        tasks = []
        for key, (query, (niche, location)) in _fresh(keyed, ctx, int(self.params.get("max_queries_per_run", 3))):
            tasks.append(
                self._task(
                    campaign,
                    key,
                    params={"query": query, "limit": int(self.params.get("max_results", 20))},
                    hints={"query_locations": [location] if location else [], "query_niches": [niche] if niche else []},
                )
            )
        return tasks


@register
class HashtagDiscovery(DiscoveryStrategy):
    """Top posts for niche/location hashtags; post owners become candidates."""

    name = "hashtag"
    capability = Capability.HASHTAG_POSTS

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        tags = [t.lstrip("#").lower() for t in self.params.get("tags", [])]
        if not tags:
            tags = [f"{_slug(location)}{_slug(niche)}" for location in campaign.locations for niche in campaign.niches]
        keyed = [(f"{self.name}|{tag}", tag) for tag in tags]
        return [
            self._task(campaign, key, params={"tag": tag, "limit": int(self.params.get("max_posts", 9))})
            for key, tag in _fresh(keyed, ctx, int(self.params.get("tags_per_run", 2)))
        ]


@register
class LocationDiscovery(DiscoveryStrategy):
    """Recent/top posts on configured Instagram location pages."""

    name = "location"
    capability = Capability.LOCATION_POSTS

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        keyed = []
        for loc in self.params.get("locations", []):
            entry = loc if isinstance(loc, dict) else {"name": str(loc)}
            keyed.append((f"{self.name}|{entry.get('url') or entry['name']}".lower(), entry))
        return [
            self._task(
                campaign,
                key,
                params={
                    "location": entry.get("name"),
                    "location_url": entry.get("url"),
                    "limit": int(self.params.get("max_posts", 9)),
                },
                hints={"query_locations": [entry.get("name", "")]},
            )
            for key, entry in _fresh(keyed, ctx, int(self.params.get("locations_per_run", 1)))
        ]


@register
class FollowersOf(DiscoveryStrategy):
    """Followers of relevant seed accounts (e.g. a local business association)."""

    name = "followers_of"
    capability = Capability.LIST_FOLLOWERS
    default_cooldown_days = 30

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        seeds = [s for s in (try_canonical_username(x) for x in self.params.get("seeds", [])) if s]
        keyed = [(f"{self.name}|{seed}", seed) for seed in seeds]
        return [
            self._task(campaign, key, target_username=seed, params={"limit": int(self.params.get("max_results", 40))})
            for key, seed in _fresh(keyed, ctx, int(self.params.get("seeds_per_run", 1)))
        ]


@register
class FollowingOf(FollowersOf):
    """Accounts that relevant seeds follow (suppliers, peers, partners)."""

    name = "following_of"
    capability = Capability.LIST_FOLLOWING


@register
class SuggestedAccounts(DiscoveryStrategy):
    """'Similar accounts' of businesses we already qualified: niche expansion."""

    name = "suggested_accounts"
    capability = Capability.SUGGESTED_ACCOUNTS
    default_cooldown_days = 60

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        explicit = [s for s in (try_canonical_username(x) for x in self.params.get("seeds", [])) if s]
        seeds = explicit + [s for s in ctx.qualified_seeds if s not in explicit]
        keyed = [(f"{self.name}|{seed}", seed) for seed in seeds]
        return [
            self._task(campaign, key, target_username=seed, params={"limit": int(self.params.get("max_results", 15))})
            for key, seed in _fresh(keyed, ctx, int(self.params.get("seeds_per_run", 2)))
        ]


@register
class PostEngagers(DiscoveryStrategy):
    """Accounts engaging (commenting) on niche hub posts."""

    name = "post_engagers"
    capability = Capability.POST_ENGAGERS

    def plan(self, campaign: CampaignSettings, ctx: PlanningContext) -> list[DiscoveryTask]:
        keyed = [(f"{self.name}|{url.rstrip('/')}", url) for url in self.params.get("posts", [])]
        return [
            self._task(campaign, key, params={"post_url": url, "limit": int(self.params.get("max_results", 30))})
            for key, url in _fresh(keyed, ctx, int(self.params.get("posts_per_run", 1)))
        ]


def build_strategies(campaign: CampaignSettings) -> list[DiscoveryStrategy]:
    strategies = []
    for spec in campaign.strategies:
        if not spec.enabled:
            continue
        cls = REGISTRY.get(spec.name)
        if cls is None:
            log.warning(
                "campaign %s: unknown discovery strategy %r (known: %s)", campaign.id, spec.name, sorted(REGISTRY)
            )
            continue
        strategies.append(cls(spec))
    return strategies
