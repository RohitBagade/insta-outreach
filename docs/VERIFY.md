# Verify it yourself (before touching the real account)

Everything on this page runs on your computer against a **simulated Instagram**, or, for the browser demo, a **local mock of the Instagram web UI**. None of it contacts Instagram or Meta, and none of it needs credentials.

## What is proven, and what is not

| | Status | How you can check |
|---|---|---|
| **Fully tested in simulation** | The whole decision pipeline: discovery → analysis → dedupe → scoring → personalization → eligibility gate → outreach. Modes OBSERVE / DRAFT / APPROVAL / AUTONOMOUS. Approvals, including edits and rejections. Daily and hourly caps, pacing and send hours. Follow-ups. Duplicate prevention. Suppression. Human-takeover lock. Checkpoint / login / restriction halts. Rate-limit cooldowns. Crash recovery without double sends. Comment → private-reply flow. Audit trail. The live readiness gate that refuses AUTONOMOUS. | `insta-outreach scenario --check` (31 self-checks, transcript identical to the committed one), `insta-outreach demo --checkpoint`, `pytest` (185 tests) |
| **Tested against a local mock, not live** | The **real** Playwright browser agent: search, profile reading, thread opening, typing, sending, thread-history checks, checkpoint detection with screenshot and trace. The mock is a simplified imitation of instagram.com, so it proves the agent's *logic*, not that its selectors match today's real Instagram. | `insta-outreach browser-demo`, `pytest -m browser` |
| **Implemented, not tested live** | Graph API adapter: unit-tested against a fake HTTP server using Meta's documented formats. Also: webhook signature check and parsing, the website checker, the Claude message writer (tests use a scripted stand-in), notification webhook. | code + unit tests only |
| **Impossible to verify until you provide credentials** | Whether the browser agent works on real instagram.com today: selectors, the English UI texts it detects, login-session lifetime, what real checkpoint and rate-limit pages look like. Also: your Meta app permissions and tokens, real webhook delivery (messages, echoes, comments), private replies, and deliverability of first DMs (they land in *Requests*). And the quality of Claude-written messages if you set `ANTHROPIC_API_KEY`. | [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md), step by step, starting with your own test account |

Nothing on this page can enable live sending. AUTONOMOUS for the live account is refused by code until the preflight checks pass (section 5).

---

## 1. Run everything from a clean start

You need Python 3.11 or newer (`python3 --version`) and git.

```bash
git clone https://github.com/RohitBagade/insta-outreach.git
cd insta-outreach
git checkout claude/practical-keller-nw54ab

python3 -m venv .venv
source .venv/bin/activate            # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

insta-outreach scenario --check      # (a) deterministic proof, ~10 s
insta-outreach demo --checkpoint     # (b) narrated 4-day simulation, ~15 s
playwright install chromium          #     one-time, only needed for (c) and (d)
insta-outreach browser-demo          # (c) real browser agent vs local mock site, ~20 s
pytest -q                            # (d) full test suite, ~2-3 min
```

Expected endings:

| Command | Last line(s) you should see |
|---|---|
| `scenario --check` | `IDENTICAL to …/docs/verification/expected_scenario.txt: RESULT: 31/31 checks passed` (exit code 0) |
| `demo --checkpoint` | `=== SUMMARY ===` with `sent via: {'private_reply:API': 1, 'send_dm_reply:BROWSER': 6, 'send_new_dm:BROWSER': 14}`, followed by the inspection commands |
| `browser-demo` | `screenshots taken by the agent (3):` and three `.png` paths |
| `pytest -q` | `185 passed` |

Every run keeps its own database under `data/…` and writes a `settings.yaml` next to it, so you can inspect the run afterwards. `data/` is never committed.

## 2. Where to see each thing

After `insta-outreach demo --checkpoint`, point the commands at the demo's database once:

