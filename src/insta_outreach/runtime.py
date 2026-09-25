"""Runtime-mutable control state (mode, global pause, limit overrides).

Stored in the database so the CLI, the control API and the orchestrator
process all see the same value, and a switch takes effect on the next tick
without a restart or a different implementation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from insta_outreach.config import LimitsSettings, Settings
from insta_outreach.domain.enums import OperatingMode
from insta_outreach.policy.audit import audit
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import RuntimeSetting
from insta_outreach.util.clock import Clock

MODE_KEY = "operating_mode"
PAUSED_KEY = "global_pause"
LIMITS_KEY = "limits_overrides"
BROWSER_SESSION_KEY = "browser_session"


class RuntimeControl:
    def __init__(self, db: Database, settings: Settings, clock: Clock) -> None:
        self._db = db
        self._settings = settings
        self._clock = clock

    # -- generic -----------------------------------------------------------
    def _get(self, session: Session, key: str) -> Any:
        row = session.get(RuntimeSetting, key)
        return None if row is None else row.value

    def _put(self, session: Session, key: str, value: Any, by: str) -> None:
        row = session.get(RuntimeSetting, key)
        now = self._clock.now()
        if row is None:
            session.add(RuntimeSetting(key=key, value=value, updated_by=by, updated_at=now))
        else:
            row.value = value
            row.updated_by = by
            row.updated_at = now

    # -- mode --------------------------------------------------------------
    def mode(self) -> OperatingMode:
        with self._db.session() as session:
            raw = self._get(session, MODE_KEY)
        return OperatingMode(raw) if raw else self._settings.default_mode

    def set_mode(self, mode: OperatingMode, by: str) -> OperatingMode:
        with self._db.session() as session:
            raw = self._get(session, MODE_KEY)
            previous = OperatingMode(raw) if raw else self._settings.default_mode
            self._put(session, MODE_KEY, mode.value, by)
            audit(
                session,
                self._clock.now(),
                actor=by,
                kind="mode.changed",
                subject="runtime",
                summary=f"mode {previous.value} -> {mode.value}",
                previous=previous,
                mode=mode,
            )
        return previous

    # -- global pause (kill switch: no executor calls at all) ---------------
    def paused(self) -> bool:
        with self._db.session() as session:
            return bool(self._get(session, PAUSED_KEY))

    def set_paused(self, paused: bool, by: str) -> None:
        with self._db.session() as session:
            self._put(session, PAUSED_KEY, bool(paused), by)
            audit(
                session,
                self._clock.now(),
                actor=by,
                kind="pause.changed",
                subject="runtime",
                summary=f"global pause {'ON: no executor activity' if paused else 'OFF'}",
                paused=bool(paused),
            )

    # -- limits ------------------------------------------------------------
    def limit_overrides(self) -> dict[str, Any]:
        with self._db.session() as session:
            return dict(self._get(session, LIMITS_KEY) or {})

    def limits(self) -> LimitsSettings:
        merged = self._settings.limits.model_dump()
        merged.update(self.limit_overrides())
        return LimitsSettings.model_validate(merged)

    def set_limit_overrides(self, overrides: dict[str, Any], by: str) -> LimitsSettings:
        current = self.limit_overrides()
        current.update(overrides)
        # Validate before persisting so a typo cannot poison the runtime state.
        merged = self._settings.limits.model_dump()
        merged.update(current)
        validated = LimitsSettings.model_validate(merged)
        unknown = set(current) - set(LimitsSettings.model_fields)
        if unknown:
            raise ValueError(f"unknown limit keys: {sorted(unknown)}")
        with self._db.session() as session:
            self._put(session, LIMITS_KEY, current, by)
            audit(
                session,
                self._clock.now(),
                actor=by,
                kind="limits.changed",
                subject="runtime",
                summary="limit overrides: " + ", ".join(f"{k}={v}" for k, v in sorted(overrides.items())),
                overrides=overrides,
                active_overrides=current,
            )
        return validated

    def clear_limit_overrides(self, by: str) -> None:
        with self._db.session() as session:
            self._put(session, LIMITS_KEY, {}, by)
            audit(
                session,
                self._clock.now(),
                actor=by,
                kind="limits.cleared",
                subject="runtime",
                summary="limit overrides cleared (configured limits apply)",
            )

    # -- browser session proof (for the live readiness check) --------------------
    def browser_session(self) -> dict[str, Any]:
        with self._db.session() as session:
            return dict(self._get(session, BROWSER_SESSION_KEY) or {})

    def record_browser_session(self, via: str, by: str, detail: str = "") -> None:
        now = self._clock.now()
        with self._db.session() as session:
            self._put(session, BROWSER_SESSION_KEY, {"verified_at": now.isoformat(), "via": via}, by)
            audit(
                session,
                now,
                actor=by,
                kind="browser.session_verified",
                subject="lane:BROWSER",
                summary=f"browser session verified via {via}" + (f": {detail}" if detail else ""),
                via=via,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._db.session() as session:
            rows = session.scalars(select(RuntimeSetting)).all()
            return {
                row.key: {"value": row.value, "updated_by": row.updated_by, "updated_at": row.updated_at}
                for row in rows
            }
