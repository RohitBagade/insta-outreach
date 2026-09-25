"""Command line: setup, running the service, and every human control."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from insta_outreach.config import Settings, default_config_path, load_settings
from insta_outreach.domain.enums import (
    ActionStatus,
    Channel,
    Environment,
    LeadStatus,
    OperatingMode,
    SuppressionKind,
)
from insta_outreach.orchestrator.control import ControlError

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "settings.example.yaml"


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _app(args: argparse.Namespace) -> Any:
    from insta_outreach.app import build_app

    return build_app(load_settings(args.config))


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config or default_config_path())
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_CONFIG.exists():
            shutil.copy(EXAMPLE_CONFIG, target)
            print(f"created {target} from the example (environment: local, mode: OBSERVE)")
        else:
            target.write_text("environment: local\ndefault_mode: OBSERVE\n", encoding="utf-8")
            print(f"created minimal {target}")
    settings = load_settings(target)
    for directory in (settings.data_dir, settings.browser.evidence_dir, settings.browser.profiles_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
    app = _app(args)
    print(f"database ready: {settings.resolved_database_url}")
    _print(app.control.status() | {"limits": "…"})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(_app(args).control.status())
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    app = _app(args)
    if not args.mode:
        print(app.runtime.mode().value)
        return 0
    mode = OperatingMode(args.mode.upper())
    if mode is OperatingMode.AUTONOMOUS and app.settings.environment is Environment.LIVE and not args.confirm:
        answer = input("Switch the LIVE account to AUTONOMOUS (sends eligible outreach within limits)? [y/N] ")
        if answer.strip().lower() != "y":
            print("unchanged")
            return 1
    _print(app.control.set_mode(mode, by="cli", confirm=True))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    app = _app(args)
    app.control.set_paused(args.state == "on", by="cli")
    print(f"global pause: {app.runtime.paused()}")
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    rows = _app(args).control.list_actions([ActionStatus.PENDING_APPROVAL, ActionStatus.DRAFTED], args.limit)
    for row in rows:
        print(f"{row['id']}  {row['status']:<16} {row['type']:<15} @{row['target_username']}")
        print(f"    {row['message']}\n")
    if not rows:
        print("approval queue is empty")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    _print(_app(args).control.approve(args.action_id, by="cli", edited_text=args.message))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    _print(_app(args).control.reject(args.action_id, by="cli", reason=args.reason, redraft=args.redraft))
    return 0


def cmd_leads(args: argparse.Namespace) -> int:
    statuses = [LeadStatus(s.upper()) for s in args.status] if args.status else None
    for lead in _app(args).control.leads(statuses, args.limit):
        opportunities = ",".join(o["type"] for o in lead["opportunities"] or [])
        print(
            f"{lead['username']:<32} {lead['score']!s:>4} {lead['status']:<16} {opportunities:<40} "
            f"{(lead['status_reason'] or '')[:70]}"
        )
    return 0


def cmd_lead(args: argparse.Namespace) -> int:
    _print(_app(args).control.lead_detail(args.ref))
    return 0


def cmd_incidents(args: argparse.Namespace) -> int:
    _print(_app(args).control.incidents(open_only=not args.all))
    return 0


def cmd_lane(args: argparse.Namespace) -> int:
    app = _app(args)
    channel = Channel(args.channel.upper())
    if args.lane_action == "resume":
        print(f"lane resumed; {app.control.resume_lane(channel, by='cli', note=args.note)} parked actions released")
    else:
        app.control.halt_lane(channel, by="cli", reason=args.note or "manual halt")
        print("lane halted")
    return 0


def cmd_conversations(args: argparse.Namespace) -> int:
    _print(_app(args).control.conversations(paused_only=args.paused))
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    _print(_app(args).control.claim_conversation(args.ref, by="cli"))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    _print(_app(args).control.release_conversation(args.ref, by="cli"))
    return 0


def cmd_suppress(args: argparse.Namespace) -> int:
    created = _app(args).control.suppress(SuppressionKind(args.kind.upper()), args.value, args.reason, by="cli")
    print("suppressed" if created else "already suppressed")
    return 0


def cmd_unsuppress(args: argparse.Namespace) -> int:
    removed = _app(args).control.unsuppress(SuppressionKind(args.kind.upper()), args.value)
    print("removed" if removed else "not found")
    return 0


def cmd_limits(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.clear:
        app.runtime.clear_limit_overrides("cli")
    if args.set:
        import yaml

        overrides = {}
        for item in args.set:
            key, _, raw = item.partition("=")
            overrides[key.strip()] = yaml.safe_load(raw)
        app.runtime.set_limit_overrides(overrides, "cli")
    _print(app.runtime.limits().model_dump())
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    app = _app(args)

    async def run() -> None:
        for _ in range(args.n):
            report = await app.orchestrator.tick()
            _print({k: v for k, v in report.as_dict().items() if v})
        await app.close()

    asyncio.run(run())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from insta_outreach.app import build_app, describe

    settings = load_settings(args.config)
    app = build_app(settings)
    print(json.dumps(describe(app) | {"mode": app.runtime.mode().value}, indent=2))
    if settings.environment is Environment.LIVE and app.runtime.mode() is OperatingMode.AUTONOMOUS:
        print("WARNING: LIVE + AUTONOMOUS: eligible outreach will be sent automatically within limits.")
    if args.no_api:

        async def loop() -> None:
            stop = asyncio.Event()
            try:
                await app.orchestrator.run_forever(stop)
            finally:
                await app.close()

        asyncio.run(loop())
        return 0
    import uvicorn

    from insta_outreach.api.server import create_api

    uvicorn.run(
        create_api(app, run_orchestrator=True),
        host=args.host or settings.control_api.host,
        port=args.port or settings.control_api.port,
        log_level="info",
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from insta_outreach.api.server import create_api

    settings = load_settings(args.config)
    app = _app(args)
    uvicorn.run(
        create_api(app, run_orchestrator=False),
        host=args.host or settings.control_api.host,
        port=args.port or settings.control_api.port,
        log_level="info",
    )
    return 0


def cmd_browser(args: argparse.Namespace) -> int:
    from insta_outreach.execution.browser.tools import interactive_login, probe

    settings = load_settings(args.config)
    if args.browser_action == "login":
        print(asyncio.run(interactive_login(settings)))
        return 0
    from insta_outreach.llm import AnthropicLLM
    from insta_outreach.storage.db import Database
    from insta_outreach.util.clock import SystemClock

    report = asyncio.run(
        probe(
            settings,
            SystemClock(),
            AnthropicLLM(settings.llm),
            Database(settings.resolved_database_url),
            args.target,
            args.query,
        )
    )
    _print(report)
    return 0


def cmd_mock_site(args: argparse.Namespace) -> int:
    import uvicorn

    from insta_outreach.devtools.mock_instagram import create_app

    print(f"mock Instagram on http://127.0.0.1:{args.port} (point browser.base_url here; allowed_hosts: [127.0.0.1])")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Accelerated multi-day run against the simulated world (no Instagram, no network)."""
    from insta_outreach.app import build_app
    from insta_outreach.llm import NullLLM
    from insta_outreach.util.clock import FakeClock

    data_dir = Path(args.data_dir)
    shutil.rmtree(data_dir, ignore_errors=True)
    settings = Settings(data_dir=data_dir)
    settings.browser.evidence_dir = data_dir / "evidence"
    clock = FakeClock()
    llm = None if args.use_llm else NullLLM()
    app = build_app(settings, clock=clock, llm=llm)
    world = app.world
    assert world is not None
    app.runtime.set_mode(OperatingMode(args.mode.upper()), "demo")

    async def run() -> None:
        if args.commenter:
            world.comment_on_our_post(args.commenter, "This looks amazing! 😍")
            print(f"day 1: @{args.commenter} commented on one of our posts")
        for day in range(1, args.days + 1):
            if day == 2 and args.checkpoint:
                world.faults.checkpoint_after_browser_ops = world.browser_ops + 2
            if day == 2 and args.human:
                world.human_sends(args.human, "Hey! Rohit here personally, following up myself.")
            for _ in range(66):  # ~11 hours of 10-minute ticks
                await app.orchestrator.tick()
                clock.advance(minutes=10)
            if day == 2 and args.checkpoint:
                incidents = app.control.incidents()
                print(f"day {day}: {len(incidents)} open incident(s): " + "; ".join(i["title"] for i in incidents))
                world.faults.checkpoint_after_browser_ops = None
                released = app.control.resume_lane(Channel.BROWSER, by="demo", note="checkpoint cleared by Rohit")
                print(
                    f"day {day}: Rohit cleared the checkpoint and resumed the browser lane "
                    f"({released} parked action(s) released)"
                )
            clock.advance(hours=13)
        status = app.control.status()
        sends = [a for a in app.control.list_actions(limit=500) if a["type"].startswith("SEND")]
        print("\n=== demo summary ===")
        _print(
            {
                "mode": status["mode"],
                "leads": status["leads"],
                "open_incidents": status["open_incidents"],
                "paused_conversations": status["paused_conversations"],
                "sends": dict(Counter(f"{a['type']}:{a['status']}" for a in sends)),
                "sent_via": dict(
                    Counter(f"{a['capability']}:{a['executed_channel']}" for a in sends if a["status"] == "SUCCEEDED")
                ),
            }
        )
        print("\nsample first messages:")
        for action in [a for a in sends if a["type"] == "SEND_OUTREACH" and a["status"] == "SUCCEEDED"][:3]:
            print(f"  @{action['target_username']}: {action['message']}\n")
        print("conversations now owned by the human:")
        for conv in app.control.conversations(paused_only=True):
            print(f"  @{conv['peer_username']}: {conv['paused_reason']}")
        await app.close()

    asyncio.run(run())
    print(f"\ndatabase: {settings.resolved_database_url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insta-outreach", description="LemmeDeliver Instagram outreach orchestrator")
    parser.add_argument("--config", help="settings YAML (default: $INSTA_OUTREACH_CONFIG or config/settings.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config, data dirs and database").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="mode, lanes, counters").set_defaults(fn=cmd_status)
    p = sub.add_parser("mode", help="show or set the runtime operating mode")
    p.add_argument(
        "mode", nargs="?", choices=[m.value for m in OperatingMode] + [m.value.lower() for m in OperatingMode]
    )
    p.add_argument("--confirm", action="store_true", help="skip the LIVE+AUTONOMOUS confirmation prompt")
    p.set_defaults(fn=cmd_mode)
    p = sub.add_parser("pause", help="global kill switch for all executor activity")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(fn=cmd_pause)
    p = sub.add_parser("run", help="run orchestrator + control plane/webhooks")
    p.add_argument("--no-api", action="store_true")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("serve", help="control plane + webhooks only (orchestrator runs elsewhere)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("tick", help="run N orchestrator ticks now")
    p.add_argument("-n", type=int, default=1)
    p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("demo", help="accelerated multi-day simulation (safe: no Instagram)")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--mode", default="AUTONOMOUS")
    p.add_argument("--checkpoint", action="store_true", help="inject a security checkpoint on day 2")
    p.add_argument("--human", default="sim.the.brew.room", help="prospect Rohit messages manually on day 2")
    p.add_argument("--commenter", default="sim.smileline.dental", help="account that comments on our post on day 1")
    p.add_argument("--use-llm", action="store_true", help="use Claude for messages if credentials exist")
    p.add_argument("--data-dir", default="data/demo")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("approvals", help="list outreach awaiting approval")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_approvals)
    p = sub.add_parser("approve", help="approve an action (optionally with edited text)")
    p.add_argument("action_id")
    p.add_argument("--message")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("reject", help="reject an action")
    p.add_argument("action_id")
    p.add_argument("--reason", default="")
    p.add_argument("--redraft", action="store_true", help="put the lead back for a fresh draft")
    p.set_defaults(fn=cmd_reject)
    p = sub.add_parser("leads", help="list leads")
    p.add_argument("--status", action="append")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_leads)
    p = sub.add_parser("lead", help="lead detail (id or username)")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_lead)
    p = sub.add_parser("incidents", help="barriers/anomalies needing a human")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_incidents)
    p = sub.add_parser("lane", help="resume or halt an execution lane")
    p.add_argument("lane_action", choices=["resume", "halt"])
    p.add_argument("channel", choices=["api", "browser", "API", "BROWSER"])
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_lane)
    p = sub.add_parser("conversations", help="list conversations")
    p.add_argument("--paused", action="store_true")
    p.set_defaults(fn=cmd_conversations)
    p = sub.add_parser("claim", help="take a conversation over manually (pauses automation)")
    p.add_argument("ref", help="conversation id or @username")
    p.set_defaults(fn=cmd_claim)
    p = sub.add_parser("release", help="hand a conversation back to automation")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("suppress", help="never contact this prospect")
    p.add_argument("value")
    p.add_argument("--kind", default="USERNAME", choices=[k.value for k in SuppressionKind])
    p.add_argument("--reason", default="manual")
    p.set_defaults(fn=cmd_suppress)
    p = sub.add_parser("unsuppress")
    p.add_argument("value")
    p.add_argument("--kind", default="USERNAME", choices=[k.value for k in SuppressionKind])
    p.set_defaults(fn=cmd_unsuppress)
    p = sub.add_parser("limits", help="show / override runtime limits")
    p.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(fn=cmd_limits)
    p = sub.add_parser("browser", help="browser lane tools")
    p.add_argument("browser_action", choices=["login", "probe"])
    p.add_argument("--target", help="probe: a profile to inspect read-only")
    p.add_argument("--query", help="probe: a search query to run read-only")
    p.set_defaults(fn=cmd_browser)
    p = sub.add_parser("mock-site", help="run the mock Instagram UI for local browser demos")
    p.add_argument("--port", type=int, default=8899)
    p.set_defaults(fn=cmd_mock_site)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return int(args.fn(args) or 0)
    except ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
