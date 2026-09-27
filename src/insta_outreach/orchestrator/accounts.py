"""Instagram logins from Mission Control: open a login window, test the session.

The login window is an ordinary Chromium window on this computer: Rohit types
the password and answers any security check himself. The program never types
credentials and never touches a checkpoint. While the window is open, the
account's lane is stopped so no automated step shares the browser profile;
a lane that was running is resumed afterwards. A lane Instagram had stopped
(checkpoint, logged out) stays stopped: Rohit resumes it once he is satisfied.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from insta_outreach.config import Settings
from insta_outreach.domain.enums import Capability, Channel, Environment, ExecutionStatus, LaneState
from insta_outreach.domain.models import OperationRequest
from insta_outreach.orchestrator.control import ControlError, ControlService
from insta_outreach.orchestrator.pipeline import Services
from insta_outreach.orchestrator.readiness import browser_verified_at
from insta_outreach.policy.lanes import LaneService

log = logging.getLogger(__name__)

LoginFn = Callable[[Settings, Callable[[str], None]], Awaitable[tuple[bool, str]]]
ACCOUNTS = {"brand": Channel.BROWSER, "research": Channel.RESEARCH}

_CHECK_RESULT = {
    ExecutionStatus.SUCCESS: "The login works: the profile opened normally.",
    ExecutionStatus.LOGIN_REQUIRED: "Instagram shows its login page. Press Log in and sign in again.",
    ExecutionStatus.CHECKPOINT_REQUIRED: (
        "Instagram asks to confirm it's you. Confirm in the Instagram app, press Log in, then Resume."
    ),
    ExecutionStatus.ACCOUNT_RESTRICTED: "Instagram says the account is restricted. Check the Instagram app.",
    ExecutionStatus.HUMAN_ACTION_REQUIRED: "Instagram is asking for something only you can do. Check the app.",
    ExecutionStatus.RATE_LIMITED: "Instagram said 'try again later'. Wait a while before checking again.",
}


def account_settings(settings: Settings, which: str) -> tuple[Settings, Channel]:
    """Settings for one of our Instagram accounts: the brand account or the research account."""
    if which not in ACCOUNTS:
        raise ControlError("account must be brand or research")
    if which == "brand":
        return settings, Channel.BROWSER
    research = settings.research
    if not (research.enabled and research.account.username):
        raise ControlError("turn on the research account and give its username first (Settings)")
    return settings.model_copy(update={"account": research.account}), Channel.RESEARCH


async def _real_login(settings: Settings, announce: Callable[[str], None]) -> tuple[bool, str]:
    from insta_outreach.execution.browser.tools import interactive_login

    return await interactive_login(settings, announce=announce)


@dataclass
class Job:
    kind: str  # login / check
    which: str
    state: str = "running"  # running / ok / failed
    message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    task: asyncio.Task[None] | None = None

    def as_dict(self, local: Callable[[datetime | None], str | None]) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "message": self.message,
            "started_at_local": local(self.started_at),
            "finished_at_local": local(self.finished_at),
        }


class AccountSessions:
    def __init__(
        self,
        services: Services,
        lanes: LaneService,
        control: ControlService,
        local: Callable[[datetime | None], str | None],
        login: LoginFn | None = None,
    ) -> None:
        self.s = services
        self._lanes = lanes
        self._control = control
        self._local = local
        self._login = login or _real_login
        self._jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------ view
    def accounts(self) -> list[dict[str, Any]]:
        settings = self.s.settings
        live = settings.environment is Environment.LIVE
        rows = []
        for which, channel in ACCOUNTS.items():
            account = settings.account if which == "brand" else settings.research.account
            if which == "research" and not settings.research.enabled:
                continue
            with self.s.db.session() as session:
                lane = self._lanes.snapshot(session, settings.account.id, channel)
            verified = browser_verified_at(self.s, channel)
            profile = Path(settings.browser.profiles_dir) / account.id
            job = self._jobs.get(which)
            rows.append(
                {
                    "which": which,
                    "channel": channel.value,
                    "username": account.username,
                    "configured": channel in self.s.executor.adapters,
                    "simulated": not live,
                    "lane_state": lane.state.value,
                    "lane_reason": lane.reason,
                    "verified_at_local": self._local(verified),
                    "verified_via": self.s.runtime.browser_session(channel).get("via"),
                    "profile_saved": live and profile.is_dir() and any(profile.iterdir()),
                    "job": job.as_dict(self._local) if job else None,
                }
            )
        return rows

    # --------------------------------------------------------------- actions
    def _ready(self, which: str) -> tuple[Settings, Channel]:
        chosen, channel = account_settings(self.s.settings, which)
        if self.s.settings.environment is not Environment.LIVE:
            raise ControlError("The simulation has no real Instagram account to log in to.")
        if channel not in self.s.executor.adapters:
            raise ControlError(
                "Turn on the browser (Settings) and restart first."
                if which == "brand"
                else "The research account is saved but not running yet: restart first."
            )
        running = next((j for j in self._jobs.values() if j.state == "running"), None)
        if running is not None:
            raise ControlError(f"Wait for the {running.kind} of the {running.which} account to finish.")
        return chosen, channel

    def start_login(self, which: str, by: str) -> dict[str, Any]:
        chosen, channel = self._ready(which)
        job = Job("login", which, message="Opening a login window on this computer…", started_at=self.s.clock.now())
        self._jobs[which] = job
        job.task = asyncio.get_running_loop().create_task(self._run_login(job, chosen, channel, by))
        return job.as_dict(self._local)

    async def _run_login(self, job: Job, chosen: Settings, channel: Channel, by: str) -> None:
        account_id = self.s.settings.account.id
        with self.s.db.session() as session:
            was = self._lanes.snapshot(session, account_id, channel).state
        stopped_here = was is LaneState.ACTIVE
        if stopped_here:
            self._control.halt_lane(channel, by=by, reason="login window open")

        def announce(text: str) -> None:
            job.message = text

        try:
            async with self.s.executor.lane_lock(channel):  # lets a running step finish first
                await self.s.executor.adapters[channel].close()  # frees the browser profile
                ok, message = await self._login(chosen, announce)
            job.state, job.message = ("ok" if ok else "failed"), message
            if ok:
                self.s.runtime.record_browser_session("login", by=by, detail=message, channel=channel)
                if was is LaneState.HALTED:
                    job.message += ". Press Resume when you're happy for the bot to continue."
        except asyncio.CancelledError:
            job.state, job.message = "failed", "stopped before the login finished"
            raise
        except Exception as exc:  # e.g. Chromium missing, profile locked: shown on the page
            log.exception("login window failed")
            job.state, job.message = "failed", f"The login window failed: {type(exc).__name__}: {exc}"[:300]
        finally:
            job.finished_at = self.s.clock.now()
            if stopped_here:
                self._control.resume_lane(channel, by=by, note="login window closed")

    def start_check(self, which: str, by: str) -> dict[str, Any]:
        chosen, channel = self._ready(which)
        job = Job("check", which, message="Opening your own profile, read-only…", started_at=self.s.clock.now())
        self._jobs[which] = job
        job.task = asyncio.get_running_loop().create_task(self._run_check(job, chosen, channel, by))
        return job.as_dict(self._local)

    async def _run_check(self, job: Job, chosen: Settings, channel: Channel, by: str) -> None:
        """A read-only look at the account's own profile, through the normal lane:
        same pacing, and a checkpoint stops the lane exactly as in real work."""
        settings = self.s.settings
        try:
            request = OperationRequest(
                action_id=f"check-{job.which}-{int(self.s.clock.now().timestamp())}",
                account_id=settings.account.id,
                capability=Capability.INSPECT_PROFILE,
                target_username=chosen.account.username,
                params={"open_posts": 0},
                max_units=3,
            )
            result = await self.s.executor.execute(request, only=channel)
            with self.s.db.session() as session:
                self._lanes.record_result(session, settings.account.id, result, self.s.runtime.limits())
            await self.s.incidents.flush()
            ok = result.status is ExecutionStatus.SUCCESS
            if ok:
                self.s.runtime.record_browser_session("check", by=by, detail="own profile opened", channel=channel)
            job.state = "ok" if ok else "failed"
            job.message = _CHECK_RESULT.get(result.status) or (
                "The account is stopped or resting: resume it first."
                if result.code == "no_channel"
                else f"Couldn't confirm the login: {result.code or result.status.value}"
            )
        except Exception as exc:
            log.exception("login check failed")
            job.state, job.message = "failed", f"The check failed: {type(exc).__name__}: {exc}"[:300]
        finally:
            job.finished_at = self.s.clock.now()

    async def close(self) -> None:
        for job in self._jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job.task
