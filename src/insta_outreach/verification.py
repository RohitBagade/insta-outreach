"""Reproducible verification runs. Nothing here touches Instagram or the network.

* ``run_demo``          `insta-outreach demo`: several simulated days, narrated live
                        from the audit trail (optionally with a checkpoint and a rate limit).
* ``run_scenario``      `insta-outreach scenario`: a fixed script with self-checks. Its
                        transcript is deterministic and committed as
                        ``docs/verification/expected_scenario.txt`` so a run can be diffed.
* ``run_browser_demo``  `insta-outreach browser-demo`: the *real* Playwright agent driving
                        Chromium against the local mock Instagram site: real screenshots,
                        a real checkpoint page, a real incident.

Every run keeps its database in its own data directory and writes a
``settings.yaml`` there, so all inspection commands work on it afterwards::

    insta-outreach --config data/demo/settings.yaml explain @sim.smileline.dental
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from insta_outreach.app import App, build_app
from insta_outreach.config import CampaignSettings, Settings, StrategySpec
from insta_outreach.domain.enums import (
    ActionStatus,
    Channel,
    ConversationOwner,
    Environment,
    IncidentSeverity,
    LaneState,
    LeadStatus,
    OperatingMode,
)
from insta_outreach.llm import NullLLM
from insta_outreach.reporting import clip, when
from insta_outreach.storage.models import AuditEvent, Conversation, Lead, Suppression
from insta_outreach.util.clock import FakeClock

START = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)  # Monday 21 Sep 2026, 10:30 IST
EXPECTED_SCENARIO = Path(__file__).resolve().parents[2] / "docs" / "verification" / "expected_scenario.txt"

# Audit events worth narrating as they happen (proposals are summarised per step instead).
NARRATED = (
    "message.sent",
    "send.",
    "action.cancelled",
    "action.demoted",
    "reply.handled",
    "suppression.",
    "conversation.",
    "lane.",
    "incident.",
    "mode.",
    "limits.",
    "action.approved",
    "action.rejected",
    "lead.added",
)


# ------------------------------------------------------------------- plumbing
class Out:
    """Collects the transcript (and echoes it unless quiet)."""

    def __init__(self, echo: bool = True) -> None:
        self.echo = echo
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)


class CollectingNotifier:
    """Notifications (incidents, warm-lead handoffs) collected for the transcript."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    async def notify(self, title: str, detail: str, severity: IncidentSeverity, data: Any = None) -> None:
        self.items.append((severity.value, title, detail))


@dataclass
class Checks:
    out: Out
    results: list[tuple[bool, str]] = field(default_factory=list)

    def __call__(self, ok: bool, what: str, actual: Any = None) -> bool:
        self.results.append((bool(ok), what))
        detail = "" if actual is None else f"   (actual: {actual})"
        self.out(f"  [{'PASS' if ok else 'FAIL'}] {what}{detail}")
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [what for ok, what in self.results if not ok]


class Narrator:
    """Prints new audit events as they happen: the narration *is* the audit trail."""

    def __init__(self, app: App, out: Out, prefixes: tuple[str, ...] = NARRATED) -> None:
        self.app = app
        self.out = out
        self.prefixes = prefixes
        self.last_id = 0
        self.tz = app.settings.schedule.timezone

    def flush(self, indent: str = "    ") -> list[tuple[str, str, str, str]]:
        with self.app.db.session() as session:
            rows = session.scalars(select(AuditEvent).where(AuditEvent.id > self.last_id).order_by(AuditEvent.id)).all()
            events = [(e.kind, e.summary, e.actor, when(e.at, self.tz), e.subject or "") for e in rows]
            if rows:
                self.last_id = rows[-1].id
        for kind, summary, actor, at, subject in events:
            if kind.startswith(self.prefixes):
                about = f"{subject}: " if subject.startswith("@") and subject not in summary else ""
                by = f"  [by {actor}]" if actor != "system" else ""
                self.out(f"{indent}{at}  {kind:<24} {about}{summary}{by}")
        return [(kind, summary, actor, at) for kind, summary, actor, at, _ in events]


