"""Phone alerts through Telegram: format, severity filter, and no token leaks."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from insta_outreach.app import build_notifier
from insta_outreach.config import Settings, load_settings
from insta_outreach.domain.enums import IncidentSeverity
from insta_outreach.notify import TelegramNotifier, telegram_chats

TOKEN = "123456:SECRET-bot-token"


def recorder(status: int = 200, body: dict | None = None) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": status == 200})

    return seen, httpx.MockTransport(handle)


async def test_sends_plain_text_with_a_dashboard_link() -> None:
    seen, transport = recorder()
    bot = TelegramNotifier(TOKEN, "42", IncidentSeverity.WARNING, "http://mission.tail:8765", transport=transport)
    await bot.notify("BROWSER lane halted", "Confirm it's you <b>now</b>", IncidentSeverity.CRITICAL)
    await bot.notify("routine", "below the threshold", IncidentSeverity.INFO)
    assert len(seen) == 1 and bot.last_error is None
    request = seen[0]
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "42" and "parse_mode" not in body  # prospect text is never parsed as markup
    assert body["text"].startswith("\U0001f6d1 BROWSER lane halted")
    assert "<b>now</b>" in body["text"] and body["text"].endswith("Mission Control: http://mission.tail:8765")


@pytest.mark.parametrize("failure", ["refused", "network"])
async def test_failures_are_logged_without_the_token(failure: str, caplog: pytest.LogCaptureFixture) -> None:
    if failure == "refused":
        _, transport = recorder(401, {"ok": False, "description": "Unauthorized"})
    else:

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

        transport = httpx.MockTransport(boom)
    bot = TelegramNotifier(TOKEN, "42", IncidentSeverity.WARNING, transport=transport)
    with caplog.at_level(logging.WARNING):
        await bot.notify("t", "d", IncidentSeverity.CRITICAL)  # never raises
    assert bot.last_error is not None
    assert TOKEN not in caplog.text and TOKEN not in bot.last_error
    assert ("HTTP 401 Unauthorized" if failure == "refused" else "ConnectError") in bot.last_error


async def test_find_chat_lists_who_messaged_the_bot() -> None:
    updates = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 987, "first_name": "Rohit", "username": "rohit"}}},
            {"message": {"chat": {"id": 987, "first_name": "Rohit", "username": "rohit"}}},
            {"channel_post": {"chat": {"id": -100, "title": "Alerts"}}},
        ],
    }
    seen, transport = recorder(body=updates)
    assert await telegram_chats(TOKEN, transport=transport) == [("987", "Rohit (@rohit)"), ("-100", "Alerts")]
    assert seen[0].url.path == f"/bot{TOKEN}/getUpdates"
    _, refused = recorder(404, {"ok": False, "description": "Not Found"})
    with pytest.raises(RuntimeError) as exc:
        await telegram_chats(TOKEN, transport=refused)
    assert TOKEN not in str(exc.value)


def test_configured_from_env(tmp_path) -> None:
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42", "DASHBOARD_URL": "http://mission.tail:8765"}
    config = tmp_path / "settings.yaml"
    config.write_text("notifications: {min_severity: WARNING}\n", encoding="utf-8")
    settings = load_settings(config, environ=env)
    assert settings.notifications.telegram_configured
    assert settings.notifications.dashboard_url == "http://mission.tail:8765"
    kinds = [type(n).__name__ for n in build_notifier(settings)._notifiers]
    assert kinds == ["LogNotifier", "TelegramNotifier"]
    assert [type(n).__name__ for n in build_notifier(Settings())._notifiers] == ["LogNotifier"]
