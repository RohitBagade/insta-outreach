"""``python -m voice_agent``: serve the voice agent on 127.0.0.1 and open it in the browser."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import socket
import sys
import webbrowser

import uvicorn

from insta_outreach.config import load_dotenv
from voice_agent.server import REPO_ROOT, Agent, Settings, create_app

HOST = "127.0.0.1"  # never another interface: the page can spend the Groq quota


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # never crash on a character the terminal cannot show
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="python -m voice_agent", description="Talk out loud with an AI on Groq.")
    parser.add_argument("--port", type=int, default=8770, help="port on 127.0.0.1 (default 8770)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in the browser")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    env_file = REPO_ROOT / ".env"
    load_dotenv(env_file)  # variables already set in the environment win
    settings = Settings.from_env(env_file=env_file)

    if not _port_free(args.port):
        print(f"Port {args.port} is busy. Is the voice agent already running? Or try: --port {args.port + 1}")
        return 1
    app = create_app(settings)
    config = uvicorn.Config(app, host=HOST, port=args.port, log_level="warning", timeout_graceful_shutdown=2)
    print(f"Starting {settings.agent_name}...")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(uvicorn.Server(config), app.state.agent, f"http://{HOST}:{args.port}", not args.no_browser))
    print("Stopped.")
    return 0


async def _serve(server: uvicorn.Server, agent: Agent, url: str, open_browser: bool) -> None:
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():  # could not start; uvicorn has said why
            await task
            return
        await asyncio.sleep(0.05)
    settings = agent.settings
    if agent.problem is not None:
        print(agent.problem.message)
        print(f"The page at {url} shows the same instruction. Press Ctrl+C to stop.")
    else:
        print(f"Model: {agent.model} (speech to text: {settings.stt_model})")
        print(f"Talk to {settings.agent_name} at {url}  (press Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    await task


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        if os.name != "nt":  # on Windows SO_REUSEADDR would let the probe share a busy port
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