def prepare_data_dir(data_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    return data_dir


def local_settings(data_dir: Path) -> Settings:
    settings = Settings(environment=Environment.LOCAL, data_dir=data_dir)
    settings.browser.evidence_dir = data_dir / "evidence"
    settings.browser.profiles_dir = data_dir / "browser_profiles"
    return settings


def write_inspection_config(settings: Settings, extra: dict[str, Any] | None = None) -> Path:
    """A settings file pointing at this run's database, for the inspection commands."""
    browser = {"evidence_dir": str(settings.browser.evidence_dir), "profiles_dir": str(settings.browser.profiles_dir)}
    doc: dict[str, Any] = {
        "environment": settings.environment.value,
        "data_dir": str(settings.data_dir),
        "browser": browser | (extra or {}).pop("browser", {}),
    }
    doc |= extra or {}
    path = Path(settings.data_dir) / "settings.yaml"
    path.write_text(
        "# Written by a verification run: points the inspection commands at this run's database.\n"
        + yaml.safe_dump(doc, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _tally(rows: list[dict[str, Any]], *keys: str) -> dict[str, int]:
    """Counts of ``key1:key2`` combinations, sorted (deterministic output)."""
    return dict(sorted(Counter(":".join(str(row[k]) for k in keys) for row in rows).items()))


def _counts(app: App) -> dict[str, int]:
    return dict(sorted(app.control.status()["leads"].items()))


def _sends(app: App) -> list[dict[str, Any]]:
    return [a for a in app.control.list_actions(limit=2000) if a["type"].startswith("SEND")]


def _lane(app: App, channel: Channel) -> tuple[str, str | None, str | None]:
    lanes = {lane["channel"]: lane for lane in app.control.status()["lanes"]}
    lane = lanes[channel.value]
    return lane["state"], lane["until"], lane["reason"]


def _inspect_hint(out: Out, config: Path) -> None:
    try:
        shown = config.relative_to(Path.cwd())
    except ValueError:
        shown = config
    c = f"insta-outreach --config {shown}"
    out(f"Inspect this run (read-only; {shown} points at this run's database):")
    for line in (
        f"  {c} leads                  # discovered leads, score, status, why",
        f"  {c} generated all          # every generated message and its fate",
        f"  {c} messages               # sent DMs, replies, Rohit's own messages",
        f"  {c} actions --type SEND_FOLLOW_UP",
        f"  {c} suppressions           # never-contact list",
        f"  {c} conversations --paused # human takeover",
        f"  {c} incidents --all        # checkpoints / rate limits, with evidence",
        f"  {c} audit                  # full audit trail",
        f"  {c} explain @<handle>      # every decision about one lead, in order",
        f"  {c} safety                 # limits in force",
    ):
        out(line)


# ----------------------------------------------------------------------- demo
async def run_demo(
    data_dir: Path,
    days: int = 4,
    mode: OperatingMode = OperatingMode.AUTONOMOUS,
    checkpoint: bool = False,
    rate_limit: bool = False,
    human: str | None = "sim.the.brew.room",
    commenter: str | None = "sim.smileline.dental",
    use_llm: bool = False,
    echo: bool = True,
) -> list[str]:
    out = Out(echo)
    settings = local_settings(prepare_data_dir(data_dir))
    clock = FakeClock(START)
    notes = CollectingNotifier()
    app = build_app(settings, clock=clock, llm=None if use_llm else NullLLM(), notifier=notes)
    world = app.world
    assert world is not None
    config = write_inspection_config(settings)
    tz = settings.schedule.timezone
    narrator = Narrator(app, out)
    app.runtime.set_mode(mode, "demo")
    out(
        f"SIMULATED DEMO - no Instagram, no network. {days} day(s) in mode {mode.value},"
        f" seed {settings.simulation.seed}."
    )
    out(f"database: {settings.resolved_database_url}")
    narrator.flush()

    try:
        for day in range(1, days + 1):
            day_start = START + timedelta(days=day - 1)
            clock.set(day_start)
            sends_before = {a["id"] for a in _sends(app) if a["status"] == "SUCCEEDED"}
            counts_before = _counts(app)
            out("")
            zone = when(day_start, tz).split()[-1]
            out(f"=== DAY {day}: {when(day_start, tz)[:10]} (simulated 10:30-21:30 {zone}) ===")
            if day == 1 and commenter:
                world.comment_on_our_post(commenter, "This looks amazing! 😍")
                out(f"    @{commenter} comments on one of our posts (a comments webhook in live)")
            if day == 2 and human:
                world.human_sends(human, "Hey! Rohit here personally, following up myself.")
                out(f"    Rohit messages @{human} himself from the Instagram app")
            if day == 2 and checkpoint:
                world.faults.checkpoint_after_browser_ops = world.browser_ops + 2
                out("    [fault injected] Instagram will show a 'Confirm it's you' checkpoint on the 3rd browser page")
            if day == 3 and rate_limit:
                world.faults.rate_limit_after_sends = world.sends + 1
                out("    [fault injected] Instagram will answer the 2nd send attempt today with 'Try Again Later'")
            ops_at_halt: int | None = None
            api_ops_at_halt = 0
            for _ in range(66):  # 11 hours of 10-minute ticks
                await app.orchestrator.tick()
                clock.advance(minutes=10)
                events = narrator.flush()
                if ops_at_halt is None and any(k == "lane.halted" for k, *_ in events):
                    ops_at_halt = world.browser_ops
                    api_ops_at_halt = _api_attempts(app)
                for severity, title, detail in notes.items:
                    if severity != "INFO":
                        out(f"    NOTIFY Rohit [{severity}] {title}" + (f" - {clip(detail, 100)}" if detail else ""))
                notes.items.clear()
            sent_today = [a for a in _sends(app) if a["status"] == "SUCCEEDED" and a["id"] not in sends_before]
            by_kind = Counter(f"{a['type']} via {a['executed_channel']}" for a in sent_today)
            delta = {k: v - counts_before.get(k, 0) for k, v in _counts(app).items() if v != counts_before.get(k, 0)}
            out(f"  day {day} summary: sent {dict(sorted(by_kind.items())) or 'nothing'}; lead status changes {delta}")
            if ops_at_halt is not None:
                state, _, reason = _lane(app, Channel.BROWSER)
                parked = len(app.control.list_actions([ActionStatus.NEEDS_HUMAN], limit=500))
                out("  CHECKPOINT - what to observe:")
                out(f"    browser lane is {state} ({reason})")
                out(f"    browser operations since the halt: {world.browser_ops - ops_at_halt} (must be 0)")
                out(f"    API operations since the halt: {_api_attempts(app) - api_ops_at_halt} (API lane unaffected)")
                out(f"    actions parked for a human (NEEDS_HUMAN): {parked}")
                world.faults.checkpoint_after_browser_ops = None
                released = app.control.resume_lane(Channel.BROWSER, by="demo", note="Rohit completed the check himself")
                narrator.flush()
                out(f"    Rohit completed the check in the app; `lane resume browser` released {released} action(s)")
            state, until, reason = _lane(app, Channel.BROWSER)
            if state == LaneState.COOLDOWN.value:
                out(f"  RATE LIMIT - browser lane in COOLDOWN until {when(until, tz)} ({reason}); resumes by itself")
        out("")
        out("=== SUMMARY ===")
        sends = _sends(app)
        out(f"leads by status: {_counts(app)}")
        out(f"outbound actions: {_tally(sends, 'type', 'status')}")
        ok = [a for a in sends if a["status"] == "SUCCEEDED"]
        out(f"sent via: {_tally(ok, 'capability', 'executed_channel')}")
        owned = sorted(c["peer_username"] for c in app.control.conversations(paused_only=True))
        out(f"conversations owned by Rohit: {owned}")
        out(f"incidents (all): {[i['title'] for i in app.control.incidents(open_only=False)]}")
        out("")
        _inspect_hint(out, config)
    finally:
        await app.close()
    return out.lines


def _api_attempts(app: App) -> int:
    from insta_outreach.storage.models import ActionAttempt

    with app.db.session() as session:
        return int(
            session.scalar(select(func.count()).select_from(ActionAttempt).where(ActionAttempt.channel == Channel.API))
            or 0
        )


# ------------------------------------------------------------------- scenario
async def run_scenario(data_dir: Path, echo: bool = True) -> tuple[list[str], list[str]]:
    """Fixed script, fixed seed, fixed clock, templates only: same transcript every run.

    Returns (transcript lines, failed checks).
    """
    out = Out(echo)
    check = Checks(out)
    settings = local_settings(prepare_data_dir(data_dir))
    clock = FakeClock(START)
    notes = CollectingNotifier()
    app = build_app(settings, clock=clock, llm=NullLLM(), notifier=notes, seed=settings.simulation.seed)
    world = app.world
    assert world is not None
    write_inspection_config(settings)
    tz = settings.schedule.timezone
    narrator = Narrator(app, out)

    async def ticks(n: int) -> None:
        for _ in range(n):
            await app.orchestrator.tick()
            clock.advance(minutes=10)
            narrator.flush()

    def automated_sends() -> list[dict[str, Any]]:
        return [m for m in world.sent_log if m["channel"] != "HUMAN"]

    def step(title: str, day: int | None = None) -> None:
        if day is not None:
            clock.set(START + timedelta(days=day - 1))
        out("")
        out(f"== {title} ==")
        out(f"   simulated time {when(clock.now(), tz)}")

    out("insta-outreach verification scenario")
    out("  environment LOCAL (simulated Instagram, 31 fictional sim.* accounts), seed 7, templates only (no LLM)")
    out(f"  simulated start {when(START, tz)}; data dir <DATA_DIR>/")
    out("  limits: " + ", ".join(f"{k}={v}" for k, v in _key_limits(app).items()))
    narrator.flush()

    try:
        # 1 ------------------------------------------------------------------
        step("STEP 1  OBSERVE: discover and analyse only", day=1)
        app.control.set_mode(OperatingMode.OBSERVE, by="operator")
        await ticks(24)
        counts = _counts(app)
        out(f"  leads by status: {counts}")
        for lead in app.control.leads([LeadStatus.QUALIFIED], 50):
            top = max(lead["opportunities"] or [{}], key=lambda o: o.get("weight", 0))
            out(f"    QUALIFIED @{lead['username']} score {lead['score']} - {top.get('type')}: {top.get('rationale')}")
        for status in (LeadStatus.DISQUALIFIED, LeadStatus.DUPLICATE, LeadStatus.ANALYZED):
            for lead in app.control.leads([status], 50):
                out(f"    {status.value} @{lead['username']} - {lead['status_reason']}")
        check(counts.get("QUALIFIED", 0) >= 5, "at least 5 leads qualified with a concrete opportunity")
        check(not _sends(app) and not automated_sends(), "OBSERVE prepared no messages and sent nothing")
        check(counts.get("DUPLICATE", 0) >= 1, "branch accounts sharing one website were merged (DUPLICATE)")

        # 2 ------------------------------------------------------------------
        step("STEP 2  DRAFT: messages are written but never sent")
        app.control.set_mode(OperatingMode.DRAFT, by="operator")
        await ticks(1)
        drafts = sorted(_sends(app), key=lambda a: a["target_username"])
        for a in drafts:
            out(f"    DRAFTED for @{a['target_username']}: {a['message']}")
        check(len(drafts) == 5, "5 drafts prepared (planner batch size)", len(drafts))
        check({a["status"] for a in drafts} == {"DRAFTED"}, "every draft is DRAFTED, none approved")
        check(not automated_sends(), "nothing sent in DRAFT", len(automated_sends()))

        # 3 ------------------------------------------------------------------
        step("STEP 3  APPROVAL: only what a human approves is sent")
        app.control.set_mode(OperatingMode.APPROVAL, by="operator")
        first, second = drafts[0], drafts[1]
        edited = first["message"].replace("I'm Rohit from LemmeDeliver.", "This is Rohit from LemmeDeliver.")
        out(f"  operator approves @{first['target_username']} with an edit, rejects @{second['target_username']}")
        try:
            app.control.approve(first["id"], by="operator", edited_text=edited + " Only ₹4999!")
            check(False, "an edit that adds a price is refused")
        except Exception as exc:  # ControlError
            check("prices" in str(exc), "an edit that adds a price is refused by the validator")
        app.control.approve(first["id"], by="operator", edited_text=edited)
        app.control.reject(second["id"], by="operator", reason="tone", redraft=True)
        narrator.flush()
        await ticks(6)
        texts = [m["text"] for m in automated_sends()]
        check(texts == [edited], "exactly one message sent: the approved, edited text", len(texts))
        redraft = [
            a
            for a in _sends(app)
            if a["target_username"] == second["target_username"] and a["status"] == "PENDING_APPROVAL"
        ]
        check(len(redraft) == 1, f"@{second['target_username']} got a fresh draft waiting for approval")
        waiting = [a for a in _sends(app) if a["status"] in ("DRAFTED", "PENDING_APPROVAL")]
        check(bool(waiting), f"{len(waiting)} other drafts are still waiting, unsent")

        # 4 ------------------------------------------------------------------
        step("STEP 4  AUTONOMOUS within tight caps (5/day, 2/hour, >=240s apart) + a comment on our post")
        app.runtime.set_limit_overrides({"outreach_per_day": 5, "outreach_per_hour": 2}, "operator")
        app.control.set_mode(OperatingMode.AUTONOMOUS, by="operator")
        world.comment_on_our_post("sim.skinsense.derma", "Love the new website designs!")
        out("  @sim.skinsense.derma comments on one of our posts")
        out("  (drafts made under DRAFT/APPROVAL still need a human: AUTONOMOUS never approves them retroactively)")
        await ticks(21)
        step("STEP 4b AUTONOMOUS, next morning", day=2)
        await ticks(24)
        outreach = [a for a in _sends(app) if a["type"] == "SEND_OUTREACH" and a["status"] == "SUCCEEDED"]
        times = sorted(m["at"] for m in automated_sends())
        per_day = dict(sorted(Counter(when(t, tz)[:10] for t in times).items()))
        out(f"  first messages sent so far: {len(outreach)}; per day: {per_day}")
        check(all(v <= 5 for v in per_day.values()), "never more than 5 new conversations per day", per_day)
        worst = max((sum(1 for x in times if t <= x < t + timedelta(hours=1)) for t in times), default=0)
        check(worst <= 2, "never more than 2 sends in any rolling hour", worst)
        gaps = [(b - a).total_seconds() for a, b in pairwise(times)]
        smallest = min(gaps, default=240.0)
        check(smallest >= 240, "at least 240 s between any two sends", f"min gap {smallest:.0f}s")
        private = [a for a in outreach if a["capability"] == "private_reply"]
        check(
            len(private) == 1 and private[0]["executed_channel"] == "API",
            "the commenter got one official private reply via the API (no browser)",
        )

        # 5 ------------------------------------------------------------------
        step("STEP 5  replies arrive: opt-outs suppressed, interest handed to Rohit", day=3)
        await ticks(12)
        with app.db.session() as session:
            rows = session.execute(
                select(Lead.username, Lead.status, Lead.status_reason).where(Lead.replied_at.is_not(None))
            ).all()
        for username, status, reason in sorted(rows):
            out(f"    @{username} replied -> lead {status.value}: {reason}")
        check(bool(rows), "at least one prospect replied", len(rows))

        # 6 ------------------------------------------------------------------
        step("STEP 6  Rohit messages a prospect himself: automation steps back")
        with app.db.session() as session:
            candidates = session.scalars(
                select(Lead.username)
                .where(Lead.status == LeadStatus.CONTACTED, Lead.replied_at.is_(None))
                .order_by(Lead.username)
            ).all()
        target = candidates[0]
        world.human_sends(target, "Rohit here - happy to walk you through it on a call.")
        out(f"  Rohit writes to @{target} from the Instagram app")
        await ticks(2)
        with app.db.session() as session:
            conv = session.scalars(select(Conversation).where(Conversation.peer_username == target)).one()
            owner, paused_now = conv.owner, conv.automation_paused
        check(owner is ConversationOwner.HUMAN and paused_now, f"@{target} is now owned by Rohit, automation paused")

        # 7 ------------------------------------------------------------------
        step("STEP 7  follow-ups, three days after the first messages", day=6)
        await ticks(30)
        followups = [a for a in _sends(app) if a["type"] == "SEND_FOLLOW_UP" and a["status"] == "SUCCEEDED"]
        targets = sorted(a["target_username"] for a in followups)
        out(f"  follow-ups sent to: {targets}")
        check(bool(followups), "follow-ups were sent", len(followups))
        check(target not in targets, f"no follow-up to @{target} (Rohit owns that conversation)")
        check(
            "sim.skinsense.derma" not in targets,
            "no follow-up after the private reply (Instagram allows more only once they answer)",
        )

        # 8 ------------------------------------------------------------------
        step("STEP 8  checkpoint: the browser lane stops and waits for a human", day=7)
        world.faults.checkpoint_after_browser_ops = world.browser_ops
        out("  [fault injected] the next browser page is a 'Confirm it's you' checkpoint")
        for _ in range(6):
            await ticks(1)
            if _lane(app, Channel.BROWSER)[0] == "HALTED":
                break
        state, _, reason = _lane(app, Channel.BROWSER)
        check(state == "HALTED", "browser lane HALTED", reason)
        incident = next(iter(app.control.incidents()), None)
        check(
            incident is not None
            and incident["kind"] == "CHECKPOINT_REQUIRED"
            and "/challenge/" in (incident["page_url"] or ""),
            "critical incident opened with the checkpoint page URL",
        )
        ops, api_before = world.browser_ops, _api_attempts(app)
        await ticks(6)
        check(world.browser_ops == ops, "no browser activity at all while halted (6 ticks)", world.browser_ops - ops)
        out(f"  meanwhile the API lane kept working: {_api_attempts(app) - api_before} API operation(s)")
        parked = app.control.list_actions([ActionStatus.NEEDS_HUMAN], limit=100)
        out(f"  parked for a human: {[a['type'] + ' @' + str(a['target_username']) for a in parked]}")
        world.faults.checkpoint_after_browser_ops = None
        released = app.control.resume_lane(Channel.BROWSER, by="operator", note="completed the check on my phone")
        narrator.flush()
        check(released == len(parked) and released >= 1, "resuming the lane releases the parked action(s)", released)
        await ticks(3)
        check(world.browser_ops > ops, "browser activity resumes only after the human resumed the lane")

        # 9 ------------------------------------------------------------------
        step("STEP 9  rate limit: cooldown, never pushing on", day=8)
        world.faults.rate_limit_after_sends = world.sends
        out("  [fault injected] the next send attempt gets 'Try Again Later'")
        sends_before = world.sends
        await ticks(12)
        state, until, reason = _lane(app, Channel.BROWSER)
        check(state == "COOLDOWN", "browser lane in COOLDOWN after the rate-limit signal", reason)
        check(until is not None, f"cooldown lasts until {when(until, tz)}")
        check(world.sends == sends_before, "nothing sent after the rate-limit signal", world.sends - sends_before)

        # 10 -----------------------------------------------------------------
        step("STEP 10 summary and whole-run invariants")
        out(f"  leads by status: {_counts(app)}")
        sends = _sends(app)
        out(f"  outbound actions: {_tally(sends, 'type', 'status')}")
        ok = [a for a in sends if a["status"] == "SUCCEEDED"]
        out(f"  sent via: {_tally(ok, 'capability', 'executed_channel')}")
        with app.db.session() as session:
            kinds = dict(sorted(Counter(k for (k,) in session.execute(select(AuditEvent.kind)).all()).items()))
        out(f"  audit events: {kinds}")
        for what, violations in _invariants(app).items():
            check(not violations, what, violations or None)
        check(
            len(ok) == len(automated_sends()) == kinds.get("message.sent", 0),
            "database, audit trail and simulated Instagram agree on every automated message",
            f"{len(ok)} / {kinds.get('message.sent', 0)} / {len(automated_sends())}",
        )
    finally:
        await app.close()

    out("")
    failed = check.failed
    passed = len(check.results) - len(failed)
    out(f"RESULT: {passed}/{len(check.results)} checks passed" + (" - FAILED" if failed else ""))
    return out.lines, failed


def _invariants(app: App) -> dict[str, list[str]]:
    """Whole-run safety properties, computed from the database alone."""
    from insta_outreach.domain.enums import MessageDirection, SenderKind, SuppressionKind
    from insta_outreach.storage.models import Message

    automated = (SenderKind.API_AGENT, SenderKind.BROWSER_AGENT)
    after_reply: list[str] = []
    after_takeover: list[str] = []
    after_suppression: list[str] = []
    with app.db.session() as session:
        suppressed_at = {
            s.value: s.created_at
            for s in session.scalars(select(Suppression).where(Suppression.kind == SuppressionKind.USERNAME))
        }
        for conv in session.scalars(select(Conversation).order_by(Conversation.id)):
            msgs = session.scalars(
                select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id)
            ).all()
            sent = [
                m
                for m in msgs
                if m.direction is MessageDirection.OUTBOUND
                and m.sender_kind in automated
                and m.delivery_state == "SENT"
            ]
            first_in = next((m.created_at for m in msgs if m.direction is MessageDirection.INBOUND), None)
            who = f"@{conv.peer_username}"
            if first_in is not None:
                after_reply += [who for m in sent if m.created_at > first_in]
            if conv.automation_paused and conv.paused_at is not None:
                after_takeover += [who for m in sent if m.created_at > conv.paused_at]
            if conv.peer_username in suppressed_at:
                after_suppression += [who for m in sent if m.created_at > suppressed_at[conv.peer_username]]
    return {
        "no automated message after a prospect replied (replies go to Rohit)": sorted(set(after_reply)),
        "no automated message after Rohit took a conversation over": sorted(set(after_takeover)),
        "no automated message to anyone after they were suppressed": sorted(set(after_suppression)),
    }


def _key_limits(app: App) -> dict[str, Any]:
    lim = app.runtime.limits()
    return {
        "outreach/day": lim.outreach_per_day,
        "outreach/hour": lim.outreach_per_hour,
        "send gap": f"{lim.min_seconds_between_sends}s+0-{lim.send_jitter_seconds}s",
        "follow-ups": f"{lim.max_followups_per_lead} after {lim.followup_after_days}d",
        "send hours": "-".join(app.settings.schedule.send_hours),
    }


def normalise(lines: list[str], data_dir: Path) -> list[str]:
    return [line.replace(str(data_dir.resolve()), "<DATA_DIR>") for line in lines]


# -------------------------------------------------------------- browser demo
async def run_browser_demo(data_dir: Path, headed: bool = False, echo: bool = True) -> list[str]:
    """The real Playwright agent + real Chromium against the local mock site.

    Environment LIVE (real adapters), mode APPROVAL (a scripted operator
    approves), handles prefixed ``mock.`` so nothing is simulated in-process.
    """
    import asyncio
    import socket
    import threading

    import uvicorn

    from insta_outreach.devtools.mock_instagram import STATE, create_app

    out = Out(echo)
    data_dir = prepare_data_dir(data_dir)
    STATE.reset(prefix="mock.")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    settings = Settings(environment=Environment.LIVE, data_dir=data_dir)
    settings.browser.enabled = True
    settings.browser.base_url = base
    settings.browser.allowed_hosts = ["127.0.0.1"]
    settings.browser.profiles_dir = data_dir / "browser_profiles"
    settings.browser.evidence_dir = data_dir / "evidence"
    settings.browser.headless = not headed
    settings.browser.typing_delay_ms = 5
    settings.browser.llm_fallback = False
    settings.scoring.check_websites = False
    settings.campaigns = [
        CampaignSettings(
            id="thane-cafes",
            niches=["cafe"],
            locations=["Thane"],
            strategies=[StrategySpec(name="keyword_search", params={"max_queries_per_run": 1, "max_results": 5})],
        )
    ]
    config = write_inspection_config(
        settings, {"browser": {"enabled": True, "base_url": base, "allowed_hosts": ["127.0.0.1"]}}
    )
    clock = FakeClock(START)
    notes = CollectingNotifier()
    app = build_app(settings, clock=clock, llm=NullLLM(), notifier=notes)
    tz = settings.schedule.timezone
    narrator = Narrator(app, out)
    out("BROWSER DEMO - the real Playwright agent drives Chromium against a LOCAL MOCK of the Instagram web UI.")
    out(f"mock site: {base}  (nothing here reaches instagram.com: the navigation allowlist is 127.0.0.1)")
    out(f"environment LIVE (real adapters), mode APPROVAL, evidence in {settings.browser.evidence_dir}")

    async def ticks(n: int) -> None:
        for _ in range(n):
            await app.orchestrator.tick()
            clock.advance(minutes=10)
            narrator.flush()

    try:
        app.control.set_mode(OperatingMode.APPROVAL, by="operator")
        out("")
        out("1) discovery + inspection in the browser (search 'cafe Thane', open profiles)")
        await ticks(6)
        for lead in app.control.leads(None, 20):
            out(f"    @{lead['username']:<28} {lead['status']:<14} {clip(lead['status_reason'], 80)}")
        out("")
        out("2) the operator approves the drafted first message(s); the agent sends through the web UI")
        for action in app.control.list_actions([ActionStatus.PENDING_APPROVAL]):
            out(f"    approving {action['id']} -> @{action['target_username']}: {clip(action['message'], 90)}")
            app.control.approve(action["id"], by="operator")
        narrator.flush()
        await ticks(4)
        for sent_msg in STATE.sent:
            out(f"    mock Instagram received a DM to @{sent_msg['to']}: {clip(sent_msg['text'], 90)}")
        out("")
        out("3) Instagram shows a security checkpoint: the agent must stop, record, and wait")
        STATE.faults.add("checkpoint")
        app.control.add_lead("mock.smileline.dental", by="operator", note="forces one more browser visit")
        await ticks(3)
        state, _, reason = _lane(app, Channel.BROWSER)
        out(f"    browser lane: {state} ({reason})")
        for line in _incident_lines(app, tz):
            out(line)
        out("")
        out("4) Rohit clears the checkpoint himself, then resumes the lane")
        STATE.faults.discard("checkpoint")
        released = app.control.resume_lane(Channel.BROWSER, by="operator", note="checkpoint cleared by Rohit")
        narrator.flush()
        out(f"    released {released} parked action(s)")
        await ticks(3)
        shots = _screenshots(Path(settings.browser.evidence_dir))
        out("")
        out(f"screenshots taken by the agent ({len(shots)}):")
        for shot in shots:
            out(f"    {shot}")
        out("")
        _inspect_hint(out, config)
    finally:
        await app.close()
        server.should_exit = True
        thread.join(timeout=5)
    return out.lines


def _incident_lines(app: App, tz: str) -> list[str]:
    lines = []
    for incident in app.control.incidents():
        lines.append(f"    incident #{incident['id']} [{incident['severity']}] {incident['title']}")
        lines.append(f"      page: {incident['page_url']}")
        for evidence in incident["evidence"] or []:
            lines.append(
                f"      evidence {evidence.get('kind')}: {evidence.get('path') or clip(evidence.get('content'), 80)}"
            )
    return lines or ["    (no incident)"]


def _screenshots(evidence_dir: Path) -> list[Path]:
    return sorted(evidence_dir.rglob("*.png"))


Printer = Callable[[str], None]
