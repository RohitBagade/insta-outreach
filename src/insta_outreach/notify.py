"""Human-facing notifications (incidents, handoffs). Log always, webhook optional."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from insta_outreach.domain.enums import IncidentSeverity

log = logging.getLogger(__name__)

_SEVERITY_ORDER = {IncidentSeverity.INFO: 0, IncidentSeverity.WARNING: 1, IncidentSeverity.CRITICAL: 2}


class Notifier(Protocol):
    async def notify(
        self, title: str, detail: str, severity: IncidentSeverity, data: dict[str, Any] | None = None
    ) -> None: ...


class LogNotifier:
    async def notify(
        self, title: str, detail: str, severity: IncidentSeverity, data: dict[str, Any] | None = None
    ) -> None:
        level = logging.WARNING if severity is not IncidentSeverity.INFO else logging.INFO
        log.log(level, "[%s] %s — %s %s", severity.value, title, detail, data or "")


class WebhookNotifier:
    """POSTs a small JSON document (works with n8n, Zapier, Slack relays, ...)."""

    def __init__(self, url: str, min_severity: IncidentSeverity, timeout: float = 10.0) -> None:
        self._url = url
        self._min = min_severity
        self._timeout = timeout

    async def notify(
        self, title: str, detail: str, severity: IncidentSeverity, data: dict[str, Any] | None = None
    ) -> None:
        if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[self._min]:
            return
        payload = {
            "source": "insta-outreach",
            "severity": severity.value,
            "title": title,
            "detail": detail,
            "data": data or {},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:  # notifications must never break execution
            log.warning("notification webhook failed: %s", exc)


class FanoutNotifier:
    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = notifiers
        self.sent: list[dict[str, Any]] = []

    async def notify(
        self, title: str, detail: str, severity: IncidentSeverity, data: dict[str, Any] | None = None
    ) -> None:
        self.sent.append({"title": title, "detail": detail, "severity": severity, "data": data})
        for notifier in self._notifiers:
            await notifier.notify(title, detail, severity, data)
