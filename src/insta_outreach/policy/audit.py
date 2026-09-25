"""Append-only audit trail (``audit_events``).

Every operator change (mode, pause, limits, approvals, lanes, suppressions,
conversation ownership) and every consequential system decision (proposal
outcome, send, halt, cooldown, incident, human takeover, reply handling) is
recorded with who/what caused it. Read it with ``insta-outreach audit``.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from insta_outreach.storage.models import AuditEvent


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def audit(
    session: Session,
    at: datetime,
    *,
    actor: str,
    kind: str,
    summary: str,
    subject: str | None = None,
    **detail: Any,
) -> None:
    session.add(
        AuditEvent(
            at=at,
            actor=(actor or "system")[:64],
            kind=kind[:64],
            subject=subject[:128] if subject else None,
            summary=summary[:512],
            detail=_plain(detail),
        )
    )