```bash
export INSTA_OUTREACH_CONFIG=data/demo/settings.yaml      # PowerShell: $env:INSTA_OUTREACH_CONFIG="data/demo/settings.yaml"
```

The same works for `data/scenario/settings.yaml` and `data/browser-demo/settings.yaml`. Alternatively, add `--config data/demo/settings.yaml` to every command.

| You want to see | Command | What it shows |
|---|---|---|
| Discovered leads | `insta-outreach leads` | Every lead with score, status, opportunities, when it was found, and why it has that status. Filter with `--status QUALIFIED`, `--status DISQUALIFIED`, etc. |
| Generated outreach messages | `insta-outreach generated outreach` (or `all`) | Every generated message with its fate: DRAFTED, PENDING_APPROVAL, SUCCEEDED, BLOCKED, EXPIRED, CANCELLED. Includes composer and approver. `insta-outreach approvals` shows only what waits for you. |
| Sent simulated DMs | `insta-outreach messages` | The conversation log. `-> out` rows show who sent them (BROWSER_AGENT / API_AGENT / HUMAN) and the delivery state. `<- in` rows are replies, with intent. Add `@handle` for one conversation, `--full` for untruncated text. |
| Follow-ups | `insta-outreach generated followup`, `insta-outreach actions --type SEND_FOLLOW_UP` | Each follow-up, its number and its status. |
| Blocked / suppressed leads | `insta-outreach suppressions`, `insta-outreach leads --status DISQUALIFIED --status CLOSED --status DUPLICATE --status UNREACHABLE`, `insta-outreach actions --status BLOCKED --status CANCELLED` | The never-contact list (username, IGSID, domain, phone, email) and why. Leads that were disqualified, merged or closed. Messages the gate blocked, with reasons. |
| Human takeover | `insta-outreach conversations --paused`, `insta-outreach audit --kind conversation` | Conversations owned by Rohit and why: he wrote himself, handed off after a warm reply, or claimed. |
| Checkpoint / rate-limit incidents | `insta-outreach incidents --all`, `insta-outreach audit --kind lane` | Each incident with severity, page URL, evidence and resolution. Lane halts, cooldowns and resumes. |
| Audit log | `insta-outreach audit` | Append-only trail, in order: mode/limit/pause changes (incl. refused AUTONOMOUS attempts), approvals, rejections, sends, blocks, replies handled, suppressions, takeovers, incidents, lane changes. Filter with `--kind message.sent`, `--subject @handle`. |
| Browser screenshots / evidence | `insta-outreach browser-demo`, then `data/browser-demo/evidence/<date>/` | Real screenshots (`*-sent.png`, `*-checkpoint_required.png`, `*-already_contacted.png`) and Playwright trace zips (`playwright show-trace <zip>`). `incidents --all` and `action <id>` list the evidence files per attempt. The pure simulation renders no pages, so it has no screenshots. |
| Agent decisions and why | `insta-outreach explain @sim.smileline.dental` | For one lead, in order: how it was found (every source), the facts observed, each opportunity with its rationale, the score arithmetic, every action with its proposal-time and execution-time gate decisions (e.g. `DEFER - hourly outreach cap 4 reached`), every attempt and result, all messages, conversation owner, and its audit trail. |
| One action in detail | `insta-outreach action <act_…id>` | Message text, facts used, gate decisions plus the history of every deferral reason, attempts, evidence, page URLs. |
| Everything at once, visually | `insta-outreach serve`, then open http://127.0.0.1:8765 | The console: status, lanes, approval queue, leads, incidents, conversations. |

