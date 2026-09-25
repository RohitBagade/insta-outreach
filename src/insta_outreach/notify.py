"""Human-facing notifications (incidents, handoffs).

Always logged; optionally POSTed to a webhook (n8n, Zapier, Slack relays) and/or
sent to a phone through a Telegram bot. A failing channel is logged and never
interrupts execution.
"""

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


class TelegramNotifier:
    """Phone alerts through a Telegram bot: free, works on any phone, no app of our own.

    The bot token is part of the request URL, so errors are logged by type and
    status only, never with the URL.
    """

    API = "https://api.telegram.org"
    ICON = {
        IncidentSeverity.CRITICAL: "\U0001f6d1",  # stop sign
        IncidentSeverity.WARNING: "\u26a0\ufe0f",
        IncidentSeverity.INFO: "\u2139\ufe0f",
    }

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        min_severity: IncidentSeverity,
        dashboard_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{self.API}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._min = min_severity
        self._dashboard_url = dashboard_url
        self._timeout = timeout
        self._transport = transport
        self.last_error: str | None = None

    def render(self, title: str, detail: str, severity: IncidentSeverity) -> str:
        text = f"{self.ICON[severity]} {title}"
        if detail:
            text += f"\n\n{detail}"
        if self._dashboard_url:
            text += f"\n\nMission Control: {self._dashboard_url}"
        return text[:4000]  # Telegram's limit is 4096 characters

    async def notify(
        self, title: str, detail: str, severity: IncidentSeverity, data: dict[str, Any] | None = None
    ) -> None:
        if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[self._min]:
            return
        # Plain text (no parse_mode): prospect-written text can never be interpreted as markup.
        text = self.render(title, detail, severity)
        body = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        self.last_error = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            self.last_error = f"network error ({type(exc).__name__})"
        else:
            if response.status_code != 200:
                self.last_error = f"HTTP {response.status_code} {_telegram_reason(response)}".strip()
        if self.last_error:
            log.warning("telegram alert failed: %s", self.last_error)


def _telegram_reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", ""))[:200]
    except ValueError:
        return ""


async def telegram_chats(bot_token: str, transport: httpx.AsyncBaseTransport | None = None) -> list[tuple[str, str]]:
    """Chats that have messaged the bot: how to find your TELEGRAM_CHAT_ID."""
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            response = await client.get(f"{TelegramNotifier.API}/bot{bot_token}/getUpdates")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Telegram ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise RuntimeError(f"Telegram refused: HTTP {response.status_code} {_telegram_reason(response)}".strip())
    chats: dict[str, str] = {}
    for update in response.json().get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[str(chat["id"])] = (name or "") + (f" (@{chat['username']})" if chat.get("username") else "")
    return list(chats.items())


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
