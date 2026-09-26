"""The three Groq calls the voice agent makes, over Groq's OpenAI-compatible HTTP API.

Plain ``httpx``, no SDK. Every failure becomes a :class:`Problem` whose message
is written for the user and safe to show or speak. Groq's own error bodies are
never passed on, because they can quote parts of the request.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
# Fast, capable chat models, best first. Groq's live model list has the final say.
PREFERRED_MODELS = ("openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant")
_NOT_CHAT = ("whisper", "tts", "guard", "embed", "orpheus", "playai")  # speech, safety and embedding models
REPLY_TOKENS = 300
REASONING_TOKENS = 700  # reasoning models spend part of their budget thinking before they answer
TEMPERATURE = 0.7
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_BUSY = "Groq is busy, try again in a moment."

# Whisper reports the language by name; the page picks a voice by ISO 639-1 code.
_LANGUAGE_CODES = {
    "english": "en", "hindi": "hi", "marathi": "mr", "gujarati": "gu", "bengali": "bn", "punjabi": "pa",
    "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml", "urdu": "ur", "nepali": "ne",
    "sanskrit": "sa", "sinhala": "si", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "dutch": "nl", "russian": "ru", "arabic": "ar", "turkish": "tr", "japanese": "ja",
    "korean": "ko", "chinese": "zh", "indonesian": "id",
}  # fmt: skip


class Problem(Exception):
    """Something went wrong that the user should hear about, phrased for them."""

    def __init__(self, code: str, message: str, status: int = 502, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, "retry_after": self.retry_after}


def is_chat_model(model_id: str) -> bool:
    name = model_id.lower()
    return not any(word in name for word in _NOT_CHAT)


def choose_model(available: Iterable[str], wanted: str | None = None) -> tuple[str | None, str | None]:
    """The chat model to use, and a warning when the ``wanted`` one is not available.

    ``wanted`` wins when Groq lists it; otherwise the first of :data:`PREFERRED_MODELS`
    that Groq lists, otherwise the first chat model in Groq's list.
    """
    chat = [m for m in dict.fromkeys(available) if is_chat_model(m)]
    if wanted and wanted in chat:
        return wanted, None
    choice = next((m for m in PREFERRED_MODELS if m in chat), chat[0] if chat else None)
    warning = f"GROQ_MODEL={wanted} is not available to your Groq key; using {choice} instead." if wanted else None
    return choice, warning


def language_code(value: object) -> str | None:
    """``"English"`` / ``"english"`` / ``"en"`` -> ``"en"``; ``None`` when unknown."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if name in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[name]
    return name if 2 <= len(name) <= 3 and name.isalpha() else None


def chat_payload(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    """A streamed chat request with a short reply budget, tuned per model family."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": TEMPERATURE,
        "max_completion_tokens": REPLY_TOKENS,
    }
    family = model.lower()
    if family.startswith("openai/gpt-oss"):  # always reasons first: keep it brief and give it room
        payload["reasoning_effort"] = "low"
        payload["max_completion_tokens"] = REPLY_TOKENS + REASONING_TOKENS
    elif family.startswith("qwen/qwen3"):  # would otherwise think out loud, and that would be spoken
        payload["reasoning_effort"] = "none"
    return payload


class GroqClient:
    """An authenticated client for one Groq API key. The key only ever goes into the request header."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        key_help: str = "Check GROQ_API_KEY in the .env file.",
    ) -> None:
        self._key_help = key_help
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> list[str]:
        """IDs of the models this key can use right now."""
        response = await self._send(self._http.build_request("GET", "/models"))
        data = _json(response).get("data")
        if not isinstance(data, list):
            raise Problem("groq_error", "Groq sent an unexpected model list.")
        return [
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("active") is not False
        ]

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str, model: str) -> dict[str, Any]:
        """``{text, language, no_speech_prob, duration}`` for one recorded turn."""
        request = self._http.build_request(
            "POST",
            "/audio/transcriptions",
            data={"model": model, "response_format": "verbose_json", "temperature": "0"},
            files={"file": (filename, audio, content_type)},
        )
        body = _json(await self._send(request))
        segments = body.get("segments")
        no_speech = [
            float(s["no_speech_prob"])
            for s in (segments if isinstance(segments, list) else [])
            if isinstance(s, dict) and isinstance(s.get("no_speech_prob"), int | float)
        ]
        duration = body.get("duration")
        return {
            "text": str(body.get("text") or "").strip(),
            "language": language_code(body.get("language")),
            # Whisper's own guess that this was not speech at all (lowest over the segments).
            "no_speech_prob": round(min(no_speech), 3) if no_speech else None,
            "duration": duration if isinstance(duration, int | float) else None,
        }

    async def open_chat(self, payload: dict[str, Any]) -> ChatStream:
        """Starts a streamed completion. Errors before the first byte raise here, as a :class:`Problem`."""
        request = self._http.build_request("POST", "/chat/completions", json=payload)
        return ChatStream(await self._send(request, stream=True))

    async def _send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        try:
            response = await self._http.send(request, stream=stream)
        except httpx.TimeoutException:
            raise Problem("timeout", "Groq took too long to answer. Try again.", 504) from None
        except httpx.HTTPError as exc:  # DNS, refused connection, TLS, proxy... (only the type is logged)
            log.warning("Groq request failed: %s", type(exc).__name__)
            raise Problem("network", "Can't reach Groq. Check your internet connection.", 502) from None
        if response.status_code >= 400:
            try:
                body = await response.aread()
            except httpx.HTTPError:
                body = b""
            finally:
                await response.aclose()
            raise _problem_for(response.status_code, response.headers, body, self._key_help)
        return response


