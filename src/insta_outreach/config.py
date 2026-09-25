"""Settings: YAML file + environment overrides, validated by pydantic.

Static configuration lives here. The *runtime* knobs Rohit changes while the
system runs (operating mode, limit overrides) live in the database and are
read through :class:`insta_outreach.runtime.RuntimeControl`, so switching
OBSERVE -> AUTONOMOUS never needs a restart or a different code path.

Environment overrides:
  * ``INSTA_OUTREACH_CONFIG``   path of the YAML file (default config/settings.yaml)
  * ``IG_ACCESS_TOKEN``, ``IG_APP_SECRET``, ``IG_WEBHOOK_VERIFY_TOKEN``,
    ``CONTROL_API_TOKEN``, ``NOTIFY_WEBHOOK_URL``  common secrets
  * ``INSTA__SECTION__KEY=value``  any nested key, value parsed as YAML
    (e.g. ``INSTA__LIMITS__OUTREACH_PER_DAY=10``)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator

from insta_outreach.domain.enums import Environment, IncidentSeverity, OperatingMode

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AccountSettings(BaseModel):
    """Our own Instagram account (the one Rohit also uses manually)."""

    id: str = "lemmedeliver"
    username: str = "lemmedeliver"


class ApiSettings(BaseModel):
    """Tier 1: official Meta Instagram Platform APIs.

    Capabilities differ by login type (see docs/RESEARCH.md):
      * both: replies inside the 24h window, private replies to comments,
        comment replies, conversations read, user profile of people who
        messaged us, webhooks (messages, echoes, comments...)
      * facebook login only: Business Discovery (profile enrichment by
        username) - requires a Page-linked account + Advanced Access.
    No login type allows starting a conversation with someone who has not
    messaged the account first.
    """

    enabled: bool = False
    # "instagram": Instagram API with Instagram Login (graph.instagram.com)
    # "facebook":  Instagram API with Facebook Login (graph.facebook.com)
    login_type: Literal["instagram", "facebook"] = "instagram"
    graph_version: str = "v26.0"
    base_url: str | None = None  # derived from login_type when unset
    access_token: SecretStr | None = None
    ig_user_id: str | None = None  # our professional account's IG user id
    app_secret: SecretStr | None = None  # verifies webhook signatures
    webhook_verify_token: SecretStr | None = None
    timeout_seconds: float = 20.0
    use_business_discovery: bool = True  # only effective with login_type=facebook

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.access_token and self.ig_user_id)

    @property
    def business_discovery_available(self) -> bool:
        return self.configured and self.use_business_discovery and self.login_type == "facebook"

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        host = "graph.instagram.com" if self.login_type == "instagram" else "graph.facebook.com"
        return f"https://{host}/{self.graph_version}"


class BrowserSettings(BaseModel):
    """Tier 2: Playwright browser agent driving the normal Instagram web UI."""

    enabled: bool = False
    base_url: str = "https://www.instagram.com"
    # Hard navigation allowlist: the agent refuses to load any other host.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["www.instagram.com", "instagram.com"])
    profiles_dir: Path = Path("data/browser_profiles")
    evidence_dir: Path = Path("data/evidence")
    headless: bool = True
    channel: str | None = None  # e.g. "chrome" to drive an installed Google Chrome
    executable_path: str | None = None
    locale: str = "en-US"  # detectors match English UI strings
    timezone_id: str = "Asia/Kolkata"
    viewport_width: int = 1280
    viewport_height: int = 900
    navigation_timeout_ms: int = 30_000
    action_timeout_ms: int = 8_000
    typing_delay_ms: int = 35
    screenshot_on_success: bool = True
    trace_on_failure: bool = True
    llm_fallback: bool = True  # constrained LLM for page-state + element resolution
    max_operation_retries: int = 1
    ui_map_path: Path | None = None  # optional YAML overriding selectors/detector text


class LLMSettings(BaseModel):
    enabled: bool = True  # still requires Anthropic credentials to be present
    model: str = "claude-opus-5"
    vision_model: str = "claude-opus-5"
    personalization_effort: Effort = "high"
    vision_effort: Effort = "low"
    use_refusal_fallbacks: bool = True
    max_calls_per_hour: int = 200
    timeout_seconds: float = 90.0


class LimitsSettings(BaseModel):
    """Configurable safeguards. Conservative by default: one shared account."""

    outreach_per_day: int = 15
    outreach_per_hour: int = 4
    followups_per_day: int = 10
    replies_per_hour: int = 20
    browser_units_per_hour: int = 120  # page views/steps across all browser operations
    api_calls_per_hour: int = 150
    profile_inspections_per_day: int = 250
    discovery_runs_per_day: int = 12
    min_seconds_between_sends: int = 240
    send_jitter_seconds: int = 180
    min_seconds_between_browser_units: float = 6.0
    browser_unit_jitter_seconds: float = 6.0
    max_concurrent_browser_sessions: int = 1
    max_concurrent_api_calls: int = 4
    max_followups_per_lead: int = 2
    followup_after_days: list[int] = Field(default_factory=lambda: [3, 7])
    rate_limit_cooldown_minutes: int = 24 * 60
    rate_limit_events_before_halt: int = 2  # within 24h
    ui_changed_events_before_halt: int = 2  # consecutive
    retry_max_attempts: int = 3
    retry_base_seconds: int = 120
    approval_ttl_hours: int = 72
    max_units_per_operation: int = 25

    @field_validator("followup_after_days")
    @classmethod
    def _sorted_days(cls, value: list[int]) -> list[int]:
        return sorted(value)


class ScheduleSettings(BaseModel):
    timezone: str = "Asia/Kolkata"
    browser_active_hours: tuple[str, str] = ("09:30", "21:30")
    send_hours: tuple[str, str] = ("10:00", "20:00")
    tick_seconds: float = 20.0
    discovery_interval_minutes: int = 90
    inbox_sync_interval_minutes: int = 30
    max_actions_per_tick: int = 5


class OfferSettings(BaseModel):
    """What LemmeDeliver sells, used for opportunity framing and messages."""

    brand: str = "LemmeDeliver"
    sender_name: str = "Rohit"
    website_url: str = "https://lemmedeliver.com"
    pitch: str = (
        "LemmeDeliver designs and builds premium, animated websites for local businesses, "
        "with online booking wired in, delivered in days rather than weeks."
    )
    services: list[str] = Field(
        default_factory=lambda: [
            "a custom animated website designed for that one business",
            "online booking wired to the tools the business already uses (e.g. Cal.com, Fresha)",
            "rebuilding an existing Wix / WordPress / Squarespace site the business owns",
            "an SEO setup that only states verified facts",
        ]
    )
    include_link_in_first_message: bool = False
    language: str = "English"
    max_message_chars: int = 480
    banned_phrases: list[str] = Field(
        default_factory=lambda: [
            "elevate your",
            "seamless",
            "passionate about",
            "cutting-edge",
            "game-changer",
            "game changer",
            "take your business to the next level",
            "guaranteed",
            "limited time",
            "act now",
            "100%",
            "skyrocket",
            "dear sir",
            "dear madam",
        ]
    )


class NicheRule(BaseModel):
    name: str
    keywords: list[str]
    weight: float = 1.0


def _default_niches() -> list[NicheRule]:
    return [
        NicheRule(name="dental", keywords=["dentist", "dental", "orthodont", "smile", "teeth", "implant"]),
        NicheRule(name="salon", keywords=["salon", "hair", "barber", "unisex", "nail", "makeup", "beauty"]),
        NicheRule(name="spa", keywords=["spa", "massage", "wellness", "ayurved"]),
        NicheRule(name="clinic", keywords=["clinic", "dermat", "skin", "physio", "doctor", "hospital"]),
        NicheRule(name="restaurant", keywords=["restaurant", "dining", "kitchen", "bistro", "biryani"]),
        NicheRule(name="cafe", keywords=["cafe", "café", "coffee", "bakery", "patisserie", "dessert"]),
        NicheRule(name="pet", keywords=["pet shop", "pet store", "pets", "grooming", "veterinar", "vet "]),
        NicheRule(name="boutique", keywords=["boutique", "designer wear", "ethnic wear", "bridal", "couture"]),
        NicheRule(name="fitness", keywords=["gym", "fitness", "yoga", "pilates", "crossfit"]),
        NicheRule(name="studio", keywords=["tattoo", "photography", "photographer", "studio"]),
        NicheRule(name="jewellery", keywords=["jewellery", "jewelry", "jeweller", "gold", "diamond"]),
    ]


class ScoringSettings(BaseModel):
    niches: list[NicheRule] = Field(default_factory=_default_niches)
    target_locations: list[str] = Field(
        default_factory=lambda: [
            "mumbai",
            "navi mumbai",
            "thane",
            "andheri",
            "bandra",
            "powai",
            "vashi",
            "kharghar",
            "borivali",
            "malad",
            "goregaon",
            "juhu",
            "dadar",
            "chembur",
            "ghatkopar",
            "mulund",
            "panvel",
            "belapur",
            "nerul",
            "mira road",
            "bhayandar",
            "worli",
            "lower parel",
            "colaba",
            "kandivali",
            "santacruz",
            "vile parle",
            "khar",
            "versova",
            "lokhandwala",
            "airoli",
            "kalyan",
            "dombivli",
            "sion",
            "matunga",
            "byculla",
        ]
    )
    competitor_keywords: list[str] = Field(
        default_factory=lambda: [
            "web design",
            "website design",
            "web development",
            "web developer",
            "digital marketing",
            "marketing agency",
            "seo services",
            "social media marketing",
            "branding agency",
            "app development",
            "smm",
            "website developer",
        ]
    )
    min_followers: int = 100
    sweet_spot_followers: tuple[int, int] = (300, 50_000)
    max_followers: int = 300_000
    inactive_after_days: int = 120
    min_score_to_contact: int = 60
    require_business_signals: bool = True
    check_websites: bool = True
    website_check_timeout_seconds: float = 8.0
    website_recheck_days: int = 30
    # How strongly each LemmeDeliver opportunity counts (0-30 points).
    opportunity_weights: dict[str, int] = Field(
        default_factory=lambda: {
            "NEW_WEBSITE": 30,
            "BROKEN_WEBSITE": 28,
            "ONLINE_BOOKING": 22,
            "WEBSITE_REBUILD": 18,
            "VISUAL_SHOWCASE": 10,
        }
    )


class StrategySpec(BaseModel):
    name: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class CampaignSettings(BaseModel):
    id: str
    enabled: bool = True
    niches: list[str]
    locations: list[str]
    strategies: list[StrategySpec]
    min_score: int | None = None
    max_new_leads_per_day: int = 60


def _default_campaigns() -> list[CampaignSettings]:
    return [
        CampaignSettings(
            id="mumbai-local-services",
            niches=["dentist", "restaurant", "salon", "cafe", "pet shop", "boutique"],
            locations=["Mumbai", "Navi Mumbai", "Thane"],
            strategies=[
                StrategySpec(name="keyword_search", params={"max_queries_per_run": 3, "max_results": 20}),
                StrategySpec(name="suggested_accounts", params={"seeds_per_run": 2, "max_results": 15}),
                StrategySpec(name="hashtag", params={"tags_per_run": 2, "max_posts": 9}),
                StrategySpec(name="location", enabled=False, params={"locations": [], "max_posts": 9}),
                StrategySpec(name="followers_of", enabled=False, params={"seeds": [], "max_results": 40}),
                StrategySpec(name="post_engagers", enabled=False, params={"posts": [], "max_results": 30}),
            ],
        )
    ]


class RepliesSettings(BaseModel):
    # Even in AUTONOMOUS mode, replies to prospects are drafted for approval
    # unless this is explicitly enabled.
    autonomous: bool = False
    handoff_on_interest: bool = True
    acknowledge_opt_out: bool = False


class OwnershipSettings(BaseModel):
    lock_ttl_seconds: int = 300
    human_hold_hours: float | None = None  # None: paused until released explicitly
    echo_match_window_seconds: int = 900


class ControlApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    token: SecretStr | None = None


class RetentionSettings(BaseModel):
    """Data minimisation: raw webhook payloads can contain unrelated private DMs."""

    webhook_payload_days: int = 7  # processed raw deliveries (dedupe needs only ~36h of Meta retries)
    evidence_days: int = 30  # screenshots/traces; folders cited by open incidents are kept


class NotificationSettings(BaseModel):
    webhook_url: str | None = None  # generic JSON POST (n8n, Slack-compatible relays...)
    min_severity: IncidentSeverity = IncidentSeverity.WARNING


class SimulationSettings(BaseModel):
    """LOCAL environment only: the in-process simulated Instagram world."""

    seed: int = 7
    # Simulate Business Discovery on the API lane so local runs exercise the
    # API-first / browser-fallback routing for profile inspection.
    api_business_discovery: bool = True
    prospects_reply: bool = True


class Settings(BaseModel):
    environment: Environment = Environment.LOCAL
    default_mode: OperatingMode = OperatingMode.OBSERVE
    data_dir: Path = Path("data")
    database_url: str | None = None
    account: AccountSettings = Field(default_factory=AccountSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    offer: OfferSettings = Field(default_factory=OfferSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    campaigns: list[CampaignSettings] = Field(default_factory=_default_campaigns)
    replies: RepliesSettings = Field(default_factory=RepliesSettings)
    ownership: OwnershipSettings = Field(default_factory=OwnershipSettings)
    control_api: ControlApiSettings = Field(default_factory=ControlApiSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        # Separate databases per environment so simulated leads can never be
        # mixed into live outreach state.
        return f"sqlite:///{self.data_dir / f'{self.environment.value}.db'}"


_SECRET_ENV = {
    "IG_ACCESS_TOKEN": ("api", "access_token"),
    "IG_APP_SECRET": ("api", "app_secret"),
    "IG_WEBHOOK_VERIFY_TOKEN": ("api", "webhook_verify_token"),
    "IG_USER_ID": ("api", "ig_user_id"),
    "CONTROL_API_TOKEN": ("control_api", "token"),
    "NOTIFY_WEBHOOK_URL": ("notifications", "webhook_url"),
}


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def apply_env_overrides(data: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    for var, path in _SECRET_ENV.items():
        if env.get(var):
            _set_path(data, list(path), env[var])
    for var, raw in env.items():
        if not var.startswith("INSTA__"):
            continue
        keys = [part.lower() for part in var.removeprefix("INSTA__").split("__") if part]
        if keys:
            _set_path(data, keys, yaml.safe_load(raw) if raw != "" else None)
    return data


def default_config_path() -> Path:
    return Path(os.environ.get("INSTA_OUTREACH_CONFIG", "config/settings.yaml"))


def load_settings(path: Path | str | None = None, environ: dict[str, str] | None = None) -> Settings:
    config_path = Path(path) if path else default_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{config_path}: top level must be a mapping")
        data = loaded or {}
    return Settings.model_validate(apply_env_overrides(data, environ))
