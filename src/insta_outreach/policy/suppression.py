"""Prospect suppression (opt-outs, manual blocks, policy exclusions)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import SuppressionKind
from insta_outreach.storage.models import Suppression
from insta_outreach.util.text import canonical_username, registrable_domain

_DIGITS = re.compile(r"\D+")


def canonical_value(kind: SuppressionKind, value: str) -> str:
    value = (value or "").strip()
    if kind is SuppressionKind.USERNAME:
        return canonical_username(value)
    if kind is SuppressionKind.DOMAIN:
        domain = registrable_domain(value)
        if not domain:
            raise ValueError(f"not a domain: {value!r}")
        return domain
    if kind is SuppressionKind.EMAIL:
        return value.lower()
    if kind is SuppressionKind.PHONE:
        digits = _DIGITS.sub("", value)
        return digits[-10:] if len(digits) >= 10 else digits
    return value


def add_suppression(session: Session, kind: SuppressionKind, value: str, reason: str, source: str) -> bool:
    canonical = canonical_value(kind, value)
    exists = session.scalars(
        select(Suppression).where(Suppression.kind == kind, Suppression.value == canonical)
    ).first()
    if exists is not None:
        return False
    try:
        with session.begin_nested():
            session.add(Suppression(kind=kind, value=canonical, reason=reason, source=source))
    except IntegrityError:
        return False
    return True


def remove_suppression(session: Session, kind: SuppressionKind, value: str) -> bool:
    canonical = canonical_value(kind, value)
    row = session.scalars(select(Suppression).where(Suppression.kind == kind, Suppression.value == canonical)).first()
    if row is None:
        return False
    session.delete(row)
    return True


def suppression_reason(
    session: Session,
    *,
    username: str | None = None,
    igsid: str | None = None,
    domain: str | None = None,
    emails: Iterable[str] = (),
    phones: Iterable[str] = (),
) -> str | None:
    """Return the reason if any identifier of the prospect is suppressed."""
    clauses = []
    if username:
        clauses.append(
            (Suppression.kind == SuppressionKind.USERNAME)
            & (Suppression.value == canonical_value(SuppressionKind.USERNAME, username))
        )
    if igsid:
        clauses.append((Suppression.kind == SuppressionKind.IGSID) & (Suppression.value == igsid))
    if domain:
        canonical_domain = registrable_domain(domain)
        if canonical_domain:
            clauses.append((Suppression.kind == SuppressionKind.DOMAIN) & (Suppression.value == canonical_domain))
    email_values = [canonical_value(SuppressionKind.EMAIL, e) for e in emails if e]
    if email_values:
        clauses.append((Suppression.kind == SuppressionKind.EMAIL) & Suppression.value.in_(email_values))
    phone_values = [canonical_value(SuppressionKind.PHONE, p) for p in phones if p]
    if phone_values:
        clauses.append((Suppression.kind == SuppressionKind.PHONE) & Suppression.value.in_(phone_values))
    if not clauses:
        return None
    row = session.scalars(select(Suppression).where(or_(*clauses))).first()
    return None if row is None else f"suppressed ({row.kind.value}={row.value}): {row.reason}"