class ChatStream:
    """An open streamed completion: :meth:`deltas` yields the reply text as Groq sends it."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.finish_reason: str | None = None

    async def deltas(self) -> AsyncIterator[str]:
        try:
            async for line in self._response.aiter_lines():
                if not line.startswith("data:"):
                    continue  # event names, comments, keep-alives, blank separators
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                error = chunk.get("error") or _field(chunk.get("x_groq"), "error")
                if error:
                    raise _stream_problem(error)
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                        continue
                    text = _field(choice.get("delta"), "content")  # ignores any "reasoning" deltas
                    if isinstance(text, str) and text:
                        yield text
                    if choice.get("finish_reason"):
                        self.finish_reason = str(choice["finish_reason"])
        except httpx.TimeoutException:
            raise Problem("timeout", "Groq went quiet in the middle of the answer. Try again.", 504) from None
        except httpx.HTTPError:
            raise Problem("network", "The connection to Groq dropped. Try again.", 502) from None

    async def aclose(self) -> None:
        await self._response.aclose()

    @property
    def closed(self) -> bool:
        return self._response.is_closed


def _field(obj: object, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise Problem("groq_error", "Groq sent an answer I couldn't read. Try again.")
    return body


def _retry_after(headers: httpx.Headers) -> int | None:
    for name, scale in (("retry-after", 1.0), ("retry-after-ms", 0.001)):
        try:
            seconds = float(headers.get(name, "")) * scale
        except ValueError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return max(1, math.ceil(seconds))
    return None


def _error_fields(body: bytes) -> tuple[str, str]:
    """``(code, type)`` from a Groq error body. Only these identifiers are read, never the message."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return "", ""
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code") or ""), str(error.get("type") or "")


def _problem_for(status: int, headers: httpx.Headers, body: bytes, key_help: str) -> Problem:
    code, kind = _error_fields(body)
    if status == 401:
        return Problem("bad_key", f"Groq rejected the API key. {key_help}", 401)
    if status == 429 or kind == "rate_limit_error":
        wait = _retry_after(headers)
        return Problem("rate_limited", f"Groq is busy, try again in {wait} s." if wait else _BUSY, 429, wait)
    if status == 404 or code in ("model_not_found", "model_decommissioned"):
        return Problem("model_unavailable", "That Groq model is not available any more.", 502)
    if status == 413 or code == "request_too_large":
        return Problem("too_large", "That recording is too long for Groq. Try a shorter turn.", 413)
    if status >= 500 or status == 498:  # 498: Groq's capacity-exceeded status
        return Problem("groq_down", "Groq is having trouble right now. Try again in a moment.", 502)
    return Problem("groq_error", f"Groq could not handle that request (error {status}).", 502)


def _stream_problem(error: object) -> Problem:
    if _field(error, "type") == "rate_limit_error":
        return Problem("rate_limited", _BUSY, 429)
    return Problem("groq_error", "Groq stopped in the middle of the answer. Try again.", 502)
