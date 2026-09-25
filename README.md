# insta-outreach

Outreach orchestrator for the **@lemmedeliver** Instagram account. It finds local businesses that could use a LemmeDeliver website, works out why, writes a message specific to each one, and sends it within strict limits. It shares the account with Rohit and stays out of any conversation he handles himself.

Every candidate goes through the same pipeline:

```
DISCOVER → ANALYZE → DEDUPE → SCORE → PERSONALIZE → ELIGIBILITY GATE → OUTREACH
```

Execution is **hybrid**. Every operation is routed per capability:

- **Tier 1 – official Meta API** where Meta allows it:
  - replies inside the 24-hour window
  - one private reply to someone who commented on our post
  - reading threads and the inbox
  - Business Discovery lookups
- **Tier 2 – Playwright browser agent** on the normal Instagram web UI for everything the API cannot do:
  - search, hashtags, similar accounts
  - profile inspection
  - first DMs and follow-ups

The browser agent only executes. It never decides who to contact or what to say.

> **Read [the risk section](#risk-instagram-terms-of-use) before going live.**

## Safe by default

| Setting | Default | Meaning |
|---|---|---|
| `environment` | `local` | Everything runs against an in-process **simulated Instagram** made of fictional `sim.*` accounts. Nothing touches the network. |
| runtime mode | `OBSERVE` | Discovers and analyses only; never prepares or sends a message. |
| `api.enabled` / `browser.enabled` | `false` | No real channel exists until you configure one. |

The runtime modes are a single setting that can be changed while it runs:

| Mode | Discover + analyse | Prepare messages | Send |
|---|---|---|---|
| `OBSERVE` | ✓ | – | – |
| `DRAFT` | ✓ | ✓ (drafts only) | – |
| `APPROVAL` | ✓ | ✓ | only after a human approves each one |
| `AUTONOMOUS` | ✓ | ✓ | automatically, inside every limit below |

Replies to prospects still go to the approval queue in `AUTONOMOUS` unless `replies.autonomous: true`. A warm reply (interested, or asking a question) is handed to Rohit with a suggested answer.

## Quick start: verify it yourself

**[docs/VERIFY.md](docs/VERIFY.md)** has the exact commands, the expected outputs, where to see every lead / message / decision, and what is and is not proven. In short:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
insta-outreach scenario --check      # deterministic 10-step scenario; must print IDENTICAL ... 31/31 checks passed
insta-outreach demo --checkpoint     # 4 simulated days, narrated from the audit trail, no Instagram involved
insta-outreach browser-demo          # the real browser agent against a local mock of the Instagram UI
```

The demo runs the whole system in `AUTONOMOUS` mode against the simulated world:

- discovery and inspection;
- first DMs within the caps, including one **private reply** to someone who commented on a LemmeDeliver post;
- follow-ups after 3 days;
- opt-outs suppressed;
- interested prospects handed to Rohit;
- a security **checkpoint** that halts the browser lane until "Rohit" resumes it.

It narrates every event as it happens and ends with the commands to inspect the run. Examples: `insta-outreach --config data/demo/settings.yaml explain @sim.smileline.dental`, `audit`, `messages`, `incidents --all`.

Day-to-day use:

```bash
.venv/bin/insta-outreach init                 # creates config/settings.yaml + database (local, OBSERVE)
.venv/bin/insta-outreach run                  # orchestrator + console on http://127.0.0.1:8765
.venv/bin/insta-outreach mode APPROVAL        # change the runtime mode
.venv/bin/insta-outreach approvals            # review drafts; approve / reject --redraft
```

The console at `http://127.0.0.1:8765` shows:

- status, lanes and incidents;
- the approval queue (approve, edit or reject);
- leads and conversations (claim or release);
- the pause switch.

## Safeguards

All of these are configurable, and can be overridden at runtime with `insta-outreach limits --set KEY=VALUE`:

| Safeguard | Default |
|---|---|
| New conversations | 15/day, 4/hour |
| Follow-ups | at most 2 per lead, after 3 and 7 days; 10/day |
| Spacing between sends | ≥ 240 s + random 0–180 s |
| Send hours / browser hours | 10:00–20:00 / 09:30–21:30 IST |
| Browser activity | ≤ 120 page views per hour, ≥ 6–12 s apart, 1 browser session |
| Repeated contact | Never messages anyone with any earlier outbound message, or a thread with history. Branch accounts sharing a website, phone or email count as one business. |
| Suppression | Opt-outs and "not interested" replies are suppressed permanently, by username, IGSID, domain, phone and email. |
| Rate-limit signal | That lane cools down for 24 h; a second signal within 24 h halts it for a human. |
| Checkpoint, login, restriction, captcha | The lane stops immediately and an incident opens with a screenshot and URL. **Never bypassed.** |
| UI drift | After 2 consecutive "can't find the element" failures the lane halts for review. |
| Human activity | Any message Rohit sends himself pauses automation for that conversation until he releases it. |
| Global kill switch | `insta-outreach pause on` |

## Going live (summary)

Follow **[docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md)** step by step. It starts with a sandbox where only your own test account can receive messages. [docs/OPERATIONS.md](docs/OPERATIONS.md) is the day-to-day runbook.

1. Set `environment: live` and put a `CONTROL_API_TOKEN` in `.env` (loaded automatically). In live mode the control plane refuses to work without one.
2. **Browser lane:**
   - set `browser.enabled: true`;
   - run `insta-outreach browser login` and log in yourself, including any 2FA;
   - run `insta-outreach browser probe --target <some business>` (read-only).
3. **API lane (optional, recommended):**
   - create a Meta app with Instagram Login;
   - set `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_APP_SECRET` and `IG_WEBHOOK_VERIFY_TOKEN`;
   - subscribe the webhook fields `messages`, `message_echoes` and `comments` to `https://<host>/webhooks/instagram`.
4. Start in `OBSERVE`, then `DRAFT`, then `APPROVAL` for a while. For the live account, **AUTONOMOUS is refused by code** until `insta-outreach preflight` passes. That requires:
   - the token;
   - an outbound lane;
   - a browser session verified within 7 days;
   - no halted lane or open incident;
   - at least 3 human-approved live sends that succeeded;
   - explicit confirmation.

Claude writes messages and is the constrained fallback for unexpected pages when `ANTHROPIC_API_KEY` is set. Without it, deterministic templates are used and unknown pages stop the lane.

## Risk: Instagram Terms of Use

Instagram's [Terms of Use](https://help.instagram.com/581066165581870/) forbid accessing or collecting information in automated ways without Meta's permission. Meta's official API **cannot start a conversation**. The only API routes to a first message are:

- the other person messages first;
- a single private reply to a comment on your post.

So the browser lane automates something Meta has not authorised, and the account can be rate-limited, checkpointed or restricted. The limits above are deliberately low and every barrier stops the lane, but that lowers the risk; it does not remove it.

The comment → private-reply path and the API-only reply path are fully within the official API.

## Documentation

- [docs/VERIFY.md](docs/VERIFY.md): reproduce everything yourself. Expected outputs, where to look, what is and is not proven.
- [docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md): going live with @lemmedeliver, phase by phase, starting with your own test account.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, the pipeline, routing, lanes, conversation ownership, idempotency, data model, extension points.
- [docs/RESEARCH.md](docs/RESEARCH.md): what the Meta APIs can and cannot do (Sept 2026, with sources), and why Playwright plus a constrained Claude resolver was chosen over browser-use or Stagehand.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): runbook covering setup, modes, approvals, incidents and resuming lanes, human takeover, retention and troubleshooting.

## Development

```bash
.venv/bin/pytest -q                  # includes real Chromium runs against a local mock Instagram site
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
.venv/bin/insta-outreach mock-site   # the mock Instagram UI used by the browser tests (fault modes via /__control)
```

Project layout (`src/insta_outreach/`):

| Path | Responsibility |
|---|---|
| `orchestrator/` | pipeline stages, execution worker, tick loop, human controls |
| `policy/` | eligibility gate, lanes (stop-on-warning), usage ledger and pacing, suppression, incidents |
| `conversations/` | conversation ownership lease, human-takeover detection, thread reconciliation |
| `discovery/` | pluggable discovery strategies (registry) |
| `intelligence/` | signals, website checks, opportunity and score analysis, entity dedupe |
| `personalization/` | Claude/template composer, message validator |
| `execution/` | executor (routing), Graph API adapter, Playwright browser agent, simulator |
| `api/` | webhooks + control plane (FastAPI), console |
| `storage/` | SQLAlchemy models (SQLite by default) |
