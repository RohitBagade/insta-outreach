"""Command line: setup, running the service, and every human control."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from insta_outreach.config import Settings, default_config_path, load_dotenv, load_settings
from insta_outreach.domain.enums import (
    ActionStatus,
    ActionType,
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


def _lines(lines: list[str]) -> None:
    print("\n".join(lines))


def _app(args: argparse.Namespace) -> Any:
    from insta_outreach.app import build_app

    return build_app(load_settings(args.config))


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    app = _prepare(args)
    _print(app.control.status() | {"limits": "…"})
    return 0


def _prepare(args: argparse.Namespace) -> Any:
    """Config file, data directories and database; returns the app."""
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
    return app


def cmd_setup(args: argparse.Namespace) -> int:
    """Prepare this computer: .env with a control token, config, Chromium, optional service file."""
    import subprocess

    from insta_outreach.deploy import current_service_plan, ensure_env_file

    root = Path.cwd()
    if not (root / "pyproject.toml").exists() or not (root / ".env.example").exists():
        print("run this from the insta-outreach folder (where pyproject.toml is)", file=sys.stderr)
        return 2
    created, token = ensure_env_file(root)
    print(f"{'created' if created else 'kept'} .env")
    if token:
        print(f"  CONTROL_API_TOKEN generated and saved in .env: {token}")
        print("  (Mission Control asks for it once per browser tab; keep it private)")
    load_dotenv(root / ".env")
    _prepare(args)
    if not args.no_browser:
        print("installing Playwright's Chromium (first time: ~150 MB)...")
        done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        if done.returncode != 0:
            print("Chromium install failed; on Linux try: python -m playwright install --with-deps chromium")
            return 1
    if args.service:
        plan = current_service_plan(root)
        (root / "data").mkdir(exist_ok=True)
        for path, content in plan.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"wrote {path}")
        print("start it (and at every log-in):")
        for command in plan.start:
            print(f"  {command}")
        print("stop it:")
        for command in plan.stop:
            print(f"  {command}")
        print(f"logs:  {plan.logs}")
    print("next: insta-outreach demo --watch --checkpoint   # Mission Control with simulated data")
    print("      then docs/LIVE_CHECKLIST.md for the real account (browser login is yours to do)")
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
    from insta_outreach.reporting import leads_view

    statuses = [LeadStatus(s.upper()) for s in args.status] if args.status else None
    app = _app(args)
    if args.json:
        _print(app.control.leads(statuses, args.limit))
    else:
        _lines(leads_view(app, statuses, args.limit))
    return 0


def cmd_lead(args: argparse.Namespace) -> int:
    _print(_app(args).control.lead_detail(args.ref))
    return 0


def cmd_incidents(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import incidents_view

    app = _app(args)
    if args.json:
        _print(app.control.incidents(open_only=not args.all))
    else:
        _lines(incidents_view(app, include_resolved=args.all))
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
    from insta_outreach.reporting import conversations_view

    app = _app(args)
    if args.json:
        _print(app.control.conversations(paused_only=args.paused))
    else:
        _lines(conversations_view(app, paused_only=args.paused))
    return 0


def cmd_actions(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import actions_view

    types = [ActionType(t.upper()) for t in args.type] if args.type else None
    statuses = [ActionStatus(s.upper()) for s in args.status] if args.status else None
    _lines(actions_view(_app(args), types, statuses, args.target, args.limit))
    return 0


def cmd_action(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import action_report

    app = _app(args)
    if args.json:
        _print(app.control.action_detail(args.action_id))
    else:
        _lines(action_report(app, args.action_id))
    return 0


_GENERATED = {
    "outreach": [ActionType.SEND_OUTREACH],
    "followup": [ActionType.SEND_FOLLOW_UP],
    "reply": [ActionType.SEND_REPLY],
    "all": [ActionType.SEND_OUTREACH, ActionType.SEND_FOLLOW_UP, ActionType.SEND_REPLY],
}


def cmd_generated(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import outbound_messages_view

    _lines(outbound_messages_view(_app(args), _GENERATED[args.kind], args.limit))
    return 0


def cmd_messages(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import messages_view

    _lines(messages_view(_app(args), args.handle, args.limit, args.full))
    return 0


def cmd_suppressions(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import suppressions_view

    _lines(suppressions_view(_app(args)))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import explain_lead

    _lines(explain_lead(_app(args), args.handle))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import audit_view

    _lines(audit_view(_app(args), args.kind, args.subject, args.limit))
    return 0


def cmd_safety(args: argparse.Namespace) -> int:
    from insta_outreach.reporting import safety_view

    _lines(safety_view(_app(args)))
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    from insta_outreach.orchestrator.readiness import blocking
    from insta_outreach.reporting import preflight_view

    app = _app(args)
    checks = app.control.preflight()
    if args.json:
        _print([{"check": c.name, "result": c.mark, "required": c.required, "detail": c.detail} for c in checks])
    else:
        _lines(preflight_view(checks))
    failures = blocking(checks)
    if app.settings.environment is Environment.LIVE:
        print(
            "\nAUTONOMOUS would be REFUSED: " + ", ".join(c.name for c in failures)
            if failures
            else "\nall required checks pass: AUTONOMOUS may be enabled explicitly (`insta-outreach mode AUTONOMOUS`)"
        )
    return 1 if failures else 0


def cmd_add_lead(args: argparse.Namespace) -> int:
    _print(_app(args).control.add_lead(args.handle, by="cli", campaign_id=args.campaign, note=args.note))
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
    removed = _app(args).control.unsuppress(SuppressionKind(args.kind.upper()), args.value, by="cli")
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
    from insta_outreach.orchestrator.readiness import blocking

    app = _app(args)
    if app.settings.environment is Environment.LIVE and app.runtime.mode() is OperatingMode.AUTONOMOUS:
        failures = blocking(app.control.preflight())
        if failures:
            print(
                "refusing: the live account is set to AUTONOMOUS but preflight fails ("
                + ", ".join(c.name for c in failures)
                + "). Switch down first: `insta-outreach mode APPROVAL`.",
                file=sys.stderr,
            )
            return 2

    async def run() -> None:
        for _ in range(args.n):
            report = await app.orchestrator.tick()
            _print({k: v for k, v in report.as_dict().items() if v})
        await app.close()

    asyncio.run(run())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from insta_outreach.app import build_app, describe
    from insta_outreach.orchestrator.readiness import blocking
    from insta_outreach.reporting import preflight_view

    settings = load_settings(args.config)
    app = build_app(settings)
    print(json.dumps(describe(app) | {"mode": app.runtime.mode().value}, indent=2))
    if settings.environment is Environment.LIVE:
        checks = app.control.preflight()
        _lines(preflight_view(checks))
        failures = blocking(checks)
        if app.runtime.mode() is OperatingMode.AUTONOMOUS:
            if failures:
                print(
                    "REFUSING TO START: the live account is set to AUTONOMOUS but required preflight checks fail ("
                    + ", ".join(c.name for c in failures)
                    + "). Switch down first: `insta-outreach mode APPROVAL`.",
                    file=sys.stderr,
                )
                return 2
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
        ok, message = asyncio.run(interactive_login(settings))
        print(message)
        if ok:
            _runtime(settings).record_browser_session("login", by="cli", detail=message)
        return 0 if ok else 1
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
    session_ok = report.get("session", {}).get("state") == "ok"
    if session_ok:
        _runtime(settings).record_browser_session("probe", by="cli", detail=str(report["session"].get("url")))
    return 0 if session_ok else 1


def _runtime(settings: Settings) -> Any:
    from insta_outreach.runtime import RuntimeControl
    from insta_outreach.storage.db import Database
    from insta_outreach.util.clock import SystemClock

    db = Database(settings.resolved_database_url)
    db.create_all()
    return RuntimeControl(db, settings, SystemClock())


def cmd_alerts(args: argparse.Namespace) -> int:
    """Check phone/webhook alerts: send a test alert, or find your Telegram chat id."""
    from insta_outreach.domain.enums import IncidentSeverity
    from insta_outreach.notify import TelegramNotifier, WebhookNotifier, telegram_chats

    cfg = load_settings(args.config).notifications
    if args.alerts_action == "find-chat":
        if cfg.telegram_bot_token is None:
            print("set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather)", file=sys.stderr)
            return 2
        try:
            chats = asyncio.run(telegram_chats(cfg.telegram_bot_token.get_secret_value()))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not chats:
            print("No messages yet. Open your bot in Telegram, press Start (or send it anything), then run this again.")
            return 1
        for chat_id, name in chats:
            print(f"TELEGRAM_CHAT_ID={chat_id}   # {name}")
        return 0

    title = "Test alert from insta-outreach"
    detail = "If you can read this, alerts reach you. Real alerts: checkpoints, halted lanes, warm leads."
    ok = True
    if cfg.telegram_bot_token is not None and cfg.telegram_chat_id:
        telegram = TelegramNotifier(
            cfg.telegram_bot_token.get_secret_value(), cfg.telegram_chat_id, IncidentSeverity.INFO, cfg.dashboard_url
        )
        asyncio.run(telegram.notify(title, detail, IncidentSeverity.CRITICAL))
        print(f"telegram: {'FAILED - ' + telegram.last_error if telegram.last_error else 'sent'}")
        ok = ok and telegram.last_error is None
    else:
        print("telegram: not configured (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; see docs/DEPLOY.md)")
    if cfg.webhook_url:
        asyncio.run(
            WebhookNotifier(cfg.webhook_url, IncidentSeverity.INFO).notify(title, detail, IncidentSeverity.CRITICAL)
        )
        print("webhook: posted (failures are logged above)")
    else:
        print("webhook: not configured (NOTIFY_WEBHOOK_URL)")
    print(f"alerts at or above {cfg.min_severity.value} are sent; everything is also in the log and the audit trail")
    return 0 if ok else 1


def cmd_mock_site(args: argparse.Namespace) -> int:
    import uvicorn

    from insta_outreach.devtools.mock_instagram import create_app

    print(f"mock Instagram on http://127.0.0.1:{args.port} (point browser.base_url here; allowed_hosts: [127.0.0.1])")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Accelerated multi-day run against the simulated world (no Instagram, no network)."""
    from insta_outreach.verification import run_demo

    tick_seconds = args.tick_seconds if args.tick_seconds is not None else (1.0 if args.watch else 0.0)
    try:
        asyncio.run(
            run_demo(
                Path(args.data_dir),
                days=args.days,
                mode=OperatingMode(args.mode.upper()),
                checkpoint=args.checkpoint,
                rate_limit=args.rate_limit,
                human=args.human or None,
                commenter=args.commenter or None,
                use_llm=args.use_llm,
                watch_port=args.port if args.watch else None,
                tick_seconds=tick_seconds,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_scenario(args: argparse.Namespace) -> int:
    """Deterministic, self-checking scenario; compare with the committed expected transcript."""
    import difflib

    from insta_outreach.verification import EXPECTED_SCENARIO, normalise, run_scenario

    data_dir = Path(args.data_dir)
    lines, failed = asyncio.run(run_scenario(data_dir, echo=not args.check))
    actual = normalise(lines, data_dir)
    if args.out:
        Path(args.out).write_text("\n".join(actual) + "\n", encoding="utf-8")
        print(f"transcript written to {args.out}")
    if args.check:
        expected = EXPECTED_SCENARIO.read_text(encoding="utf-8").splitlines()
        diff = list(difflib.unified_diff(expected, actual, "expected_scenario.txt", "actual", lineterm=""))
        if diff:
            print("\n".join(diff))
            print(f"\nDIFFERENT from {EXPECTED_SCENARIO} ({len(failed)} failed check(s))")
            return 1
        print(f"IDENTICAL to {EXPECTED_SCENARIO}: {lines[-1]}")
    return 1 if failed else 0


def cmd_browser_demo(args: argparse.Namespace) -> int:
    """The real Playwright agent against the local mock Instagram site (needs Chromium)."""
    from insta_outreach.verification import run_browser_demo

    try:
        asyncio.run(run_browser_demo(Path(args.data_dir), headed=args.headed))
    except Exception as exc:  # most likely: no Chromium installed for Playwright
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            print("Chromium for Playwright is missing: run `.venv/bin/playwright install chromium`", file=sys.stderr)
            return 2
        raise
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insta-outreach", description="LemmeDeliver Instagram outreach orchestrator")
    parser.add_argument("--config", help="settings YAML (default: $INSTA_OUTREACH_CONFIG or config/settings.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config, data dirs and database").set_defaults(fn=cmd_init)
    p = sub.add_parser("setup", help="prepare this computer: .env + control token, config, Chromium, service file")
    p.add_argument("--service", action="store_true", help="also write a start-at-log-in service for this OS")
    p.add_argument("--no-browser", action="store_true", help="skip installing Playwright's Chromium")
    p.set_defaults(fn=cmd_setup)
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
    p = sub.add_parser("demo", help="accelerated multi-day simulation, narrated (safe: no Instagram)")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--mode", default="AUTONOMOUS")
    p.add_argument("--checkpoint", action="store_true", help="inject a security checkpoint on day 2")
    p.add_argument("--rate-limit", action="store_true", help="inject an Instagram rate limit on day 3")
    p.add_argument("--human", default="sim.the.brew.room", help="prospect Rohit messages manually on day 2")
    p.add_argument("--commenter", default="sim.smileline.dental", help="account that comments on our post on day 1")
    p.add_argument("--use-llm", action="store_true", help="use Claude for messages if credentials exist")
    p.add_argument("--data-dir", default="data/demo")
    p.add_argument("--watch", action="store_true", help="serve Mission Control while the demo runs (slowed down)")
    p.add_argument("--port", type=int, default=8765, help="port for --watch (default 8765)")
    p.add_argument(
        "--tick-seconds",
        type=float,
        help="real seconds per simulated 10 minutes (default 1 with --watch, otherwise 0)",
    )
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("scenario", help="deterministic self-checking scenario (compare with the expected transcript)")
    p.add_argument("--data-dir", default="data/scenario")
    p.add_argument("--check", action="store_true", help="diff against docs/verification/expected_scenario.txt")
    p.add_argument("--out", help="also write the transcript to this file")
    p.set_defaults(fn=cmd_scenario)
    p = sub.add_parser("browser-demo", help="real Playwright agent against the local mock Instagram site")
    p.add_argument("--data-dir", default="data/browser-demo")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.set_defaults(fn=cmd_browser_demo)

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
    p = sub.add_parser("leads", help="list leads with score, status and why")
    p.add_argument("--status", action="append", help="e.g. QUALIFIED, DISQUALIFIED, CONTACTED (repeatable)")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_leads)
    p = sub.add_parser("add-lead", help="add a handle by hand (goes through the full pipeline)")
    p.add_argument("handle")
    p.add_argument("--campaign")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_add_lead)
    p = sub.add_parser("explain", help="everything known and decided about one lead, in order")
    p.add_argument("handle")
    p.set_defaults(fn=cmd_explain)
    p = sub.add_parser("actions", help="action log (discovery, inspection, sends...) with status and reason")
    p.add_argument("--type", action="append", help="DISCOVER, INSPECT_PROFILE, SEND_OUTREACH, SEND_FOLLOW_UP, ...")
    p.add_argument("--status", action="append", help="e.g. SUCCEEDED, BLOCKED, PENDING_APPROVAL (repeatable)")
    p.add_argument("--target", help="@handle")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_actions)
    p = sub.add_parser("action", help="one action: text, gate decisions, attempts, evidence")
    p.add_argument("action_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_action)
    p = sub.add_parser("generated", help="generated messages and what happened to each")
    p.add_argument("kind", nargs="?", default="outreach", choices=sorted(_GENERATED))
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_generated)
    p = sub.add_parser("messages", help="message log: sent DMs, replies, Rohit's own messages")
    p.add_argument("handle", nargs="?")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--full", action="store_true", help="do not shorten message text")
    p.set_defaults(fn=cmd_messages)
    p = sub.add_parser("suppressions", help="everyone who must never be contacted")
    p.set_defaults(fn=cmd_suppressions)
    p = sub.add_parser("audit", help="audit trail: operator changes and system decisions")
    p.add_argument("--kind", help="event prefix, e.g. mode, message.sent, lane, incident, conversation")
    p.add_argument("--subject", help="e.g. @handle or lane:BROWSER")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("safety", help="every safety limit and stop behaviour currently in force")
    p.set_defaults(fn=cmd_safety)
    p = sub.add_parser("preflight", help="live readiness checks (exit 1 while AUTONOMOUS would be refused)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_preflight)
    p = sub.add_parser("lead", help="lead detail (id or username)")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_lead)
    p = sub.add_parser("incidents", help="barriers/anomalies needing a human (with evidence paths)")
    p.add_argument("--all", action="store_true", help="include resolved incidents")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_incidents)
    p = sub.add_parser("lane", help="resume or halt an execution lane")
    p.add_argument("lane_action", choices=["resume", "halt"])
    p.add_argument("channel", choices=["api", "browser", "API", "BROWSER"])
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_lane)
    p = sub.add_parser("conversations", help="conversations and who owns them (human takeover)")
    p.add_argument("--paused", action="store_true", help="only conversations automation must not touch")
    p.add_argument("--json", action="store_true")
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
    p = sub.add_parser("alerts", help="phone/webhook alerts: send a test, or find your Telegram chat id")
    p.add_argument("alerts_action", choices=["test", "find-chat"])
    p.set_defaults(fn=cmd_alerts)
    p = sub.add_parser("mock-site", help="run the mock Instagram UI for local browser demos")
    p.add_argument("--port", type=int, default=8899)
    p.set_defaults(fn=cmd_mock_site)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()  # secrets from ./.env (never committed); real environment variables win
    chatty = args.command in ("run", "serve", "tick", "browser", "init", "setup")
    narrated = args.command in ("demo", "scenario", "browser-demo")  # events are printed as narration
    logging.basicConfig(
        level=logging.DEBUG
        if args.verbose
        else logging.INFO
        if chatty
        else logging.ERROR
        if narrated
        else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.fn(args) or 0)
    except (ControlError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
