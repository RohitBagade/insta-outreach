# Operations runbook

## 1. Install and initialise

```bash
uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/playwright install chromium        # skip if a Chromium is already provided (PLAYWRIGHT_BROWSERS_PATH)
.venv/bin/insta-outreach init                 # config/settings.yaml (local, OBSERVE) + data dirs + database
cp .env.example .env                          # fill in secrets; loaded automatically; never commit .env
```

Before anything live, run through **[VERIFY.md](VERIFY.md)** (simulation, reproducible) and then **[LIVE_CHECKLIST.md](LIVE_CHECKLIST.md)** (phase by phase, starting with a sandbox on your own test account).

**Configuration** is read from three places:

- `config/settings.yaml`; the path can be overridden with `--config` or `INSTA_OUTREACH_CONFIG`.
- **Secrets as environment variables:** `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_APP_SECRET`, `IG_WEBHOOK_VERIFY_TOKEN`, `CONTROL_API_TOKEN`, `NOTIFY_WEBHOOK_URL`, `ANTHROPIC_API_KEY`. A `.env` file in the working directory is loaded automatically; variables already set in the shell win.
- **Overrides of any setting:** `INSTA__SECTION__KEY=value`, e.g. `INSTA__LIMITS__OUTREACH_PER_DAY=10`.

The **runtime mode, the pause switch and limit overrides** are stored in the database. They change without a restart, from the CLI or Mission Control, and every change is recorded with who made it.

## 2. Rolling out safely

1. `environment: local`. Run `insta-outreach demo` and `insta-outreach run` against the simulator until the behaviour and messages look right. Nothing here touches Instagram.
2. `environment: live`, mode **OBSERVE**. The live database is separate (`data/live.db`) and simulated leads can never leak into it. Discovery and analysis run on the real account; check `insta-outreach leads` for sensible qualification.
3. **DRAFT**. Messages are written but never sent. Read them in Mission Control or with `insta-outreach approvals`.
4. **APPROVAL**. You approve each message (optionally edited) or reject it (`--redraft` for a fresh draft). Stay here until the drafts are consistently good.
5. **AUTONOMOUS**. For the live account this is **refused by code** until `insta-outreach preflight` passes:
   - control token;
   - an outbound lane;
   - a browser session verified within 7 days;
   - no halted lane or open incident;
   - at least 3 human-approved live sends that succeeded.

   Then `insta-outreach mode AUTONOMOUS` still asks for confirmation. `run` and `tick` refuse to start if the stored mode is AUTONOMOUS while preflight fails. Sends happen automatically inside every limit. Replies from prospects still wait for approval unless `replies.autonomous: true`; interested or question replies are handed to you.

Going back down is always safe. Switching from AUTONOMOUS to APPROVAL moves already auto-approved messages back into the approval queue.

## 3. Browser lane (Tier 2)

Set in `config/settings.yaml`:

```yaml
environment: live
browser:
  enabled: true
  headless: true
  locale: en-US      # page-state detectors match English UI text: keep the Instagram account language English
```

1. `insta-outreach browser login` opens a visible browser on the persistent profile (`data/browser_profiles/<account>`). **Log in yourself**, including any 2FA or "confirm it's you" step. The command returns once the home feed loads. The profile directory holds the session cookies: keep it private, and back it up with care.
2. `insta-outreach browser probe --target <a business handle> --query "dentist thane"` checks the session and extracts a profile and search results. It is **read-only**: it never opens a thread or types anything. If a field comes back empty, see Troubleshooting (UI drift).
3. Keep the pace conservative: one browser session, about 120 page views per hour, 09:30–21:30 IST.

## 4. API lane (Tier 1, optional but recommended)

Why this lane matters:

- replies inside the 24 h window use the official API;
- webhooks give real-time replies, human-takeover detection and comments on our posts;
- comments on our posts get official private replies.

1. Create a Meta app with **Instagram API with Instagram Login**. Add the permissions `instagram_business_basic`, `instagram_business_manage_messages` and `instagram_business_manage_comments`.
   - Serving an account without a role on the app, and receiving webhooks, needs Advanced Access. That means App Review, Business Verification and the app set to Live.
   - Business Discovery (profile inspection via the API) exists only with **Facebook Login**: set `api.login_type: facebook` if you use that setup.
