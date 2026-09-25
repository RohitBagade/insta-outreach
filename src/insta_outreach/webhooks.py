"""Meta webhook handling: signature verification, parsing, durable inbox.

The HTTP handler only verifies + persists (and answers 200 fast); the
orchestrator drains persisted events. Meta retries deliveries for ~36h, so
events are de-duplicated by content hash and messages again by ``mid``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from insta_outreach.domain.models import InboundEvent
from insta_outreach.storage.db import Database
from insta_outreach.storage.models import WebhookEvent
from insta_outreach.util.clock import Clock


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=' + HMAC-SHA256(app secret, raw body)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


def _ts(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, UTC)
    return fallback


def _message_event(item: dict[str, Any], our_id: str | None, now: datetime) -> InboundEvent | None:
    sender = (item.get("sender") or {}).get("id")
    recipient = (item.get("recipient") or {}).get("id")
    at = _ts(item.get("timestamp"), now)
    if "message" in item:
        msg = item["message"] or {}
        if msg.get("is_deleted") or msg.get("is_self") or msg.get("is_unsupported"):
            return None
        is_echo = bool(msg.get("is_echo")) or (our_id is not None and sender == our_id)
        text = msg.get("text")
        if not text and msg.get("attachments"):
            kinds = ",".join(str(a.get("type")) for a in msg["attachments"])
            text = f"[{kinds}]"
        return InboundEvent(
            kind="echo" if is_echo else "message",
            peer_igsid=recipient if is_echo else sender,
            text=text or "",
            platform_message_id=msg.get("mid"),
            at=at,
            raw=item,
        )
    if "message_edit" in item:
        edit = item["message_edit"] or {}
        return InboundEvent(
            kind="edit", peer_igsid=sender, text=edit.get("text"), platform_message_id=edit.get("mid"), at=at, raw=item
        )
    if "read" in item:
        return InboundEvent(kind="read", peer_igsid=sender, at=at, raw=item)
    if "reaction" in item:
        return InboundEvent(kind="reaction", peer_igsid=sender, at=at, raw=item)
    if "referral" in item:
        return InboundEvent(kind="referral", peer_igsid=sender, at=at, raw=item)
    return None


def parse_payload(payload: dict[str, Any], our_ig_id: str | None, now: datetime) -> list[InboundEvent]:
    if payload.get("object") not in ("instagram", "page"):
        return []
    events: list[InboundEvent] = []
    for entry in payload.get("entry") or []:
        for item in entry.get("messaging") or []:
            event = _message_event(item, our_ig_id, now)
            if event is not None:
                events.append(event)
        for change in entry.get("changes") or []:
            field, value = change.get("field"), change.get("value") or {}
            if field == "messages":
                event = _message_event(value, our_ig_id, now)
                if event is not None:
                    events.append(event)
            elif field in ("comments", "live_comments"):
                author = value.get("from") or {}
                events.append(
                    InboundEvent(
                        kind="comment",
                        peer_igsid=author.get("id"),
                        peer_username=author.get("username"),
                        text=value.get("text"),
                        comment_id=value.get("id"),
                        media_id=(value.get("media") or {}).get("id"),
                        at=now,
                        raw=value,
                    )
                )
            elif field == "mentions":
                events.append(
                    InboundEvent(
                        kind="mention",
                        comment_id=value.get("comment_id"),
                        media_id=value.get("media_id"),
                        at=now,
                        raw=value,
                    )
                )
    return events


class WebhookInbox:
    def __init__(self, db: Database, clock: Clock, our_ig_id: str | None) -> None:
        self._db = db
        self._clock = clock
        self._our_ig_id = our_ig_id

    def store(self, payload: dict[str, Any]) -> bool:
        """Persist a verified delivery. Returns False for a duplicate retry."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(canonical.encode()).hexdigest()
        try:
            with self._db.session() as session:
                if session.scalars(select(WebhookEvent.id).where(WebhookEvent.dedupe_key == key)).first():
                    return False
                session.add(
                    WebhookEvent(
                        object=str(payload.get("object")),
                        dedupe_key=key,
                        payload=payload,
                        received_at=self._clock.now(),
                    )
                )
        except IntegrityError:
            return False
        return True

    def drain(self, limit: int = 200) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        with self._db.session() as session:
            rows = session.scalars(
                select(WebhookEvent).where(WebhookEvent.processed_at.is_(None)).order_by(WebhookEvent.id).limit(limit)
            ).all()
            for row in rows:
                try:
                    events.extend(parse_payload(row.payload, self._our_ig_id, row.received_at))
                except (KeyError, TypeError, ValueError) as exc:
                    row.error = f"{type(exc).__name__}: {exc}"[:1000]
                row.processed_at = self._clock.now()
        return events
