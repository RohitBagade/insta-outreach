"""Thin wrapper around the Anthropic SDK for structured (JSON) outputs.

Used for three bounded jobs: writing outreach text from a fact sheet,
classifying an unexpected browser page into a fixed set of states, and
picking one element index for a fixed UI intent. It never decides targets.

The system works without credentials: callers check ``available`` and fall
back to templates / deterministic detectors.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from insta_outreach.config import LLMSettings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(Exception):
    pass


class LLMRefused(Exception):
    pass


class StructuredLLM(Protocol):
    @property
    def available(self) -> bool: ...

    async def parse(
        self,
        *,
        purpose: str,
        system: str,
        content: list[dict[str, Any]],
        output_model: type[T],
        vision: bool = False,
        effort: str | None = None,
        max_tokens: int = 2048,
    ) -> T: ...


class NullLLM:
    available = False

    async def parse(self, **_: Any) -> Any:
        raise LLMUnavailable("no LLM configured")


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` profiles live here; the SDK resolves them itself.
    config_dir = os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(config_dir) and any(os.scandir(config_dir))


class AnthropicLLM:
    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings
        self._calls: deque[float] = deque()
        self._disabled_until = 0.0
        self._client: Any = None
        if settings.enabled and _has_credentials():
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic(timeout=settings.timeout_seconds, max_retries=2)
            except Exception as exc:  # construction must never break the app
                log.warning("Anthropic client unavailable: %s", exc)

    @property
    def available(self) -> bool:
        return self._client is not None and time.monotonic() >= self._disabled_until

    def _take_budget(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 3600:
            self._calls.popleft()
        if len(self._calls) >= self._settings.max_calls_per_hour:
            raise LLMUnavailable("hourly LLM call budget exhausted")
        self._calls.append(now)

    async def parse(
        self,
        *,
        purpose: str,
        system: str,
        content: list[dict[str, Any]],
        output_model: type[T],
        vision: bool = False,
        effort: str | None = None,
        max_tokens: int = 2048,
    ) -> T:
        if not self.available:
            raise LLMUnavailable("LLM not available")
        import anthropic

        self._take_budget()
        model = self._settings.vision_model if vision else self._settings.model
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "output_format": output_model,
        }
        if effort:
            request["output_config"] = {"effort": effort}
        try:
            if self._settings.use_refusal_fallbacks:
                response = await self._client.beta.messages.parse(
                    betas=[REFUSAL_FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.parse(**request)
        except anthropic.AuthenticationError as exc:
            self._disabled_until = time.monotonic() + 3600
            raise LLMUnavailable(f"authentication failed: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            self._disabled_until = time.monotonic() + 3600
            raise LLMUnavailable(f"permission denied: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"rate limited: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"API error {exc.status_code}: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMRefused(f"{purpose}: model declined")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMUnavailable(f"{purpose}: no structured output (stop_reason={response.stop_reason})")
        return parsed
