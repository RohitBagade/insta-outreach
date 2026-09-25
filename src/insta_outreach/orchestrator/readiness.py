"""Live readiness: what must be true before the real account may run AUTONOMOUS.

``insta-outreach preflight`` prints every check. ``ControlService.set_mode``
refuses AUTONOMOUS in the live environment while any *required* check fails,
and ``insta-outreach run`` refuses to start in that state. The simulated
(local) environment has no prerequisites.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
    Capability,
    Channel,
    Environment,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
)
from insta_outreach.orchestrator.pipeline import Services
from insta_outreach.policy.gate import AUTO_APPROVER
from insta_outreach.policy.lanes import LaneService
from insta_outreach.storage.models import Action, ActionAttempt, Incident
from insta_outreach.util.clock import utc

_SEND_TYPES = (ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool  # required before AUTONOMOUS may run in the live environment

    @property
    def mark(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL" if self.required else "WARN"


def live_readiness(services: Services, lanes: LaneService) -> list[Check]:
    settings = services.settings
    now = services.clock.now()
    rollout = settings.rollout
    checks: list[Check] = []
    live = settings.environment is Environment.LIVE
    checks.append(
        Check(
            "environment",
            True,
            f"{settings.environment.value}" + ("" if live else " (simulation: no live prerequisites apply)"),
            False,
        )
    )
    if not live:
        return checks

    checks.append(
        Check(
            "control_token",
            settings.control_api.token is not None,
            "CONTROL_API_TOKEN is set"
            if settings.control_api.token
            else "set CONTROL_API_TOKEN (protects the kill switch and approvals)",
            True,
        )
    )
    adapters = services.executor.adapters
    browser_on, api_on = Channel.BROWSER in adapters, Channel.API in adapters
    send_channels = sorted(
        {
            c.value
            for cap in (Capability.SEND_NEW_DM, Capability.SEND_DM_REPLY, Capability.PRIVATE_REPLY)
            for c in services.executor.candidate_channels(cap)
        }
    )
    checks.append(
        Check(
            "send_lane",
            bool(send_channels),
            f"outbound lanes configured: {', '.join(send_channels)}"
            if send_channels
            else "no outbound lane: enable the browser (browser.enabled) and/or the API (api.enabled + token)",
            True,
        )
    )
    if browser_on:
        profile = Path(settings.browser.profiles_dir) / settings.account.id
        has_profile = profile.is_dir() and any(profile.iterdir())
        verified_at = _browser_verified_at(services)
        max_age = timedelta(days=rollout.browser_session_max_age_days)
        fresh = verified_at is not None and now - verified_at <= max_age
        detail = (f"session verified {verified_at.isoformat()}" if verified_at else "never verified") + (
            "" if has_profile else f"; no browser profile at {profile}"
        )
        if not (fresh and has_profile):
            detail += " -> run `insta-outreach browser login`, then `insta-outreach browser probe`"
        checks.append(Check("browser_session", fresh and has_profile, detail, True))
    else:
        checks.append(Check("browser_session", True, "browser lane disabled (first DMs need it)", False))
    if settings.api.enabled:
        checks.append(
            Check(
                "api_credentials",
                settings.api.configured and api_on,
                "IG_ACCESS_TOKEN + IG_USER_ID present"
                if settings.api.configured
                else "api.enabled but IG_ACCESS_TOKEN / IG_USER_ID missing",
                True,
            )
        )
        webhooks = bool(settings.api.app_secret and settings.api.webhook_verify_token)
        checks.append(
            Check(
                "webhooks",
                webhooks,
                "IG_APP_SECRET + IG_WEBHOOK_VERIFY_TOKEN present (subscription itself is verified in Meta)"
                if webhooks
                else "no webhook secrets: replies/takeovers are only seen when threads are read",
                False,
            )
        )
    else:
        checks.append(Check("api_credentials", True, "API lane disabled (optional)", False))

    with services.db.session() as session:
        halted = [s for s in lanes.all_snapshots(session, settings.account.id) if s.halted]
        open_incidents = session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.status == IncidentStatus.OPEN,
                Incident.severity.in_([IncidentSeverity.WARNING, IncidentSeverity.CRITICAL]),
            )
        )
        approved_ok = session.scalar(
            select(func.count())
            .select_from(Action)
            .where(
                Action.type.in_(_SEND_TYPES),
                Action.status == ActionStatus.SUCCEEDED,
                Action.approved_by.is_not(None),
                Action.approved_by != AUTO_APPROVER,
            )
        )
    checks.append(
        Check(
            "lanes",
            not halted,
            "no lane halted"
            if not halted
            else "halted: " + "; ".join(f"{s.channel.value} ({s.reason})" for s in halted),
            True,
        )
    )
    checks.append(
        Check(
            "incidents",
            not open_incidents,
            "no open warning/critical incidents" if not open_incidents else f"{open_incidents} open incident(s)",
            True,
        )
    )
    need = rollout.min_approved_sends_for_autonomous
    checks.append(
        Check(
            "approved_sends",
            (approved_ok or 0) >= need,
            f"{approved_ok or 0} human-approved live send(s) succeeded; {need} required before AUTONOMOUS",
            True,
        )
    )
    targets = rollout.allowed_targets
    checks.append(
        Check(
            "sandbox",
            True,
            f"outbound restricted to {', '.join('@' + t.lstrip('@') for t in targets)}"
            if targets
            else "no target allowlist (outbound may go to any qualified lead)",
            False,
        )
    )
    checks.append(Check("global_pause", True, "ON" if services.runtime.paused() else "off", False))
    llm = "Claude available" if _llm_available(services) else "templates only (no ANTHROPIC_API_KEY)"
    checks.append(Check("llm", True, llm, False))
    return checks


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.required and not c.ok]


def _browser_verified_at(services: Services) -> datetime | None:
    marker = services.runtime.browser_session().get("verified_at")
    candidates = [utc(datetime.fromisoformat(marker))] if marker else []
    with services.db.session() as session:
        last_ok = session.scalar(
            select(func.max(ActionAttempt.finished_at)).where(
                ActionAttempt.channel == Channel.BROWSER,
                ActionAttempt.status == ExecutionStatus.SUCCESS,
                ActionAttempt.simulated.is_(False),
            )
        )
    if last_ok is not None:
        candidates.append(utc(last_ok))
    known = [c for c in candidates if c is not None]
    return max(known) if known else None


def _llm_available(services: Services) -> bool:
    composer_llm = getattr(services.composer, "_llm", None)
    return bool(getattr(composer_llm, "available", False))
