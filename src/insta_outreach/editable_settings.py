"""The settings Rohit changes on Mission Control's Settings and Campaigns pages.

Mission Control never rewrites ``config/settings.yaml`` (its comments stay).
It writes only the fields in :data:`EDITABLE` to a small file next to it,
``settings.dashboard.yaml``, applied on top of settings.yaml whenever settings
load; environment variables still win over both. Every change is validated
through the full settings model before anything is written, then applied to
the running program at once, or, for fields marked ``restart`` (they shape
how the program is built: which Instagram, which browser accounts), when the
program restarts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from insta_outreach.config import Settings, compose_settings, overlay_path, read_overlay
from insta_outreach.discovery.strategies import REGISTRY
from insta_outreach.domain.enums import Environment
from insta_outreach.util.text import InvalidUsername, canonical_username


class SettingsError(ValueError):
    """A change that cannot be saved; the message is shown to Rohit as is."""


@dataclass(frozen=True)
class Editable:
    label: str
    restart: bool = False  # applies after a restart


EDITABLE: dict[str, Editable] = {
    "environment": Editable("Real Instagram or the simulation", restart=True),
    "browser.enabled": Editable("Use the browser on Instagram", restart=True),
    "browser.headless": Editable("Hide the browser window while it works", restart=True),
    "research.enabled": Editable("Separate research account", restart=True),
    "research.account.username": Editable("Research account username", restart=True),
    "rollout.allowed_targets": Editable("Test mode: only message these accounts"),
    "schedule.send_hours": Editable("Sending hours"),
    "schedule.browser_active_hours": Editable("Browser hours"),
    "scoring.min_score_to_contact": Editable("Lowest score worth a message"),
    "offer.sender_name": Editable("Your name in messages"),
    "offer.website_url": Editable("Your website"),
    "offer.include_link_in_first_message": Editable("Website link in the first message"),
    "notifications.min_severity": Editable("Which problems reach your phone"),
    "notifications.dashboard_url": Editable("Mission Control link in alerts"),
    "campaigns": Editable("Who to find"),
}
NOTIFICATION_KEYS = frozenset(k for k in EDITABLE if k.startswith("notifications."))

_HOUR = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CAMPAIGN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def get_path(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _set_path(obj: Any, dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, leaf, value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value"):  # enums
        return value.value
    return value


def _handle(raw: Any, what: str) -> str:
    try:
        return canonical_username(str(raw))
    except InvalidUsername as exc:
        raise SettingsError(f"{what}: {exc}") from None


def _handles(raw: Any, what: str) -> list[str]:
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw or [])
    seen: list[str] = []
    for item in items:
        if str(item).strip():
            handle = _handle(item, what)
            if handle not in seen:
                seen.append(handle)
    return seen


def _hours(raw: Any, what: str) -> list[str]:
    if not isinstance(raw, list | tuple) or len(raw) != 2 or not all(_HOUR.match(str(h)) for h in raw):
        raise SettingsError(f"{what}: give a start and an end time like 10:00 and 20:00")
    if raw[0] == raw[1]:
        raise SettingsError(f"{what}: the start and end time must differ")
    return [str(raw[0]), str(raw[1])]


def _text(raw: Any, what: str, limit: int) -> str:
    text = " ".join(str(raw or "").split())
    if not text or len(text) > limit:
        raise SettingsError(f"{what}: 1 to {limit} characters")
    return text


def _url(raw: Any, what: str, optional: bool) -> str | None:
    text = str(raw or "").strip()
    if not text and optional:
        return None
    if not _URL.match(text):
        raise SettingsError(f"{what}: a full link starting with https://")
    return text


def _words(raw: Any) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    words: list[str] = []
    for item in items:
        word = " ".join(str(item).split())
        if word and word.lower() not in (w.lower() for w in words):
            words.append(word)
    return words


# How each way of finding businesses is shown on the Campaigns page, and the
# one list it takes from Rohit (its other parameters keep their configured values).
STRATEGY_INFO: list[dict[str, str]] = [
    {
        "name": "keyword_search",
        "label": "Instagram search",
        "help": "Searches Instagram for each kind of business in each place, e.g. 'dentist Thane'.",
        "list": "extra_queries",
        "list_label": "Extra searches (optional)",
        "list_kind": "text",
    },
    {
        "name": "hashtag",
        "label": "Hashtags",
        "help": "Top posts for hashtags like #thanedentist, made from your kinds and places unless you list your own.",
        "list": "tags",
        "list_label": "Your own hashtags (optional)",
        "list_kind": "tag",
    },
    {
        "name": "suggested_accounts",
        "label": "Similar accounts",
        "help": "Instagram's 'similar accounts' of businesses that were a good fit.",
        "list": "seeds",
        "list_label": "Also start from these accounts (optional)",
        "list_kind": "handle",
    },
    {
        "name": "location",
        "label": "Location pages",
        "help": "Recent posts on Instagram location pages you choose.",
        "list": "locations",
        "list_label": "Location pages, one per line: name | link",
        "list_kind": "location",
    },
    {
        "name": "followers_of",
        "label": "Followers of an account",
        "help": "Businesses following an account you name, e.g. a local business group.",
        "list": "seeds",
        "list_label": "Accounts",
        "list_kind": "handle",
    },
    {
        "name": "following_of",
        "label": "Accounts an account follows",
        "help": "Businesses that a well-connected local account follows.",
        "list": "seeds",
        "list_label": "Accounts",
        "list_kind": "handle",
    },
    {
        "name": "post_engagers",
        "label": "People commenting on a post",
        "help": "Businesses commenting on popular local posts you name.",
        "list": "posts",
        "list_label": "Post links",
        "list_kind": "url",
    },
]
_LIST_PARAM = {info["name"]: (info["list"], info["list_kind"]) for info in STRATEGY_INFO}
_IG_LINK = re.compile(r"^https://(www\.)?instagram\.com/\S+$", re.IGNORECASE)


def _ig_link(raw: Any, what: str) -> str:
    link = str(raw or "").strip()
    if not _IG_LINK.match(link):
        raise SettingsError(f"{what}: an https://www.instagram.com/... link")
    return link


def _strategy_list(name: str, raw: Any) -> list[Any]:
    """The one list a strategy takes from the page, cleaned like its planner expects it."""
    _, kind = _LIST_PARAM[name]
    items = [line for line in (raw.splitlines() if isinstance(raw, str) else list(raw or [])) if str(line).strip()]
    if kind == "handle":
        return _handles(items, "accounts")
    if kind == "tag":
        return _words(re.sub(r"[^a-z0-9_]", "", str(t).lower().lstrip("#")) for t in items)
    if kind == "url":
        return [_ig_link(item, "post links") for item in items]
    if kind == "location":
        places = []
        for item in items:
            if isinstance(item, dict):
                name_, link = str(item.get("name", "")).strip(), str(item.get("url") or "").strip()
            else:
                name_, _, link = (part.strip() for part in str(item).partition("|"))
            if not name_:
                raise SettingsError("location pages: give each a name")
            places.append({"name": name_} | ({"url": _ig_link(link, "location pages")} if link else {}))
        return places
    return _words(items)


def _campaigns(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SettingsError("campaigns: a list")
    seen: set[str] = set()
    cleaned = []
    for item in raw:
        if not isinstance(item, dict):
            raise SettingsError("campaigns: each campaign is a set of fields")
        campaign = dict(item)
        cid = str(campaign.get("id", "")).strip().lower()
        if not _CAMPAIGN_ID.match(cid):
            raise SettingsError(f"campaign name {cid!r}: lowercase letters, digits and dashes, e.g. thane-salons")
        if cid in seen:
            raise SettingsError(f"two campaigns are called {cid!r}")
        seen.add(cid)
        campaign["id"] = cid
        campaign["niches"], campaign["locations"] = _words(campaign.get("niches")), _words(campaign.get("locations"))
        if campaign.get("enabled", True) and not (campaign["niches"] and campaign["locations"]):
            raise SettingsError(f"campaign {cid}: needs at least one kind of business and one place")
        strategies = []
        for spec in campaign.get("strategies") or []:
            name = str((spec or {}).get("name", ""))
            if name not in REGISTRY:
                raise SettingsError(f"campaign {cid}: unknown way to find businesses {name!r}")
            spec = dict(spec)
            params = dict(spec.get("params") or {})
            key = _LIST_PARAM.get(name, ("", ""))[0]
            if key and key in params:
                params[key] = _strategy_list(name, params[key])
            spec["params"] = params
            strategies.append(spec)
        campaign["strategies"] = strategies
        if campaign.get("min_score") in ("", None):
            campaign["min_score"] = None
        else:
            campaign["min_score"] = min(100, max(0, int(campaign["min_score"])))
        cleaned.append(campaign)
    return cleaned


# Turns what the page sent into the value settings.yaml would hold.
_NORMALIZE: dict[str, Callable[[Any], Any]] = {
    "environment": lambda v: Environment(str(v).lower()).value,
    "browser.enabled": bool,
    "browser.headless": bool,
    "research.enabled": bool,
    "research.account.username": lambda v: _handle(v, "research account") if str(v or "").strip() else "",
    "rollout.allowed_targets": lambda v: _handles(v, "test accounts"),
    "schedule.send_hours": lambda v: _hours(v, "sending hours"),
    "schedule.browser_active_hours": lambda v: _hours(v, "browser hours"),
    "scoring.min_score_to_contact": lambda v: min(100, max(0, int(v))),
    "offer.sender_name": lambda v: _text(v, "your name", 40),
    "offer.website_url": lambda v: _url(v, "your website", optional=False),
    "offer.include_link_in_first_message": bool,
    "notifications.min_severity": lambda v: str(v).upper(),
    "notifications.dashboard_url": lambda v: _url(v, "Mission Control link", optional=True),
    "campaigns": _campaigns,
}


def _put(tree: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    node = tree
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[leaf] = value


def _drop(tree: dict[str, Any], dotted: str) -> None:
    *parents, leaf = dotted.split(".")
    chain = [tree]
    for part in parents:
        child = chain[-1].get(part)
        if not isinstance(child, dict):
            return
        chain.append(child)
    chain[-1].pop(leaf, None)
    for node, part in zip(reversed(chain[:-1]), reversed(parents), strict=True):  # prune empty sections
        if not node[part]:
            del node[part]


def _has(tree: dict[str, Any], dotted: str) -> bool:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _readable(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid value')}"


class SettingsEditor:
    """Reads and changes the editable settings of the running program."""

    def __init__(self, settings: Settings, on_notifications_changed: Callable[[], None] | None = None) -> None:
        self._settings = settings  # the running program's settings object, shared by every service
        self._on_notifications_changed = on_notifications_changed

    @property
    def config_path(self) -> Path | None:
        return self._settings.config_path

    @property
    def can_edit(self) -> bool:
        return self.config_path is not None

    def saved(self) -> Settings:
        """What the files (plus environment variables) say now."""
        if self.config_path is None:
            return self._settings
        return compose_settings(self.config_path)

    def pending_restart(self) -> list[str]:
        saved = self.saved()
        return [
            key
            for key, spec in EDITABLE.items()
            if spec.restart and _jsonable(get_path(saved, key)) != _jsonable(get_path(self._settings, key))
        ]

    def view(self) -> dict[str, Any]:
        saved = self.saved()
        overlay = read_overlay(self.config_path) if self.config_path else {}
        return {
            "values": {key: _jsonable(get_path(saved, key)) for key in EDITABLE},
            "labels": {key: spec.label for key, spec in EDITABLE.items()},
            "restart_keys": [key for key, spec in EDITABLE.items() if spec.restart],
            "changed_here": [key for key in EDITABLE if _has(overlay, key)],
            "pending_restart": self.pending_restart(),
            "can_edit": self.can_edit,
            "why_not": None
            if self.can_edit
            else "This is the demo: its settings are fixed. Start the program itself to change them.",
            "files": {
                "config": str(self.config_path) if self.config_path else None,
                "overlay": str(overlay_path(self.config_path)) if self.config_path else None,
            },
        }

    def apply(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate, save and apply ``changes`` ({dotted key: value}; None puts the
        settings.yaml value back). Returns what was applied now and what waits for
        a restart."""
        if self.config_path is None:
            raise SettingsError("This is the demo: its settings are fixed. Start the program itself to change them.")
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise SettingsError(f"these settings cannot be changed here: {', '.join(unknown)}")
        overlay = read_overlay(self.config_path)
        for key, raw in changes.items():
            if raw is None:
                _drop(overlay, key)
                continue
            try:
                _put(overlay, key, _NORMALIZE[key](raw))
            except (TypeError, ValueError) as exc:
                raise SettingsError(str(exc) if isinstance(exc, SettingsError) else f"{key}: {exc}") from None
        try:
            fresh = compose_settings(self.config_path, overlay=overlay)
        except ValidationError as exc:
            raise SettingsError(_readable(exc)) from None
        if fresh.environment is Environment.LIVE and fresh.control_api.token is None:
            raise SettingsError(
                "Real Instagram needs a CONTROL_API_TOKEN in .env first (insta-outreach setup creates one)"
            )
        research = fresh.research
        if research.enabled and not research.account.username:
            raise SettingsError("Give the research account's username before turning it on")
        if research.enabled and research.account.username == fresh.account.username.lower():
            raise SettingsError("The research account must be a different account from @" + fresh.account.username)
        self._write(overlay)
        applied = []
        for key in changes:
            if not EDITABLE[key].restart:
                _set_path(self._settings, key, get_path(fresh, key))
                applied.append(key)
        if NOTIFICATION_KEYS & set(changes) and self._on_notifications_changed is not None:
            self._on_notifications_changed()
        return {"applied": applied, "restart_required": self.pending_restart()}

    def _write(self, overlay: dict[str, Any]) -> None:
        assert self.config_path is not None
        target = overlay_path(self.config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Written by Mission Control (Settings and Campaigns pages); applied on top of\n"
            f"# {self.config_path.name}. Delete this file to use {self.config_path.name} alone.\n"
        )
        body = yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True) if overlay else ""
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(header + body, encoding="utf-8")
        os.replace(temp, target)