2. Set `IG_ACCESS_TOKEN` and `IG_USER_ID` (the professional account's Instagram id), plus `IG_APP_SECRET` and `IG_WEBHOOK_VERIFY_TOKEN`. Then set `api.enabled: true`.
3. **Webhook.**
   - Callback URL: `https://<your-host>/webhooks/instagram`. Verify token: the same value as `IG_WEBHOOK_VERIFY_TOKEN`.
   - Subscribe to the fields **`messages`, `message_echoes`, `comments`**.
   - Deliveries are rejected unless their `X-Hub-Signature-256` matches `IG_APP_SECRET`. In live mode, a missing app secret returns 503.
   - Expose **only** `/webhooks/instagram` publicly, for example through a reverse proxy or tunnel. Keep `/api/*` and Mission Control on localhost or behind the token ([DEPLOY.md §7](DEPLOY.md#7-instagram-webhooks-optional)).
4. Run `insta-outreach run`. It starts the orchestrator and the HTTP server; `--no-api` would disable webhooks.

## 5. Daily operation

| Task | CLI | Mission Control (`http://127.0.0.1:8765`) |
|---|---|---|
| Overview: mode, lanes, counters, incidents | `insta-outreach status` | Header, attention strip, workflow, lanes |
| Review / approve / edit / reject drafts | `approvals`, `approve <id> [--message "..."]`, `reject <id> --reason ... [--redraft]` | Approvals tab |
| Leads and why they (don't) qualify | `leads [--status QUALIFIED]`, `explain @handle` | Leads tab; click a workflow step |
| Every decision about one lead, in order | `explain @handle` | click any handle → decision trail |
| Generated messages and their fate | `generated [outreach\|followup\|reply\|all]` | Approvals tab (waiting), Live activity |
| Message log (sent, received, Rohit's own) | `messages [@handle]` | click any handle → conversation |
| One action: gate decisions, attempts, evidence | `actions [--type …] [--status …]`, `action <id>` | – |
| Audit trail | `audit [--kind mode\|message.sent\|lane\|…] [--subject @handle]` | Live activity; Audit trail tab |
| Limits and stop behaviour in force | `safety` | Today's limits (usage vs caps) |
| Live readiness (AUTONOMOUS prerequisites) | `preflight` | Go-live readiness |
| Add a handle by hand (full pipeline) | `add-lead @handle [--note …]` | – |
| Never-contact list | `suppressions` | – |
| Take over a conversation | `claim @handle` | Conversations → Take over |
| Hand a conversation back | `release @handle` | Conversations → Hand back |
| Never contact someone | `suppress @handle` (or `--kind DOMAIN/PHONE/EMAIL`) | click the handle → Never contact |
| Stop everything now | `pause on` / `pause off` | Pause all |
| Halt / resume a lane | `lane halt browser`, `lane resume browser --note …` | Lanes, Incidents |
| Alerts on your phone | `alerts find-chat`, `alerts test` | – (see [DEPLOY.md §6](DEPLOY.md#6-alerts-on-your-phone-telegram)) |
| Change limits | `limits --set outreach_per_day=10 min_seconds_between_sends=300`, `limits --clear` | – |

With `CONTROL_API_TOKEN` set, Mission Control asks for the token once per browser tab and the HTTP API expects `Authorization: Bearer <token>`. **In live mode the control plane refuses to work without a token** (503).

## 6. Working alongside automation

- **If you message a prospect yourself**, from the phone app or the web, automation detects it within one tick. It uses the webhook echo when the API lane is configured, and otherwise the next thread read. That conversation is then **yours**: automation pauses it and cancels any queued follow-up, and you get a notification. It stays paused until you `release` it, or for `ownership.human_hold_hours` if you set it.
- **When a prospect replies:**
  - "stop", "not interested" and similar are suppressed permanently for that business: handle, IGSID, website domain, phone and email.
  - Interested replies and questions are handed to you, with a suggested reply in the notification.
- Your personal chats are never stored. DMs from people who are neither leads nor existing conversations are ignored.

## 7. Incidents and lanes

A **lane** is one channel (API or browser) of the account. When Instagram shows a barrier, the lane stops and an incident opens. The incident has a severity, the page URL, a screenshot and a Playwright trace under `data/evidence/<date>/`. Notifications go to the log, and to `NOTIFY_WEBHOOK_URL` if set.

| Incident | What happened | What to do |
|---|---|---|
| `CHECKPOINT_REQUIRED` (browser lane halted) | "Confirm it's you", a security code or a captcha | Open Instagram yourself (phone or `browser login`) and complete the check. Then `insta-outreach lane resume browser --note "..."`. |
| `LOGIN_REQUIRED` | Session expired (browser) or token invalid (API, code 190) | Browser: `insta-outreach browser login`. API: refresh `IG_ACCESS_TOKEN` and restart. Then resume the lane. |
| `ACCOUNT_RESTRICTED` (**all** lanes halted) | "We restrict certain activity", action blocked, suspension | Stop and read Instagram's notice in the app. Do **not** resume for at least the period Instagram states. Consider lowering the limits before resuming. |
| `RATE_LIMITED` (cooldown, then halt) | "Try again later", HTTP 429, API limits | The cooldown lifts automatically after 24 h. A second one within 24 h halts the lane: lower the limits before resuming. |
| `UI_CHANGED` (halt after repeats) | Instagram changed its web UI and elements weren't found | Run `browser probe`, look at the screenshot and trace (`playwright show-trace <zip>`), then patch `config/ui_map.yaml`. |
| `HUMAN_ACTION_REQUIRED` | Anything else needing you, e.g. DM access disabled in the app settings, or the browser failed to launch | Read the detail, fix, resume. |

**The system never tries to get past a checkpoint, captcha, restriction or login wall.** Resuming a lane releases the actions parked while it was halted, re-checked by the gate. `insta-outreach lane halt browser --note "..."` stops a lane by hand.

## 8. Data, privacy, retention

- **Databases:** SQLite at `data/<environment>.db` (WAL mode). Any SQLAlchemy URL works via `database_url`. Back up the file together with `data/browser_profiles/`.
- **What is kept:**
  - for leads: observed public profile facts, analysis, sources;
  - conversations with leads;
  - every action and attempt, with results;
  - suppressions and incidents;
  - the append-only audit trail (`audit_events`): who changed what, and every consequential system decision.
- **Retention:** raw webhook payloads are deleted after `retention.webhook_payload_days` (7). Evidence folders are deleted after `retention.evidence_days` (30), except those cited by an open incident. Usage-ledger rows are deleted after 8 days.
- **Never commit** `data/`, `.env` or `config/settings.yaml`; `.gitignore` covers them.

## 9. Troubleshooting

- **Nothing is being sent.** Check `insta-outreach status` (mode, pause, lanes), then the Gate step in Mission Control, which lists why queued messages wait (or `GET /api/actions`): the `gate` field of each action lists its reasons, such as send hours, caps, pacing or awaiting approval.
- **A lead isn't contacted.** Run `insta-outreach lead @handle`: `status_reason`, `score_breakdown`, `disqualify_reasons` and `opportunities` explain why. Qualification requires a concrete opportunity.
- **UI drift.** Selectors and detector texts are data. Copy the relevant entries into a YAML file, point `browser.ui_map_path` at it, and re-run `browser probe`. With `ANTHROPIC_API_KEY` set, the constrained resolver usually finds moved elements by itself and caches what it learned. Repeated drift still halts the lane on purpose.
- **Claude unavailable.** Messages fall back to validated templates and unknown pages stop the lane. Check the key, or `llm.max_calls_per_hour`.
- **Webhook shows 403.** Signature mismatch: check `IG_APP_SECRET`. Verification returning 403 means the verify token differs.
- **Local mock UI.** `insta-outreach mock-site` serves an imitation of the Instagram web UI on port 8899, for trying the real browser agent without Instagram. Set `browser.base_url: http://127.0.0.1:8899` and `allowed_hosts: [127.0.0.1]`. Fault modes are available via `/__control`.

## 10. Risk reminder

The browser lane automates the Instagram web UI, which Instagram's Terms of Use do not permit without Meta's permission (see [RESEARCH.md](RESEARCH.md#terms-of-use)). Low limits, pacing and stop-on-warning reduce the chance of restrictions but cannot rule them out.

The API-only features stay within the official platform:

- replies inside the 24 h window;
- private replies to comments;
- webhooks.
