"""Everything the browser agent knows about Instagram's web UI, as data.

When Instagram changes its UI, this is the only place to update (or drop a
YAML override at ``browser.ui_map_path``), and the constrained LLM resolver
covers the gap in the meantime. Locators prefer accessible roles/names and
stable URL shapes over Instagram's obfuscated CSS classes.

Detector phrases target the English UI; keep the account language English
(``browser.locale`` is pinned to en-US for the same reason).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

LocatorKind = Literal["role", "css", "text", "label", "placeholder"]


@dataclass(frozen=True)
class LocatorSpec:
    kind: LocatorKind
    value: str  # role name, CSS selector, text, label or placeholder
    name: str | None = None  # accessible name for role locators; "re:" prefix = regex
    exact: bool = False
    within: str | None = None  # optional CSS scope


@dataclass(frozen=True)
class IntentSpec:
    name: str
    description: str  # what the element is, for the LLM resolver
    locators: tuple[LocatorSpec, ...]
    accept_roles: tuple[str, ...]  # roles/tags an LLM-picked element may have
    name_pattern: str | None = None  # regex an LLM-picked element's name must match
    editable: bool = False  # must be a text input / contenteditable


@dataclass(frozen=True)
class DetectorRule:
    state: str  # login_required | checkpoint | rate_limited | restricted | not_found | private
    #             | cannot_message | request_pending | human_action
    code: str
    url_patterns: tuple[str, ...] = ()
    dialog_text: tuple[str, ...] = ()  # regexes matched against open dialogs' text
    page_text: tuple[str, ...] = ()  # regexes matched against the page when markers are missing
    selectors: tuple[str, ...] = ()  # any visible match triggers the rule


@dataclass(frozen=True)
class BenignDialog:
    text: str  # regex on dialog text
    button: str  # exact accessible name of the ONLY button the agent may press


@dataclass
class UiMap:
    urls: dict[str, str]
    intents: dict[str, IntentSpec]
    detectors: list[DetectorRule]
    benign_dialogs: list[BenignDialog]
    page_markers: dict[str, list[str]]
    deny_click_names: str
    dismiss_labels: tuple[str, ...] = (
        "Not Now",
        "Not now",
        "Cancel",
        "Close",
        "OK",
        "Dismiss",
        "Decline optional cookies",
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def url(self, key: str, base_url: str, **values: str) -> str:
        return base_url.rstrip("/") + self.urls[key].format(**values)


def _r(role: str, name: str | None = None, exact: bool = False, within: str | None = None) -> LocatorSpec:
    return LocatorSpec("role", role, name, exact, within)


def _c(css: str, within: str | None = None) -> LocatorSpec:
    return LocatorSpec("css", css, within=within)


DEFAULT_INTENTS: dict[str, IntentSpec] = {
    spec.name: spec
    for spec in (
        IntentSpec(
            "nav.search",
            "the Search button/link in the left navigation that opens the search panel",
            (_r("link", "Search", exact=True), _r("button", "Search", exact=True), _c("svg[aria-label='Search']")),
            ("link", "button", "a", "div", "span", "svg"),
            r"(?i)search|explore",
        ),
        IntentSpec(
            "search.input",
            "the text input where you type an account search query",
            (_r("textbox", "re:(?i)search"), _c("input[aria-label='Search input']"), _c("input[placeholder='Search']")),
            ("textbox", "input", "searchbox"),
            r"(?i)search|find|explore|people",
            editable=True,
        ),
        IntentSpec(
            "profile.message_button",
            "the 'Message' button on the profile header that opens a direct message thread",
            (
                _r("button", "Message", exact=True, within="header"),
                _r("button", "Message", exact=True),
                _r("link", "Message", exact=True),
                _c("header div[role='button']:text-is('Message')"),
            ),
            ("button", "link", "a", "div"),
            r"(?i)message|chat|\bdm\b|direct",
        ),
        IntentSpec(
            "profile.options_button",
            "the '...' options button on the profile header",
            (_r("button", "re:(?i)^options$", within="header"), _c("header svg[aria-label='Options']")),
            ("button", "div", "svg"),
            r"(?i)options",
        ),
        IntentSpec(
            "menu.send_message",
            "the 'Send message' entry in the profile options menu",
            (_r("button", "re:(?i)^send message$"), _r("menuitem", "re:(?i)^send message$")),
            ("button", "menuitem", "div"),
            r"(?i)^send message$",
        ),
        IntentSpec(
            "profile.followers_link",
            "the link showing the follower count that opens the followers list",
            (_c("header a[href$='/followers/']"), _c("a[href$='/followers/']")),
            ("link", "a"),
            r"(?i)followers",
        ),
        IntentSpec(
            "profile.following_link",
            "the link showing the following count that opens the following list",
            (_c("header a[href$='/following/']"), _c("a[href$='/following/']")),
            ("link", "a"),
            r"(?i)following",
        ),
        IntentSpec(
            "profile.similar_accounts",
            "the button that reveals similar/suggested accounts on a profile",
            (
                _r("button", "re:(?i)similar accounts"),
                _c("header svg[aria-label='Similar accounts']"),
                _r("button", "re:(?i)suggested"),
            ),
            ("button", "div", "svg"),
            r"(?i)similar|suggest",
        ),
        IntentSpec(
            "thread.composer",
            "the text box for writing a direct message in the open conversation",
            (
                _r("textbox", "re:(?i)^message"),
                _c("div[contenteditable='true'][role='textbox']"),
                _c("textarea[placeholder^='Message']"),
            ),
            ("textbox", "textarea", "div"),
            None,
            editable=True,
        ),
        IntentSpec(
            "thread.send",
            "the Send button next to the message composer",
            (_r("button", "Send", exact=True), _c("div[role='button']:text-is('Send')")),
            ("button", "div"),
            r"(?i)^(send|submit|post)$",
        ),
    )
}

DEFAULT_DETECTORS: list[DetectorRule] = [
    DetectorRule(
        "restricted",
        "account_suspended",
        url_patterns=(r"/accounts/suspended", r"/accounts/disabled"),
        dialog_text=(
            r"we restrict certain activity to protect our community",
            r"action blocked",
            r"your account has been (?:disabled|suspended)",
            r"we suspended your account",
            r"account (?:is|has been) temporarily (?:locked|restricted)",
            r"you can'?t (?:send messages|message people|use this feature) (?:right now|for now|at this time)",
        ),
        page_text=(r"your account has been (?:disabled|suspended)", r"we suspended your account"),
    ),
    DetectorRule(
        "checkpoint",
        "security_checkpoint",
        url_patterns=(r"/challenge/", r"/auth_platform/", r"/accounts/login/two_factor"),
        dialog_text=(
            r"confirm it'?s you",
            r"help us confirm",
            r"suspicious login attempt",
            r"unusual login attempt",
            r"verify your (?:account|identity)",
        ),
        page_text=(
            r"confirm it'?s you",
            r"help us confirm (?:it'?s you|you own)",
            r"suspicious login attempt",
            r"we detected an unusual login attempt",
            r"enter (?:the |your )?(?:security|confirmation) code",
            r"complete (?:the|a) security check",
            r"verify your (?:account|identity)",
        ),
        selectors=(
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='arkoselabs']",
            "iframe[title*='captcha' i]",
        ),
    ),
    DetectorRule(
        "login_required",
        "login_page",
        url_patterns=(r"/accounts/login",),
        selectors=("input[name='password']",),
    ),
    DetectorRule(
        "rate_limited",
        "try_again_later",
        dialog_text=(
            r"try again later",
            r"please wait a few minutes before you try again",
            r"we limit how often you can do certain things",
            r"you'?ve (?:reached|hit) (?:the|a) limit",
            r"too many (?:requests|attempts)",
        ),
        page_text=(r"please wait a few minutes before you try again",),
    ),
    DetectorRule(
        "request_pending",
        "message_request_pending",
        dialog_text=(
            r"(?:send|sending) more messages (?:until|once|after)",
            r"until .{0,40}accepts? your",
            r"invite (?:is|has been) accepted",
        ),
        page_text=(r"you can send more messages (?:after|once|when) .{0,60}accept", r"can'?t send more messages until"),
    ),
    DetectorRule(
        "cannot_message",
        "cannot_message",
        dialog_text=(
            r"can'?t receive (?:your )?messages",
            r"you can'?t message this account",
            r"isn'?t receiving messages",
        ),
        page_text=(
            r"(?:this account|this person|they) can'?t receive (?:your )?messages",
            r"you can'?t message this account",
            r"isn'?t receiving messages from you",
        ),
    ),
    DetectorRule(
        "not_found",
        "page_unavailable",
        page_text=(
            r"sorry, this page isn'?t available",
            r"the link you followed may be broken",
            r"profile isn'?t available",
            r"user not found",
        ),
    ),
    DetectorRule("private", "private_account", page_text=(r"this account is private",)),
]

DEFAULT_BENIGN_DIALOGS: list[BenignDialog] = [
    BenignDialog(r"turn on notifications", "Not Now"),
    BenignDialog(r"save (?:your )?login info", "Not now"),
    BenignDialog(r"add instagram to your home screen", "Cancel"),
    BenignDialog(r"allow the use of cookies", "Decline optional cookies"),
]

DEFAULT_PAGE_MARKERS: dict[str, list[str]] = {
    "home": ["nav", "a[href='/direct/inbox/']", "svg[aria-label='Home']"],
    "profile": ["header"],
    "post": ["article", "time[datetime]", "main"],
    "tag": ["a[href*='/p/']", "a[href*='/reel/']"],
    "inbox": [
        "a[href^='/direct/t/']",
        "div[role='listitem']",
        "[aria-label*='Inbox' i]",
        "[aria-label*='Chats' i]",
        "main",
    ],
    "thread": ["div[role='textbox']", "textarea"],
    "dialog": ["div[role='dialog']"],
}

DEFAULT_URLS = {
    "home": "/",
    "login": "/accounts/login/",
    "profile": "/{username}/",
    "followers": "/{username}/followers/",
    "following": "/{username}/following/",
    "post": "/p/{shortcode}/",
    "tag": "/explore/tags/{tag}/",
    "inbox": "/direct/inbox/",
    "thread": "/direct/t/{thread_id}/",
}

# Accessible names the agent must never click, whatever a locator or the LLM says.
DENY_CLICK_NAMES = (
    r"(?i)^(?:un)?follow(?:ing)?\b|\bblock\b|\breport\b|\bdelete\b|\bremove\b|\bunsend\b|"
    r"\blog ?out\b|\bsign ?out\b|\blike\b|\bunlike\b|\brestrict\b|\bmute\b|\bpay\b|\bbuy\b|"
    r"\bsubscribe\b|\bdeactivate\b|\bchange password\b|\bconfirm\b|\bsend code\b|\bverify\b"
)


def default_ui_map() -> UiMap:
    return UiMap(
        urls=dict(DEFAULT_URLS),
        intents=dict(DEFAULT_INTENTS),
        detectors=list(DEFAULT_DETECTORS),
        benign_dialogs=list(DEFAULT_BENIGN_DIALOGS),
        page_markers={k: list(v) for k, v in DEFAULT_PAGE_MARKERS.items()},
        deny_click_names=DENY_CLICK_NAMES,
    )


def _locator(raw: dict[str, Any]) -> LocatorSpec:
    return LocatorSpec(
        kind=raw["kind"],
        value=raw["value"],
        name=raw.get("name"),
        exact=bool(raw.get("exact", False)),
        within=raw.get("within"),
    )


def load_ui_map(path: Path | None) -> UiMap:
    """Defaults, optionally patched by a YAML file:

    urls: {profile: "/{username}/"}
    intents: {thread.composer: {locators: [{kind: role, value: textbox, name: "re:(?i)message"}]}}
    detectors: [{state: rate_limited, code: new_dialog, dialog_text: ["slow down"]}]
    benign_dialogs: [{text: "new feature", button: "OK"}]
    page_markers: {thread: ["div[role='textbox']"]}
    """
    ui = default_ui_map()
    if path is None or not Path(path).exists():
        return ui
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    ui.urls.update(data.get("urls") or {})
    for name, patch in (data.get("intents") or {}).items():
        base = ui.intents.get(name)
        locators = tuple(_locator(x) for x in patch.get("locators", []))
        if base is None:
            ui.intents[name] = IntentSpec(
                name,
                patch.get("description", name),
                locators,
                tuple(patch.get("accept_roles", ("button", "link"))),
                patch.get("name_pattern"),
                bool(patch.get("editable", False)),
            )
        else:
            ui.intents[name] = replace(base, locators=locators + base.locators if locators else base.locators)
    extra_rules = [
        DetectorRule(
            state=r["state"],
            code=r["code"],
            url_patterns=tuple(r.get("url_patterns", [])),
            dialog_text=tuple(r.get("dialog_text", [])),
            page_text=tuple(r.get("page_text", [])),
            selectors=tuple(r.get("selectors", [])),
        )
        for r in data.get("detectors") or []
    ]
    ui.detectors = extra_rules + ui.detectors
    ui.benign_dialogs += [BenignDialog(d["text"], d["button"]) for d in data.get("benign_dialogs") or []]
    for kind, markers in (data.get("page_markers") or {}).items():
        ui.page_markers[kind] = list(markers) + ui.page_markers.get(kind, [])
    return ui
