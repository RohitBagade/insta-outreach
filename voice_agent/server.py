"""The voice agent's local web server: the page, speech-to-text, and a streaming chat relay.

It binds to 127.0.0.1 and keeps the Groq key to itself. The page only ever talks
to this server, and no response, error message or log line contains the key.

``/`` serves a static page (``static/``) under a strict Content-Security-Policy.
The page records a turn, posts it to ``/api/transcribe``, then streams the reply
from ``/api/chat`` and speaks it sentence by sentence while it arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from voice_agent.groq import (
    DEFAULT_BASE_URL,
    DEFAULT_STT_MODEL,
    PREFERRED_MODELS,
    ChatStream,
    GroqClient,
    Problem,
    chat_payload,
    choose_model,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {  # the only files ever served from disk, besides index.html at /
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "ticker.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
AUDIO_TYPES = {  # upload media type -> the file extension Groq goes by
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
}  # fmt: skip
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_HISTORY = 20  # messages sent to the model, besides the system prompt
MAX_MESSAGE_CHARS = 2000
RECHECK_SECONDS = 15.0  # how soon to ask Groq again when it was unreachable
DEFAULT_NAME = "Kabir"

DEFAULT_PERSONA = (
    "You are {name}, a warm, witty and genuinely helpful voice assistant. You are talking out loud with Rohit, "
    "the founder of LemmeDeliver, a studio in Mumbai that builds websites for local businesses.\n"
    "\n"
    "Everything you say is read aloud by a speech synthesizer, so:\n"
    "- Talk like a friend on a call: short, natural sentences, usually one to three of them. "
    "Say more only when Rohit asks for detail.\n"
    "- Never use markdown, lists, headings, emoji, code blocks or links. "
    "Say numbers, symbols and abbreviations the way a person would say them.\n"
    "- Reply in the language Rohit speaks: English, Hindi or Hinglish. Match his mix.\n"
    '- You are a man: in Hindi, use masculine forms, like "main samajh gaya".\n'
    "- If what he said seems cut off or unclear, ask him briefly to say it again.\n"
    "- If you don't know something, say so. You can't browse the web, see his screen or remember "
    "earlier conversations."
)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"microphone=(self), camera=(), geolocation=()"),
)
_LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
_NO_ANSWER = Problem("no_answer", "I lost my train of thought there. Could you say that again?", 502)


@dataclass(frozen=True)
class Settings:
    """Everything the agent needs, normally read from the environment (and so from ``.env``)."""

    api_key: str = field(default="", repr=False)
    model: str = ""  # GROQ_MODEL; empty: picked from Groq's live model list
    stt_model: str = DEFAULT_STT_MODEL
    agent_name: str = DEFAULT_NAME
    persona: str = ""  # VOICE_AGENT_PROMPT; empty: DEFAULT_PERSONA
    env_file: Path = REPO_ROOT / ".env"
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def get(name: str) -> str:
            return env.get(name, "").strip()

        return cls(
            api_key=get("GROQ_API_KEY"),
            model=get("GROQ_MODEL"),
            stt_model=get("GROQ_STT_MODEL") or DEFAULT_STT_MODEL,
            agent_name=get("VOICE_AGENT_NAME") or DEFAULT_NAME,
            persona=get("VOICE_AGENT_PROMPT").replace("\\n", "\n"),  # .env values are one line
            env_file=env_file or REPO_ROOT / ".env",
        )

    @property
    def key_instruction(self) -> str:
        return f"Add GROQ_API_KEY=... to the .env file in {self.env_file.parent}, then run again."


def system_prompt(settings: Settings, now: datetime | None = None) -> str:
    """The persona, always first and always from the server: the page cannot replace it."""
    persona = (settings.persona or DEFAULT_PERSONA).replace("{name}", settings.agent_name)
    now = now or datetime.now().astimezone()
    return f"{persona}\n\nRight now it is {now:%A, %d %B %Y, %I:%M %p} where Rohit is."


def greeting(name: str, now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        opener = "Good morning, Rohit!"
    elif 12 <= hour < 17:
        opener = "Good afternoon, Rohit!"
    elif 17 <= hour < 23:
        opener = "Good evening, Rohit!"
    else:
        opener = "Hey Rohit, up late?"
    return f"{opener} {name} here. What's on your mind?"


def conversation(messages: object) -> list[dict[str, str]]:
    """The recent turns from the page, as plain user/assistant text. Any other role is dropped."""
    if not isinstance(messages, list):
        raise Problem("bad_request", 'Send the conversation as {"messages": [...]}.', 400)
    turns = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role, content = item.get("role"), item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()[:MAX_MESSAGE_CHARS]
        if text:
            turns.append({"role": role, "content": text})
    turns = turns[-MAX_HISTORY:]
    if not turns or turns[-1]["role"] != "user":
        raise Problem("nothing_to_answer", "There's nothing new to answer yet.", 400)
    return turns


class Agent:
    """What the endpoints share: the Groq client, the chosen model, and whether the key works."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.groq: GroqClient | None = None
        self.model: str | None = None
        self.key_accepted: bool | None = None  # None: not confirmed yet (no key, or Groq unreachable)
        self.problem: Problem | None = None if settings.api_key else Problem("no_key", settings.key_instruction, 503)
        self._transport = transport
        self._refused: set[str] = set()  # models Groq said are gone
        self._checked_at = -math.inf
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.settings.api_key:
            self.groq = GroqClient(
                self.settings.api_key,
                base_url=self.settings.base_url,
                transport=self._transport,
                key_help=self.settings.key_instruction,
            )
            await self.discover()

    async def aclose(self) -> None:
        if self.groq is not None:
            await self.groq.aclose()
            self.groq = None

    async def discover(self) -> None:
        """Checks the key and picks the chat model from Groq's live list. Never raises."""
        if self.groq is None:
            return
        self._checked_at = time.monotonic()
        try:
            listed = await self.groq.list_models()
        except Problem as exc:
            if exc.code == "bad_key":  # said once, by whoever started the agent (python -m voice_agent)
                self.key_accepted, self.problem = False, exc
                return
            fallback = [m for m in (self.model, self.settings.model, *PREFERRED_MODELS) if m and m not in self._refused]
            self.model = fallback[0] if fallback else None
            log.warning("Could not list the Groq models (%s) Trying %s for now.", exc.message, self.model)
            return
        self.key_accepted = True
        self.model, warning = choose_model([m for m in listed if m not in self._refused], self.settings.model)
        if warning:
            log.warning(warning)
        self.problem = None if self.model else Problem("no_model", "Your Groq key has no chat model available.", 503)

    async def ready(self) -> tuple[GroqClient, str]:
        """The client and model for the next request. Asks Groq again if it was unreachable before."""
        stale = time.monotonic() - self._checked_at > RECHECK_SECONDS
        if self.groq is not None and self.key_accepted is None and stale:
            async with self._lock:
                if self.key_accepted is None and time.monotonic() - self._checked_at > RECHECK_SECONDS:
                    await self.discover()
        if self.problem is not None:
            raise Problem(self.problem.code, self.problem.message, self.problem.status)
        if self.groq is None or self.model is None:
            raise Problem("not_ready", "The voice agent is still starting. Try again in a moment.", 503)
        return self.groq, self.model

    def note(self, problem: Problem) -> None:
        """Remembers a key rejected mid-session, so the page can say so instead of failing on every turn."""
        if problem.code == "bad_key" and self.key_accepted is not False:
            self.key_accepted, self.problem = False, problem
            log.warning(problem.message)

    async def open_chat(self, history: list[dict[str, str]]) -> ChatStream:
        """A streamed reply. If Groq has dropped the model, switches to the next best one once."""
        groq, model = await self.ready()
        messages = [{"role": "system", "content": system_prompt(self.settings)}, *history]
        try:
            return await groq.open_chat(chat_payload(model, messages))
        except Problem as exc:
            if exc.code != "model_unavailable":
                raise
            self._refused.add(model)
            async with self._lock:
                if self.model == model:
                    await self.discover()
            if self.model is None or self.model in self._refused:
                raise
            log.warning("Groq no longer offers %s; switched to %s.", model, self.model)
        groq, model = await self.ready()
        return await groq.open_chat(chat_payload(model, messages))


