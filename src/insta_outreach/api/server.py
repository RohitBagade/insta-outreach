"""HTTP surface: Meta webhooks (public, signature-verified) + control plane.

The control plane (status, runtime mode, approvals, incidents, lanes,
conversation ownership, suppressions, the live Mission Control feed) requires
``control_api.token`` as a Bearer token whenever one is configured, and binds
to 127.0.0.1 by default. In the LIVE environment the token is mandatory:
without one every control endpoint answers 503.

``/`` serves Mission Control, a static page (``static/``) under a strict
Content-Security-Policy: no inline script, no third-party origin. It holds no
data itself; everything comes from the token-protected endpoints below.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from insta_outreach.app import App
from insta_outreach.domain.enums import (
    ActionStatus,
    Channel,
    Environment,
    LeadStatus,
    OperatingMode,
    SuppressionKind,
)
from insta_outreach.orchestrator.control import ControlError
from insta_outreach.reporting import explain_lead
from insta_outreach.webhooks import verify_signature

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
_STATIC_TYPES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def create_api(app: App, run_orchestrator: bool = False) -> FastAPI:
    settings = app.settings
    token = settings.control_api.token.get_secret_value() if settings.control_api.token else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(app.orchestrator.run_forever(stop)) if run_orchestrator else None
        if token is None and settings.environment is Environment.LIVE:
            log.error("control_api.token is not set: control endpoints are disabled in the live environment")
        elif token is None:
            log.warning(
                "control_api.token is not set: the control plane is unauthenticated "
                "(acceptable only on 127.0.0.1 for local use)"
            )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            await app.close()

    api = FastAPI(title="insta-outreach control plane", version="0.1.0", lifespan=lifespan)

    def require_token(authorization: str | None = Header(default=None)) -> str:
        if token is None:
            if settings.environment is Environment.LIVE:
                raise HTTPException(status_code=503, detail="set control_api.token (CONTROL_API_TOKEN) to use live")
            return "local"
        if not hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")
        return "operator"

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except ControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---------------------------------------------------------------- webhooks
    @api.get("/webhooks/instagram", response_class=PlainTextResponse)
    async def verify_webhook(
        mode: str = Query(alias="hub.mode", default=""),
        verify_token: str = Query(alias="hub.verify_token", default=""),
        challenge: str = Query(alias="hub.challenge", default=""),
    ) -> str:
        expected = settings.api.webhook_verify_token
        if mode == "subscribe" and expected and verify_token == expected.get_secret_value():
            return challenge
        raise HTTPException(status_code=403, detail="verification failed")

    @api.post("/webhooks/instagram", response_class=PlainTextResponse)
    async def receive_webhook(request: Request) -> str:
        raw = await request.body()
        secret = settings.api.app_secret
        if secret is not None:
            if not verify_signature(secret.get_secret_value(), raw, request.headers.get("x-hub-signature-256")):
                raise HTTPException(status_code=403, detail="bad signature")
        elif settings.environment is Environment.LIVE:
            raise HTTPException(status_code=503, detail="api.app_secret must be configured to accept webhooks")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        app.webhook_inbox.store(payload)  # processed by the orchestrator tick
        return "EVENT_RECEIVED"

    # ---------------------------------------------------------- mission control
    @api.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)

    @api.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> FileResponse:
        media_type = _STATIC_TYPES.get(name)  # a fixed list: nothing else is ever served from disk here
        if media_type is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(STATIC_DIR / name, media_type=media_type, headers=_PAGE_HEADERS)

    @api.get("/api/overview")
    def overview(_: str = Depends(require_token)) -> dict[str, Any]:
        return app.monitor.overview()

    @api.get("/api/feed")
    def feed(
        audit: int | None = Query(default=None, ge=0),
        attempt: int | None = Query(default=None, ge=0),
        limit: int = Query(default=80, ge=1, le=500),
        _: str = Depends(require_token),
    ) -> dict[str, Any]:
        return app.monitor.feed(after_audit=audit, after_attempt=attempt, limit=limit)

    # ----------------------------------------------------------------- control

    @api.get("/api/status")
    async def status(_: str = Depends(require_token)) -> dict[str, Any]:
        return app.control.status()

    @api.post("/api/mode")
    async def set_mode(body: dict[str, Any] = Body(...), who: str = Depends(require_token)) -> dict[str, Any]:
        try:
            mode = OperatingMode(str(body.get("mode", "")).upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"mode must be one of {[m.value for m in OperatingMode]}"
            ) from exc
        return guarded(lambda: app.control.set_mode(mode, by=who, confirm=bool(body.get("confirm"))))

    @api.post("/api/pause")
    async def pause(body: dict[str, Any] = Body(...), who: str = Depends(require_token)) -> dict[str, Any]:
        app.control.set_paused(bool(body.get("paused", True)), by=who)
        return {"global_pause": app.runtime.paused()}

    @api.patch("/api/limits")
    async def set_limits(body: dict[str, Any] = Body(...), who: str = Depends(require_token)) -> dict[str, Any]:
        try:
            if body.get("clear"):
                app.runtime.clear_limit_overrides(who)
                return app.runtime.limits().model_dump()
            return app.runtime.set_limit_overrides(dict(body.get("overrides", {})), who).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/actions")
    async def actions(
        status: list[str] = Query(default=[]), limit: int = 50, _: str = Depends(require_token)
    ) -> list[dict[str, Any]]:
        try:
            statuses = [ActionStatus(s.upper()) for s in status]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return app.control.list_actions(statuses or None, limit)

    @api.get("/api/actions/{action_id}")
    async def action_detail(action_id: str, _: str = Depends(require_token)) -> dict[str, Any]:
        return guarded(lambda: app.control.action_detail(action_id))

    @api.post("/api/actions/{action_id}/approve")
    async def approve(
        action_id: str, body: dict[str, Any] = Body(default={}), who: str = Depends(require_token)
    ) -> dict[str, Any]:
        edited = body.get("edited_text", body.get("message"))
        return guarded(lambda: app.control.approve(action_id, by=body.get("by") or who, edited_text=edited))

    @api.post("/api/actions/{action_id}/reject")
    async def reject(
        action_id: str, body: dict[str, Any] = Body(default={}), who: str = Depends(require_token)
    ) -> dict[str, Any]:
        return guarded(
            lambda: app.control.reject(
                action_id,
                by=body.get("by") or who,
                reason=str(body.get("reason", "")),
                redraft=bool(body.get("redraft")),
            )
        )

    @api.get("/api/leads")
    async def leads(
        status: list[str] = Query(default=[]), limit: int = 100, _: str = Depends(require_token)
    ) -> list[dict[str, Any]]:
        try:
            statuses = [LeadStatus(s.upper()) for s in status]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return app.control.leads(statuses or None, limit)

    @api.get("/api/leads/{ref}")
    async def lead(ref: str, _: str = Depends(require_token)) -> dict[str, Any]:
        return guarded(lambda: app.control.lead_detail(ref))

    @api.get("/api/leads/{ref}/explain")
    def explain(ref: str, _: str = Depends(require_token)) -> dict[str, Any]:
        """The lead's decision trail: every step, gate verdict and message, in order."""
        return {"lines": guarded(lambda: explain_lead(app, ref))}

    @api.post("/api/leads")
    async def add_lead(body: dict[str, Any] = Body(...), who: str = Depends(require_token)) -> dict[str, Any]:
        return guarded(
            lambda: app.control.add_lead(
                str(body.get("username", "")),
                by=who,
                campaign_id=body.get("campaign_id"),
                note=str(body.get("note", "")),
            )
        )

    @api.get("/api/preflight")
    async def preflight(_: str = Depends(require_token)) -> list[dict[str, Any]]:
        return [
            {"check": c.name, "result": c.mark, "required": c.required, "detail": c.detail}
            for c in app.control.preflight()
        ]

    @api.get("/api/audit")
    def audit_log(
        kind: str | None = None, subject: str | None = None, limit: int = 200, _: str = Depends(require_token)
    ) -> list[dict[str, Any]]:
        return app.control.audit_events(kind, subject, limit)

    @api.get("/api/incidents")
    async def incidents(all: bool = False, _: str = Depends(require_token)) -> list[dict[str, Any]]:
        return app.control.incidents(open_only=not all)

    @api.post("/api/lanes/{channel}/resume")
    async def resume_lane(
        channel: str, body: dict[str, Any] = Body(default={}), who: str = Depends(require_token)
    ) -> dict[str, Any]:
        released = app.control.resume_lane(_channel(channel), by=who, note=str(body.get("note", "")))
        return {"channel": channel.upper(), "released_actions": released}

    @api.post("/api/lanes/{channel}/halt")
    async def halt_lane(
        channel: str, body: dict[str, Any] = Body(default={}), who: str = Depends(require_token)
    ) -> dict[str, Any]:
        app.control.halt_lane(_channel(channel), by=who, reason=str(body.get("reason", "manual halt")))
        return {"channel": channel.upper(), "state": "HALTED"}

    @api.get("/api/conversations")
    async def conversations(paused: bool = False, _: str = Depends(require_token)) -> list[dict[str, Any]]:
        return app.control.conversations(paused_only=paused)

    @api.get("/api/conversations/{ref}/messages")
    async def conversation_messages(ref: str, _: str = Depends(require_token)) -> list[dict[str, Any]]:
        return guarded(lambda: app.control.conversation_messages(ref))

    @api.post("/api/conversations/{ref}/claim")
    async def claim(ref: str, who: str = Depends(require_token)) -> dict[str, Any]:
        return guarded(lambda: app.control.claim_conversation(ref, by=who))

    @api.post("/api/conversations/{ref}/release")
    async def release(ref: str, who: str = Depends(require_token)) -> dict[str, Any]:
        return guarded(lambda: app.control.release_conversation(ref, by=who))

    @api.get("/api/suppressions")
    async def suppressions(_: str = Depends(require_token)) -> list[dict[str, Any]]:
        return app.control.suppressions()

    @api.post("/api/suppressions")
    async def suppress(body: dict[str, Any] = Body(...), who: str = Depends(require_token)) -> dict[str, Any]:
        kind = SuppressionKind(str(body.get("kind", "USERNAME")).upper())
        created = guarded(
            lambda: app.control.suppress(kind, str(body["value"]), str(body.get("reason", "manual")), by=who)
        )
        return {"created": created}

    @api.delete("/api/suppressions/{kind}/{value}")
    async def unsuppress(kind: str, value: str, who: str = Depends(require_token)) -> dict[str, Any]:
        return {"removed": app.control.unsuppress(SuppressionKind(kind.upper()), value, by=who)}

    @api.post("/api/tick")
    async def tick(_: str = Depends(require_token)) -> dict[str, Any]:
        return (await app.orchestrator.tick()).as_dict()

    @api.get("/api/evidence/{path:path}")
    def evidence(path: str, _: str = Depends(require_token)) -> FileResponse:
        root = Path(settings.browser.evidence_dir).resolve()
        candidate = Path(path)
        # Evidence paths are recorded as given (often relative to the working dir).
        target = (candidate if candidate.is_absolute() or candidate.exists() else root / candidate).resolve()
        if root not in target.parents or not target.is_file():  # never serve outside the evidence dir
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target)

    return api


@dataclass
class BackgroundServer:
    """The control plane served from the caller's event loop (``demo --watch``)."""

    server: Any  # uvicorn.Server
    task: asyncio.Task[None]
    url: str

    @classmethod
    async def start(cls, app: App, host: str = "127.0.0.1", port: int = 8765) -> BackgroundServer:
        import uvicorn

        with socket.socket() as probe:  # a clear message instead of uvicorn exiting the process
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                raise ControlError(f"cannot serve on {host}:{port} ({exc.strerror}); choose another --port") from exc
        config = uvicorn.Config(
            create_api(app), host=host, port=port, log_level="warning", lifespan="off", timeout_graceful_shutdown=2
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()
                raise ControlError(f"the dashboard server did not start on {host}:{port}")
            await asyncio.sleep(0.05)
        return cls(server, task, f"http://{host}:{port}")

    async def wait(self) -> None:
        """Until the server stops (Ctrl+C)."""
        await self.task

    async def stop(self) -> None:
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


def _channel(raw: str) -> Channel:
    try:
        return Channel(raw.upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="channel must be API or BROWSER") from exc
