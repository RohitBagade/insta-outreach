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
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import RuntimeSetting
from insta_outreach.util.clock import Clock

MODE_KEY = "operating_mode"
PAUSED_KEY = "global_pause"
LIMITS_KEY = "limits_overrides"


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
            previous = self._get(session, MODE_KEY)
            self._put(session, MODE_KEY, mode.value, by)
        return OperatingMode(previous) if previous else self._settings.default_mode

    # -- global pause (kill switch: no executor calls at all) ---------------
    def paused(self) -> bool:
        with self._db.session() as session:
            return bool(self._get(session, PAUSED_KEY))

    def set_paused(self, paused: bool, by: str) -> None:
        with self._db.session() as session:
            self._put(session, PAUSED_KEY, bool(paused), by)

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
        return validated

    def clear_limit_overrides(self, by: str) -> None:
        with self._db.session() as session:
            self._put(session, LIMITS_KEY, {}, by)

    def snapshot(self) -> dict[str, Any]:
        with self._db.session() as session:
            rows = session.scalars(select(RuntimeSetting)).all()
            return {
                row.key: {"value": row.value, "updated_by": row.updated_by, "updated_at": row.updated_at}
                for row in rows
            }
