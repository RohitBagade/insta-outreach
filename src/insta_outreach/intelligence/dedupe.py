"""Entity-level dedupe: one business, possibly several usernames.

Username uniqueness is enforced by the database. This catches the same
business behind different accounts (branch accounts sharing one website,
a renamed account with the same IG user id, shared phone/email) so a
business is never contacted twice.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from insta_outreach.domain.enums import LeadStatus
from insta_outreach.intelligence.signals import LinkKind
from insta_outreach.storage.models import Lead, LeadIdentity
from insta_outreach.util.text import registrable_domain

# A lead in one of these states has no claim on the identity.
_INERT = (LeadStatus.DUPLICATE, LeadStatus.DISQUALIFIED)


def identity_keys(lead: Lead) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if lead.ig_user_id:
        keys.add(("IG_USER_ID", lead.ig_user_id))
    signals = lead.signals or {}
    own = signals.get("own_website")
    if own and signals.get("link_kinds", {}).get(own) == LinkKind.CUSTOM_DOMAIN.value:
        domain = registrable_domain(own)
        if domain:
            keys.add(("DOMAIN", domain))
    contact = lead.contact or {}
    for phone in contact.get("phones", []):
        if phone and len(phone) >= 10:
            keys.add(("PHONE", phone[-10:]))
    for email in contact.get("emails", []):
        if email:
            keys.add(("EMAIL", email.lower()))
    return keys


def register_and_find_duplicate(session: Session, lead: Lead) -> tuple[Lead, str] | None:
    """Store the lead's identity keys and return an earlier lead sharing one.

    The earlier (already known) lead wins, so an existing conversation or
    queued outreach is never displaced by a newly found branch account.
    """
    keys = identity_keys(lead)
    existing = {(i.kind, i.value) for i in session.scalars(select(LeadIdentity).where(LeadIdentity.lead_id == lead.id))}
    for kind, value in keys - existing:
        session.add(LeadIdentity(lead_id=lead.id, kind=kind, value=value))
    session.flush()
    for kind, value in sorted(keys):
        other = session.scalars(
            select(Lead)
            .join(LeadIdentity, LeadIdentity.lead_id == Lead.id)
            .where(
                LeadIdentity.kind == kind,
                LeadIdentity.value == value,
                Lead.id != lead.id,
                Lead.id < lead.id,
                Lead.status.not_in(_INERT),
            )
            .order_by(Lead.id)
        ).first()
        if other is not None:
            return other, f"same {kind.lower().replace('_', ' ')} as @{other.username} ({value})"
    return None
