"""Mission Control's Settings page: what the command line used to be needed for.

Editable settings (:mod:`insta_outreach.editable_settings`), Instagram logins
(:mod:`insta_outreach.orchestrator.accounts`), phone alerts, and the facts the
page shows about the program itself. Every change is audited; secrets are
written only to ``.env`` and never returned or logged.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import func, select

from insta_outreach import __version__
from insta_outreach.deploy import set_env_values
from insta_outreach.domain.enums import IncidentSeverity
from insta_outreach.editable_settings import STRATEGY_INFO, SettingsEditor, SettingsError
from insta_outreach.notify import FanoutNotifier, Notifier, TelegramNotifier, WebhookNotifier, telegram_chats
from insta_outreach.orchestrator.accounts import AccountSessions
from insta_outreach.orchestrator.monitor import GOOD_FIT
from insta_outreach.orchestrator.pipeline import Services
from insta_outreach.policy.audit import audit
from insta_outreach.storage.models import Lead

_BOT_TOKEN = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,64}$")
_CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
TEST_TITLE = "Test alert from insta-outreach"
TEST_DETAIL = "If you can read this, alerts reach you. Real alerts: checkpoints, stopped lanes, warm leads."


class SetupService:
    def __init__(
        self,
        services: Services,
        notifier: Notifier,
        accounts: AccountSessions,
        env_file: Path = Path(".env"),
    ) -> None:
        self.s = services
        self._notifier = notifier
        self.accounts = accounts
        self.env_file = env_file
        self.editor = SettingsEditor(services.settings, on_notifications_changed=self.reload_alerts)

    # ------------------------------------------------------------------ view
    def view(self) -> dict[str, Any]:
        settings = self.s.settings
        limits = self.s.runtime.limits()
        return {
            **self.editor.view(),
            "environment": settings.environment.value,
            "account": settings.account.username,
            "accounts": self.accounts.accounts(),
            "alerts": self.alerts(),
            "api": {
                "enabled": settings.api.enabled,
                "connected": settings.api.configured,
                "login_type": settings.api.login_type,
            },
            "limits": limits.model_dump(),
            "limit_overrides": self.s.runtime.limit_overrides(),
            "limit_defaults": settings.limits.model_dump(),
            "program": {
                "version": __version__,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "database": settings.resolved_database_url.split("///")[-1],
                "env_file": str(self.env_file),
                "env_file_exists": self.env_file.exists(),
            },
        }

    def change(self, changes: dict[str, Any], by: str) -> dict[str, Any]:
        outcome = self.editor.apply(changes)
        labels = [self.editor.view()["labels"][key] for key in changes]
        with self.s.db.session() as session:
            audit(
                session,
                self.s.clock.now(),
                actor=by,
                kind="settings.changed",
                subject="settings",
                summary="settings changed: "
                + ", ".join(sorted(changes))
                + (" (applies after a restart)" if outcome["restart_required"] else ""),
                changed={key: changes[key] for key in sorted(changes)},
                labels=labels,
                restart_required=outcome["restart_required"],
            )
        return outcome

    # ------------------------------------------------------------- campaigns
    def campaigns(self) -> dict[str, Any]:
        view = self.editor.view()
        stats: dict[str, dict[str, int]] = {}
        with self.s.db.session() as session:
            for campaign_id, status, contacted, replied, n in session.execute(
                select(
                    Lead.campaign_id,
                    Lead.status,
                    Lead.contacted_at.is_not(None),
                    Lead.replied_at.is_not(None),
                    func.count(),
                ).group_by(Lead.campaign_id, Lead.status, Lead.contacted_at.is_not(None), Lead.replied_at.is_not(None))
            ):
                row = stats.setdefault(campaign_id or "", {"found": 0, "good_fit": 0, "messaged": 0, "replied": 0})
                row["found"] += n
                row["good_fit"] += n if status in GOOD_FIT else 0
                row["messaged"] += n if contacted else 0
                row["replied"] += n if replied else 0
        return {
            "campaigns": view["values"]["campaigns"],
            "strategies": STRATEGY_INFO,
            "stats": stats,
            "min_score_default": self.s.settings.scoring.min_score_to_contact,
            "can_edit": view["can_edit"],
            "why_not": view["why_not"],
        }

    # ---------------------------------------------------------------- alerts
    def alerts(self) -> dict[str, Any]:
        cfg = self.s.settings.notifications
        return {
            "telegram_token_set": cfg.telegram_bot_token is not None,
            "telegram_chat_id": cfg.telegram_chat_id,
            "telegram_ready": cfg.telegram_configured,
            "webhook_set": bool(cfg.webhook_url),
            "min_severity": cfg.min_severity.value,
            "dashboard_url": cfg.dashboard_url,
            "can_edit": self.editor.can_edit,
        }

    def reload_alerts(self) -> None:
        """Rebuild where alerts go from the current settings, without a restart."""
        from insta_outreach.app import alert_destinations

        if isinstance(self._notifier, FanoutNotifier):
            self._notifier.reconfigure(*alert_destinations(self.s.settings))

    def save_telegram(self, by: str, bot_token: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
        if not self.editor.can_edit:
            raise SettingsError("This is the demo: alerts are set up in the program itself.")
        values: dict[str, str | None] = {}
        if bot_token is not None:
            token = bot_token.strip()
            if not _BOT_TOKEN.match(token):
                raise SettingsError("That doesn't look like a bot token from @BotFather (digits:letters).")
            values["TELEGRAM_BOT_TOKEN"] = token
        if chat_id is not None:
            chat = chat_id.strip()
            if not _CHAT_ID.match(chat):
                raise SettingsError("A chat id is a number like 123456789 (press Find my chat).")
            values["TELEGRAM_CHAT_ID"] = chat
        if not values:
            raise SettingsError("Nothing to save.")
        set_env_values(self.env_file, values)
        cfg = self.s.settings.notifications
        for key, value in values.items():
            assert value is not None
            os.environ[key] = value  # a restart reads the same values
            if key == "TELEGRAM_BOT_TOKEN":
                cfg.telegram_bot_token = SecretStr(value)
            else:
                cfg.telegram_chat_id = value
        self.reload_alerts()
        with self.s.db.session() as session:
            audit(
                session,
                self.s.clock.now(),
                actor=by,
                kind="alerts.changed",
                subject="settings",
                summary="phone alerts: " + " and ".join(k.lower() for k in sorted(values)) + " saved in .env",
                changed=sorted(values),  # names only, never the values
            )
        return self.alerts()

    async def find_telegram_chat(self, by: str) -> dict[str, Any]:
        token = self.s.settings.notifications.telegram_bot_token
        if token is None:
            raise SettingsError("Save the bot token first.")
        try:
            chats = await telegram_chats(token.get_secret_value())
        except RuntimeError as exc:
            raise SettingsError(str(exc)) from None
        if not chats:
            return {
                "chats": [],
                "saved": False,
                "message": "No messages yet. Open your bot in Telegram, press Start, then press Find my chat again.",
            }
        if len(chats) == 1:
            self.save_telegram(by, chat_id=chats[0][0])
            return {"chats": [], "saved": True, "message": f"Found and saved your chat ({chats[0][1] or chats[0][0]})."}
        return {
            "chats": [{"id": cid, "name": name} for cid, name in chats],
            "saved": False,
            "message": "Several chats wrote to the bot: pick yours.",
        }

    async def test_alert(self) -> dict[str, Any]:
        cfg = self.s.settings.notifications
        results = []
        if cfg.telegram_bot_token is not None and cfg.telegram_chat_id:
            telegram = TelegramNotifier(
                cfg.telegram_bot_token.get_secret_value(),
                cfg.telegram_chat_id,
                IncidentSeverity.INFO,
                cfg.dashboard_url,
            )
            await telegram.notify(TEST_TITLE, TEST_DETAIL, IncidentSeverity.CRITICAL)
            results.append({"to": "Telegram", "ok": telegram.last_error is None, "error": telegram.last_error})
        if cfg.webhook_url:
            await WebhookNotifier(cfg.webhook_url, IncidentSeverity.INFO).notify(
                TEST_TITLE, TEST_DETAIL, IncidentSeverity.CRITICAL
            )
            results.append({"to": "webhook", "ok": True, "error": None})
        if not results:
            raise SettingsError("No phone alerts are set up yet.")
        return {"results": results}
