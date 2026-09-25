from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from insta_outreach.app import App, build_app
from insta_outreach.config import Settings
from insta_outreach.llm import LLMUnavailable, NullLLM
from insta_outreach.util.clock import FakeClock


class ScriptedLLM:
    """Test double for StructuredLLM: a handler decides each structured reply."""

    available = True

    def __init__(self, handler: Callable[[str, list[dict[str, Any]], type], Any]) -> None:
        self.handler = handler
        self.calls: list[str] = []

    async def parse(
        self, *, purpose: str, system: str, content: list[dict[str, Any]], output_model: type, **_: Any
    ) -> Any:
        self.calls.append(purpose)
        reply = self.handler(purpose, content, output_model)
        if reply is None:
            raise LLMUnavailable("scripted: no answer")
        return reply


def candidates_from(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = next(block["text"] for block in content if block["type"] == "text")
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.browser.evidence_dir = tmp_path / "evidence"
    s.browser.profiles_dir = tmp_path / "profiles"
    return s


@pytest.fixture
def make_app(settings: Settings, clock: FakeClock) -> Iterator[Callable[..., App]]:
    created: list[App] = []

    def factory(**kwargs: Any) -> App:
        kwargs.setdefault("llm", NullLLM())
        app = build_app(kwargs.pop("settings", settings), clock=kwargs.pop("clock", clock), **kwargs)
        created.append(app)
        return app

    yield factory
    for app in created:
        app.db.dispose()


async def run_ticks(app: App, clock: FakeClock, n: int, minutes: int = 10) -> None:
    for _ in range(n):
        await app.orchestrator.tick()
        clock.advance(minutes=minutes)


@pytest.fixture(scope="session")
def mock_site() -> Iterator[str]:
    import uvicorn

    from insta_outreach.devtools.mock_instagram import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