class LocalOnly:
    """Answers only this computer's own browser, and adds the security headers to every response.

    Otherwise any web page could post to this port and spend the Groq quota, or
    reach it through DNS rebinding. So every request must name a loopback host,
    and API calls must come from this origin.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        api = str(scope["path"]).startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                raw += [(name, value) for name, value in _SECURITY_HEADERS if name not in present]
                if b"cache-control" not in present:
                    raw.append((b"cache-control", b"no-store" if api else b"no-cache"))
                message["headers"] = raw
            await send(message)

        refusal: Response | None = None
        if _hostname(host) not in _LOOPBACK:
            refusal = PlainTextResponse("The voice agent only answers on http://127.0.0.1.", status_code=421)
        elif api and not _same_origin(headers, host):
            refusal = JSONResponse({"error": "Cross-site requests are not allowed.", "code": "forbidden"}, 403)
        await (refusal or self.app)(scope, receive, send_with_headers)


def _hostname(host: str) -> str:
    host = host.strip().lower()
    return host[: host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]


def _same_origin(headers: Headers, host: str) -> bool:
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    return origin is None or origin == f"http://{host}"


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """The voice agent's web app. ``transport`` stands in for the network to Groq (tests)."""
    agent = Agent(settings, transport)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await agent.start()
        try:
            yield
        finally:
            await agent.aclose()

    app = FastAPI(title="voice agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.agent = agent
    app.add_middleware(LocalOnly)

    @app.exception_handler(Problem)
    async def on_problem(_: Request, exc: Problem) -> JSONResponse:
        agent.note(exc)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(exc.as_dict(), status_code=exc.status, headers=headers)

    @app.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/static/{name}", include_in_schema=False)
    async def static(name: str) -> FileResponse:
        media_type = STATIC_FILES.get(name)
        if media_type is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(STATIC_DIR / name, media_type=media_type)

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        with contextlib.suppress(Problem):
            await agent.ready()  # asks Groq again if it was unreachable at startup
        return {
            "name": settings.agent_name,
            "model": agent.model,
            "stt_model": settings.stt_model,
            "key_configured": bool(settings.api_key),
            "key_accepted": agent.key_accepted,
            "problem": agent.problem.as_dict() if agent.problem else None,
            "greeting": greeting(settings.agent_name),
        }

    @app.post("/api/transcribe")
    async def transcribe(request: Request) -> dict[str, Any]:
        """One recorded turn (the raw audio as the body) -> ``{text, language, no_speech_prob, duration}``."""
        kind = _media_type(request)
        extension = AUDIO_TYPES.get(kind)
        if extension is None:
            raise Problem("bad_audio", "Send the recording as webm, ogg, mp4, mp3 or wav audio.", 415)
        groq, _ = await agent.ready()
        audio = await _read_body(request, MAX_AUDIO_BYTES, "That recording is too long. Try a shorter turn.")
        if not audio:
            raise Problem("empty_audio", "The recording was empty.", 400)
        return await groq.transcribe(audio, filename=f"speech.{extension}", content_type=kind, model=settings.stt_model)

    @app.post("/api/chat")
    async def chat(request: Request) -> StreamingResponse:
        """``{messages: [{role, content}, ...]}`` -> the reply as server-sent events, streamed as Groq writes it."""
        body = await _read_json(request)
        history = conversation(body.get("messages") if isinstance(body, dict) else None)
        stream = await agent.open_chat(history)
        return StreamingResponse(_relay(stream), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app


async def _relay(stream: ChatStream) -> AsyncIterator[str]:
    """Groq's stream re-sent to the page: ``delta`` events as text arrives, then ``done`` or ``error``."""
    answered = False
    try:
        async for text in stream.deltas():
            answered = True
            yield _event(type="delta", text=text)
        if answered:
            yield _event(type="done", finish_reason=stream.finish_reason)
        else:  # e.g. the whole budget went on reasoning
            yield _event(type="error", **_NO_ANSWER.as_dict())
    except Problem as exc:
        yield _event(type="error", **exc.as_dict())
    finally:
        with anyio.CancelScope(shield=True):  # the page hung up: still stop Groq's generation
            await stream.aclose()


def _event(**data: Any) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def _read_body(request: Request, limit: int, too_big: str) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise Problem("too_large", too_big, 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise Problem("too_large", too_big, 413)
    return bytes(body)


async def _read_json(request: Request) -> Any:
    if _media_type(request) != "application/json":
        raise Problem("bad_request", "Send the conversation as JSON.", 415)
    raw = await _read_body(request, MAX_JSON_BYTES, "That conversation is too long.")
    try:
        return json.loads(raw)
    except ValueError:
        raise Problem("bad_request", "That request was not valid JSON.", 400) from None