The raw data is one SQLite file per run (`data/demo/local.db`), readable with any SQLite viewer. The tables are listed in [ARCHITECTURE.md](ARCHITECTURE.md#data-model-sqlite-by-default-any-sqlalchemy-url).

## 3. The deterministic scenario: expected vs actual

`insta-outreach scenario` runs a fixed script with the same inputs every time:
- a fixed clock, starting Monday 21 Sep 2026 10:30 IST;
- a fixed random seed (7);
- templates only, no LLM;
- action ids derived from their idempotency keys.

The transcript is therefore byte-identical on every machine. The expected transcript is committed at [`docs/verification/expected_scenario.txt`](verification/expected_scenario.txt).

```bash
insta-outreach scenario --check                   # compares for you: IDENTICAL / unified diff, exit code 0 / 1
# or compare by hand:
insta-outreach scenario --out actual.txt
diff docs/verification/expected_scenario.txt actual.txt && echo SAME     # Windows: fc docs\verification\expected_scenario.txt actual.txt
```

The script, and what the expected transcript shows at each step:

| Step | What happens | Expected (from the committed transcript) |
|---|---|---|
| 1 OBSERVE | 4 simulated hours of discovery + analysis | 11 QUALIFIED, 2 DISQUALIFIED (`personal profile`, `too large (450000 followers)`), 1 DUPLICATE (`@sim.pearl.dental.khar`: same domain as the Bandra branch), 2 ANALYZED (no concrete opportunity). **0 messages prepared, 0 sent.** |
| 2 DRAFT | one tick | 5 drafts, all `DRAFTED`, **0 sent**. The texts are printed. |
| 3 APPROVAL | approve one with an edit, reject one, try an edit containing a price | Price edit **refused by the validator**. **Exactly 1 message sent**, the edited one, to `@sim.aroma.kitchen.mulund`. The rejected lead gets a fresh draft awaiting approval. `@sim.chai.and.chapters` is taken over because Rohit had already written to them, so its draft is cancelled. |
| 4 AUTONOMOUS | caps lowered to 5/day and 2/hour; someone comments on our post | Official **private reply via API** to `@sim.skinsense.derma`. Per day `{'2026-09-21': 2, '2026-09-22': 3}`, max 2 in any hour, smallest gap 610 s. |
| 5 replies | next days | `@sim.aroma.kitchen.mulund` says "Not interested" → CLOSED + suppressed (username, IGSID, phone). |
| 6 takeover | Rohit writes to `@sim.happy.tails.grooming` himself | Owner HUMAN, automation paused. |
| 7 follow-ups | 3 days later | Follow-ups only to `lotus.physio`, `urban.fade.barbers`, `zari.boutique`. **None** to the human-owned conversation, the private-reply lead or anyone who replied. |
| 8 checkpoint | next browser page is "Confirm it's you" | Browser lane HALTED + critical incident with `/challenge/` URL. **0 browser operations** while halted; the API lane keeps working (2 operations). 1 parked action, released only by `lane resume`. |
| 9 rate limit | next send gets "Try Again Later" | Browser lane COOLDOWN until 2026-09-29 10:30 IST, **0 sends** after the signal. |
| 10 invariants | whole-run checks from the database | No automated message after a reply, after a takeover or after a suppression. DB, audit trail and simulated Instagram agree: `16 / 16 / 16`. `RESULT: 31/31 checks passed`. |

The same comparison runs automatically in `pytest tests/test_scenario.py`.

## 4. The checkpoint demo, step by step

```bash
insta-outreach demo --checkpoint        # add --rate-limit to also see a cooldown on day 3
```

The narration *is* the audit trail: each indented line is an audit event, printed as it happens with its simulated IST time. What to look for:

1. **Day 1.** Normal work:
   - `message.sent` lines: first messages via BROWSER, one private reply via API;
   - `@sim.chai.and.chapters` is recognised as Rohit's existing conversation (`conversation.human_owned … outbound message in thread was not sent by automation`);
   - a warm reply is handed to you (`NOTIFY Rohit [WARNING] Warm lead …`);
   - an opt-out is suppressed.
2. **Day 2, start.** `[fault injected] Instagram will show a 'Confirm it's you' checkpoint on the 3rd browser page`.
3. **The halt.** `incident.opened … [CRITICAL] BROWSER lane halted: Instagram security checkpoint requires the account owner`, then `lane.halted`, then `NOTIFY Rohit [CRITICAL] …`. The agent did not try to answer the checkpoint.
4. **The rest of day 2.** `day 2 summary: sent nothing`.
5. **The `CHECKPOINT - what to observe` block:**
   - `browser lane is HALTED (CHECKPOINT_REQUIRED: challenge_url)`;
   - `browser operations since the halt: 0 (must be 0)`: nothing touched the browser for the rest of the day;
   - `API operations since the halt: 24 (API lane unaffected)`: a checkpoint on the web session stops only that lane;
   - `actions parked for a human (NEEDS_HUMAN): 1`.
6. **The human step.** `incident.resolved …` and `lane.resumed … 1 parked action(s) released`. Only a human command reopens the lane (in real use: `insta-outreach lane resume browser --note "…"`, after you completed the check in the Instagram app yourself).
7. **Day 3.** Browser sends resume. **Day 4:** follow-ups go out, but not to suppressed, handed-off or human-owned conversations.

**The same with the real browser agent.** `insta-outreach browser-demo` (add `--headed` to watch Chromium). It uses environment LIVE and mode APPROVAL against a mock site on 127.0.0.1:
- `mock Instagram received a DM to @mock.the.brew.room` after a scripted operator approval;
- `send.not_sent … ALREADY_CONTACTED thread_has_history` for the account Rohit had already messaged;
- `browser lane: HALTED (CHECKPOINT_REQUIRED: security_checkpoint)`, an incident with the `/challenge/` page URL, a screenshot and a trace;
- the lane resumes only after the operator does it.

Open `…-checkpoint_required.png`: the code field is empty, because the agent never types into a security check.

## 5. Modes, and why AUTONOMOUS cannot run live without credentials

```bash
insta-outreach mode                 # show the current mode
insta-outreach mode OBSERVE         # discover + analyse, never prepares or sends
insta-outreach mode DRAFT           # prepares messages, never sends
insta-outreach mode APPROVAL        # sends only what a human approves (insta-outreach approvals / approve <id>)
insta-outreach mode AUTONOMOUS      # simulation: allowed. Live: refused unless every preflight check passes
insta-outreach pause on             # global kill switch, any mode
```

The console (`insta-outreach serve`) has the same switch. Every change is in `insta-outreach audit --kind mode`, including refused attempts.

**Proof.** Create a file `live-proof.yaml` that selects the live environment with no credentials:

```yaml
environment: live
data_dir: data/live-proof
```

```text
$ insta-outreach --config live-proof.yaml preflight
result  check            required  detail
------  ---------------  --------  -------------------------------------------------------------------------------------------
PASS    environment      no        live
FAIL    control_token    yes       set CONTROL_API_TOKEN (protects the kill switch and approvals)
FAIL    send_lane        yes       no outbound lane: enable the browser (browser.enabled) and/or the API (api.enabled + token)
PASS    browser_session  no        browser lane disabled (first DMs need it)
PASS    api_credentials  no        API lane disabled (optional)
PASS    lanes            yes       no lane halted
PASS    incidents        yes       no open warning/critical incidents
FAIL    approved_sends   yes       0 human-approved live send(s) succeeded; 3 required before AUTONOMOUS
PASS    sandbox          no        no target allowlist (outbound may go to any qualified lead)
PASS    global_pause     no        off
PASS    llm              no        templates only (no ANTHROPIC_API_KEY)

AUTONOMOUS would be REFUSED: control_token, send_lane, approved_sends

$ insta-outreach --config live-proof.yaml mode AUTONOMOUS --confirm
error: AUTONOMOUS refused for the live account; failing preflight checks: control_token: set CONTROL_API_TOKEN (…); send_lane: no outbound lane: … ; approved_sends: 0 human-approved live send(s) succeeded; 3 required before AUTONOMOUS
(exit code 2)

$ insta-outreach --config live-proof.yaml mode
OBSERVE
```

In live mode AUTONOMOUS requires **all** of the following:
- `CONTROL_API_TOKEN`;
- a configured outbound lane;
- a browser session verified within 7 days (by `browser login`, `browser probe` or a successful operation);
- no halted lane and no open incident;
- **at least 3 human-approved live sends that succeeded**, which is what the APPROVAL phase produces;
- `--confirm` (the CLI asks).

If the stored mode is AUTONOMOUS but the checks fail (e.g. the database was edited by hand), `insta-outreach run` refuses to start: `REFUSING TO START …`. Tests prove each of these: `pytest tests/test_readiness.py -v`.

## 6. Safety limits currently configured

`insta-outreach safety` prints the limits actually in force, including runtime overrides. With the defaults:

```text
SEND CAPS
  new conversations: 15/day, 4/hour
  follow-ups: 10/day
  replies: 20/hour
  profile inspections: 250/day   discovery runs: 12/day
  browser page views: 120/hour, at most 25 per operation, 1 browser session(s) at a time

DELAYS
  between any two sends: >= 240s + random 0-180s
  between browser page views: >= 6.0s + random 0-6.0s
  retries: at most 3 attempts, backoff 120s x 2^n

OPERATING HOURS (Asia/Kolkata)
  sends: 10:00-20:00   browser activity: 09:30-21:30

FOLLOW-UPS
  at most 2 per lead, after 3, 7 day(s) since the previous message
  never after they replied, never while Rohit owns the conversation, never after a private reply,
  never while Instagram shows a pending message request

DUPLICATE PREVENTION
  one idempotency key per planned action (re-planning never duplicates work)
  repeated contact denied if ANY earlier outbound message to the lead exists
  first message refused (ALREADY_CONTACTED) if the thread already has history
  identical text already in the thread -> reported as sent, never re-sent
  one business = one lead: same IG user id, website domain, phone or email -> DUPLICATE

SUPPRESSION
  opt-out / 'not interested' replies suppress username, IGSID, website domain, phone and email
  sandbox allowlist (rollout.allowed_targets): none (any qualified lead)

HUMAN TAKEOVER LOCK
  one owner per conversation (NONE / HUMAN / API_AGENT / BROWSER_AGENT), shared by API and browser
  automation lease ttl 300s, re-validated immediately before pressing send
  any outbound message automation did not send (webhook echo or thread read) -> owner HUMAN, automation paused, open actions cancelled
  paused until: released by Rohit   echo attribution window 900s

STOP BEHAVIOUR (never bypassed)
  LOGIN_REQUIRED: HALT that lane + critical incident (Instagram session is logged out / token invalid); actions parked
  CHECKPOINT_REQUIRED: HALT that lane + critical incident (Instagram security checkpoint requires the account owner); actions parked
  ACCOUNT_RESTRICTED: HALT ALL lanes + critical incident (Instagram reports the account is restricted or blocked); actions parked
  HUMAN_ACTION_REQUIRED: HALT that lane + critical incident (Instagram is asking for something only a human should do); actions parked
  checkpoint detection: /challenge/, /auth_platform/, two-factor URLs, 'confirm it's you' texts, captcha iframes (recaptcha, hcaptcha, arkose)
  RATE_LIMITED: cooldown 1440 min; 2 within 24h -> HALT
  UI_CHANGED: HALT after 2 consecutive
  resume only by a human: `insta-outreach lane resume <api|browser>`
```

Change them in `config/settings.yaml` under `limits:`, or at runtime with `insta-outreach limits --set outreach_per_day=5` (recorded in the audit trail). `insta-outreach limits --clear` returns to the configured values.

## 7. Next: the live integration

See [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md). It starts with your own test account in a sandbox where only allowlisted handles can receive messages, then goes OBSERVE → DRAFT → APPROVAL on @lemmedeliver. AUTONOMOUS stays off until those phases pass and the preflight checks say so.
