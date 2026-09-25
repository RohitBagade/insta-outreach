# Architecture

```
                 ┌────────────────────────── Orchestrator (tick loop) ──────────────────────────┐
 webhooks ──►    │ inbound events → DISCOVER → ANALYZE → DEDUPE → SCORE → PERSONALIZE → queue    │
 simulator ──►   │                      (pipeline.py: every business decision lives here)       │
                 └──────────────────────────────────────┬───────────────────────────────────────┘
                                                        │ Action rows (one intent each)
                                  ┌─────────────────────▼─────────────────────┐
                                  │ Eligibility gate (policy/gate.py)          │  proposal-time preview
                                  │ mode · approval · suppression · repeated   │  + execution-time check
                                  │ contact · ownership · caps · pacing ·      │
                                  │ send hours · lanes                         │
                                  └─────────────────────┬─────────────────────┘
                                  ┌─────────────────────▼─────────────────────┐
                                  │ Execution worker (orchestrator/worker.py)  │  claim · conversation lease
                                  │                                            │  · intent record · result → state
                                  └─────────────────────┬─────────────────────┘
                                  ┌─────────────────────▼─────────────────────┐
                                  │ Instagram executor (execution/executor.py) │  per-capability routing,
                                  │                                            │  lanes, pacing, fallback rules
                                  └───────────┬─────────────────────┬─────────┘
                              ┌───────────────▼──────┐     ┌────────▼──────────────────────┐
                              │ Graph API adapter    │     │ Playwright browser agent       │
                              │ (Tier 1, official)   │     │ (Tier 2, normal web UI)        │
                              └──────────────────────┘     └───────────────────────────────┘
```

Adapters are **execution workers**. Each one receives a single `OperationRequest`:

- action id and capability;
- target;
- for sends, the approved message plus the hashes of every message automation has already sent in that conversation;
- a unit budget.

Each one returns one `ExecutionResult`. The result carries a status (the 10 required ones plus `OWNERSHIP_CONFLICT`, `NOT_PERMITTED`, `CAPABILITY_UNAVAILABLE` and `PERMANENT_FAILURE`), a machine code, evidence, the page URL and data.

What happens next is decided in `worker.py`, `pipeline.py` and `policy/lanes.py`, never inside an adapter.

## Pipeline and lead states

| Stage | Code | Lead status after |
|---|---|---|
| Discover | `Pipeline.plan_discovery` → `DISCOVER` actions → `upsert_candidates` (candidate persisted with provenance before anything else) | `DISCOVERED` |
| Analyze | `plan_inspections` → `INSPECT_PROFILE` → `apply_observation` → `analyze_lead` (signals, website check, opportunities, score) | `ANALYZED` / `QUALIFIED` / `DISQUALIFIED` |
| Dedupe | `intelligence/dedupe.py`: identity keys `IG_USER_ID`, `DOMAIN`, `PHONE`, `EMAIL`; the earlier lead wins | `DUPLICATE` |
| Score | `intelligence/analyzer.py`: weighted breakdown; **qualification needs a concrete opportunity** (`NEW_WEBSITE`, `BROKEN_WEBSITE`, `WEBSITE_REBUILD`, `ONLINE_BOOKING`, `VISUAL_SHOWCASE`) | |
| Personalize | `plan_outreach` → `MessageComposer.compose_initial` → `MessageValidator` | |
| Gate | `EligibilityGate.preview` (as if AUTONOMOUS, so permanent denials never reach the approval queue) | `OUTREACH_PENDING` (or blocked → `DISQUALIFIED`/`CONTACTED`/`HANDED_OFF`/`CLOSED`) |
| Outreach | worker → executor (`SEND_NEW_DM` via browser, or `PRIVATE_REPLY` via API) | `CONTACTED` |
| Follow-up | `plan_followups` (days `[3, 7]`, max 2, never after a reply, never after a private reply) | |
| Replies | `process_events` / `reconcile_thread` → `plan_replies` → keyword rules first, then Claude | `REPLIED` → `CLOSED` (opt-out / negative, suppressed) or `HANDED_OFF` (interest / question) |

Discovery strategies are pluggable (`discovery/strategies.py`). The registry holds:

- `keyword_search`, e.g. "dentist Thane";
- `hashtag`: derived tags, or configured ones;
- `location`: location pages;
- `followers_of` / `following_of`;
- `suggested_accounts`: the similar accounts of qualified leads;
- `post_engagers`.

A campaign lists which strategies to use and their parameters. Each run has a `query_key`, and a cooldown (14 days by default) keeps the same query from being repeated. Comments on our own posts are an eighth, event-driven source (`own_post_comment`).

Adding a strategy:

```python
@register
class DirectoryImport(DiscoveryStrategy):
    name = "directory_import"
    capability = Capability.INSPECT_PROFILE   # or any discovery capability

    def plan(self, campaign, ctx) -> list[DiscoveryTask]:
        ...  # return tasks; results come back through interpret()
```

## Actions: the unit of work

Every planned step is an `Action` row. Each one has:

- an `idempotency_key`, e.g. `outreach:<account>:<lead>:v<n>` or `followup:<account>:<lead>:<n>`, so re-planning never duplicates work;
- a status: `PROPOSED`, `BLOCKED`, `DRAFTED`, `PENDING_APPROVAL`, `APPROVED`, `EXECUTING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `REJECTED`, `NEEDS_HUMAN` or `EXPIRED`;
- a priority: replies first, discovery last;
- attempts;
- gate decisions (proposal and execution);
- result data.

Every executor call is also stored as an `ActionAttempt`.

- **Claiming** is atomic and holds a 15-minute lease. A lease that expires while `EXECUTING` (a crash) is re-queued. The next attempt re-reads the thread before sending (see Idempotency).
- **Modes** decide only how a new outbound action enters the queue:

  | Mode | Entry state |
  |---|---|
  | `DRAFT` | `DRAFTED` |
  | `APPROVAL` | `PENDING_APPROVAL` |
  | `AUTONOMOUS` | `APPROVED` by `auto` (replies still need approval unless `replies.autonomous`) |

  `OBSERVE` never creates outbound actions. The gate re-checks the *current* mode at execution time: switching to `APPROVAL` demotes auto-approved work back to `PENDING_APPROVAL`.
- **Approval edits** are re-validated against the same rules as generated text. Drafts expire after 72 h (the facts may be stale) and the lead gets a fresh draft.

## Eligibility gate

`evaluate(GateFacts) -> GateDecision` is a pure function, unit-tested rule by rule.

- **DENY** (permanent):
  - suppressed (username, IGSID, domain, email or phone);
  - the conversation is owned by, or was paused by, the human;
  - already contacted: any outbound message to this lead other than this action's own;
  - lead not `QUALIFIED`, score below the minimum, not a business;
  - follow-up rules: replied, blocked, out of sequence, over the limit;
  - no lane can serve the capability.
- **DEFER** until a time: send hours, the browser's active hours, daily and hourly caps, minimum spacing plus jitter (persisted per action), lane cooldowns.
- **DEFER, parked:** mode doesn't send, awaiting approval, global pause, all lanes halted. `resume_lane` releases parked work.

## Routing (executor)

| Capability | Route (in order) |
|---|---|
| `SEARCH_ACCOUNTS`, `HASHTAG_POSTS`, `LOCATION_POSTS`, `LIST_FOLLOWERS`, `LIST_FOLLOWING`, `SUGGESTED_ACCOUNTS`, `POST_ENGAGERS` | browser (no official API; see RESEARCH.md) |
| `SEND_NEW_DM` | browser (the API cannot start conversations) |
| `INSPECT_PROFILE` | API (Business Discovery, Facebook Login only) → browser |
| `READ_THREAD`, `READ_INBOX` | API → browser |
| `SEND_DM_REPLY` | API (inside the 24 h window) → browser |
| `PRIVATE_REPLY`, `REPLY_COMMENT` | API only |

- Routes are data (`DEFAULT_ROUTES`). An adapter's `supports(request)` decides whether it can serve a given request right now: for example the 24 h window, or the 7-day private-reply window.
- **Fallback** to the next channel is allowed:
  - for reads, after any non-barrier failure;
  - for writes, only when the first channel *certainly did not send* (`CAPABILITY_UNAVAILABLE`, `NOT_PERMITTED`).
- An ambiguous API timeout is never followed by a browser re-send. A barrier never falls back.
- Per-channel semaphores cap concurrency: 1 browser session by default. Each operation gets a unit budget. Browser page views are paced (6 s + jitter) and counted in the usage ledger.

## Lanes: stop-on-warning

A lane is `(account, channel)` in state `ACTIVE`, `COOLDOWN` (until a time) or `HALTED` (until a human resumes it).

| Result | Lane effect |
|---|---|
| `LOGIN_REQUIRED`, `CHECKPOINT_REQUIRED`, `HUMAN_ACTION_REQUIRED` | halt that lane + critical incident (URL, screenshot, detail); actions → `NEEDS_HUMAN` |
| `ACCOUNT_RESTRICTED` | halt **all** lanes of the account |
| `RATE_LIMITED` | cooldown (24 h default); a 2nd signal within 24 h halts |
| `UI_CHANGED` | halt after 2 consecutive (a profile with no Message button counts, with threshold + 1) |
| anything successful | resets the drift counter |

Nothing in the system tries to get *past* a barrier. `insta-outreach lane resume browser` (or the console) reopens the lane after Rohit has resolved it himself, and releases the parked actions.

## Browser agent (Tier 2)

`execution/browser/`:

- **`session.py`**:
  - one persistent Chromium profile per account (`browser.profiles_dir/<account>`), created by `browser login`, where Rohit logs in himself;
  - locale and timezone pinned;
  - a navigation allowlist (other hosts are aborted), popups closed;
  - a counter of HTTP 429 responses.
- **`detect.py` + `ui_map.py`**: after every navigation the page state is classified deterministically:
  - the expected page, identified by page markers;
  - `restricted`;
  - `checkpoint`: challenge / 2FA URLs, "confirm it's you" texts, captcha iframes;
  - `login_required`, `rate_limited`, `request_pending`, `cannot_message`, `not_found`, `private`;
  - a benign dialog (notifications, save-login, add-to-home-screen, optional cookies), dismissed only with its allowlisted "Not now / Cancel / Decline" label;
  - `unknown`.

  Selectors and detector texts live in `ui_map.py` and can be patched from YAML (`browser.ui_map_path`) without a code change.
- **`vision.py`**: only for `unknown` pages. With an LLM configured, Claude classifies a screenshot plus text into a fixed set. The mapping is safety-biased: anything resembling a security check or restriction stops the lane.
- **`resolver.py`**: self-healing element resolution for a fixed *intent* (e.g. `thread.composer`). It tries, in order:
  1. deterministic role/name locators;
  2. locators learned earlier (`LearnedLocator`, with hit/miss stats);
  3. Claude picks **one index** from a list of visible interactive elements that the agent tags with `data-io-cand`.

  The pick is then validated in code:
  - allowed roles;
  - name pattern;
  - editable / visible;
  - a deny-list of dangerous names (follow/unfollow, block, report, delete, unsend, like, restrict, log out, pay, confirm, send code, verify…).

  It is then converted to a semantic locator that must resolve to the *same* element before it is used or cached. The model never chooses what to do or what to type.
- **`extract.py`**: parsers for profiles (bio without link/button noise, private accounts, website, category, counts, recent posts), search results, threads (message direction from geometry) and the inbox.
- **Send path** (`adapter._send`), pure code:
  1. open the thread from the target's profile and verify the header shows the target;
  2. read history. An identical message already there → `SUCCESS already_sent_idempotent`. History on a first message → `ALREADY_CONTACTED`. Outbound messages automation didn't send → `OWNERSHIP_CONFLICT`. A pending request on a follow-up → `NOT_PERMITTED`;
  3. type exactly the approved text (Shift+Enter for line breaks) and verify the composer;
  4. re-validate the conversation lease (`ctx.guard()`);
  5. press send and confirm the new bubble. Unconfirmed → `RETRYABLE_FAILURE send_unconfirmed`, which the next attempt resolves by reading the thread.
- **Evidence** (`evidence.py`): screenshots (always on failure, optionally on success), text excerpts and Playwright trace zips on failure, under `evidence_dir/<UTC date>/`, pruned after `retention.evidence_days`.

## API adapter (Tier 1)

`execution/api/`:

- `GraphApiClient` sends a Bearer token to `graph.instagram.com` (Instagram Login) or `graph.facebook.com` (Facebook Login), v26.0.
- `map_error` turns Graph error codes into structured statuses:

  | Codes | Status |
  |---|---|
  | 4, 17, 32, 613, 80002, HTTP 429 | `RATE_LIMITED` (with `X-Business-Use-Case-Usage` retry time) |
  | 190 | `LOGIN_REQUIRED` |
  | 10/2534022 | `NOT_PERMITTED outside_messaging_window` |
  | 551 | `NOT_PERMITTED` |
  | 100/2534025 | `NOT_PERMITTED comment_invalid_for_private_reply` |
  | 100/2534029 | `ACCOUNT_RESTRICTED` |
  | 110/2207013 | `TARGET_NOT_FOUND` |
  | 200/2534041 | `HUMAN_ACTION_REQUIRED` |

- A transport error during a send is `RETRYABLE`. Retries, and any send whose intent was already recorded (a crash), read the thread first and report `already_delivered` rather than sending twice.
- **Comment → private reply.** A `comments` webhook on our own media turns the commenter into a `DISCOVERED` lead (source `own_post_comment`, IGSID kept). Negative comments are ignored.
  - If the lead qualifies within 6.5 days and the API lane is available, the first message is a `PRIVATE_REPLY`: official, no browser, allowed once within 7 days. The message thanks them for the comment.
  - If the platform refuses, or the window has closed, the lead returns to `QUALIFIED` and gets a normal DM.
  - After a private reply, follow-ups are blocked until they respond (platform rule).

## Conversation ownership and the human

Rohit uses the same account. Each conversation (`Conversation` row, keyed by username and/or IGSID, merged when both become known) has **one current owner**: `NONE`, `HUMAN`, `API_AGENT` or `BROWSER_AGENT`.

- **Lease:** an atomic compare-and-set with a fencing token (`lock_token`, `lock_expires_at`).
  - The worker acquires it before a send and *transfers* it on API → browser fallback.
  - The adapter re-validates it immediately before pressing send.
  - The API and browser lanes share this lock.
- **Intent first:** a `PENDING` outbound message with the text hash is written *before* sending. That lets the webhook echo of our own send be attributed to automation rather than to Rohit.
- **Human takeover** is detected two ways:
  - **Echoes** (`is_echo` webhooks) that match no automation intent, by message id or by text within a time window. This includes echoes keyed only by IGSID.
  - **Thread reconciliation:** a (direction, text-hash) multiset comparison whenever a thread is read. Unknown outbound messages mean Rohit wrote them.

  Either way the conversation is set to `HUMAN` and `automation_paused`. Open outbound actions are cancelled and Rohit is notified. It stays paused until he releases it (`insta-outreach release @user`), or for `ownership.human_hold_hours` if set.
- **Handoff:** interested or question replies pause automation and notify Rohit with a suggested reply. `claim` / `release` do it manually.
- **Privacy:** DMs from people who are neither leads nor existing conversations (friends, unrelated chats) are dropped without being stored. Raw webhook payloads are deleted after `retention.webhook_payload_days` (7).

## Personalization

`MessageComposer` gives Claude only the lead's **fact sheet**, the opportunity, and (for replies) the history. It uses `messages.parse` with a Pydantic output model: message + fact keys used. Refusals are checked, and refusal fallbacks are enabled.

- Facts are declared untrusted data, never instructions.
- `MessageValidator` rejects:
  - numbers not present in the facts;
  - prices / percentages / urgency;
  - links in first messages;
  - banned phrases, hashtags, placeholders;
  - more than 1 emoji;
  - over 480 chars or Instagram's 1000-byte limit;
  - a missing brand introduction or closing question;
  - near-duplicates of recent messages.
- One feedback retry, then deterministic templates whose observation sentences each require the fact they state.
- Without an LLM the system runs on templates.

## Audit trail and live readiness

- **Audit trail** (`policy/audit.py`, table `audit_events`). Each event is written in the same transaction as the change it describes. Recorded:
  - operator changes: mode (including refused AUTONOMOUS attempts), pause, limits, approvals and edits, rejections, lane resume/halt, suppressions, claims and releases, manual leads, browser session verifications;
  - system decisions: proposal outcome (queued / auto-approved / blocked with reasons), sends, execution-time cancellations and demotions, parked or failed sends, replies handled, human takeovers, incidents opened and resolved, lane halts and cooldowns.

  `insta-outreach audit` and `explain @handle` read it. The verification demo narrates from it.
- **Live readiness** (`orchestrator/readiness.py`). `live_readiness()` computes the preflight checks. `ControlService.set_mode` refuses AUTONOMOUS for the live environment while any required check fails, and `run` and `tick` refuse to start in that state.
- **Rollout sandbox** (`rollout.allowed_targets`). When set, the planners only draft for listed handles, and the gate denies any outbound action to anyone else. That second check runs at proposal and again at execution.
- **Verification** (`verification.py`). `demo`, `scenario` and `browser-demo`. The scenario is deterministic: fixed clock, fixed seed, templates, and action ids derived from idempotency keys. Its transcript is committed as `docs/verification/expected_scenario.txt`.

## Environments

| | `local` | `live` |
|---|---|---|
| Adapters | `SimulatedApiAdapter` + `SimulatedBrowserAdapter` over `SimulatedWorld` (31 fictional `sim.*` accounts, fault injection) | `GraphApiAdapter` (if configured) + `PlaywrightBrowserAdapter` (if enabled) |
| Handles | only `sim.*` accepted | `sim.*` refused |
| Database | `data/local.db` | `data/live.db` |
| Control plane token | optional | **required** (503 without) |
| Website checks | simulated | HTTP (`HttpWebsiteChecker`) |

`devtools/mock_instagram.py` is a Starlette imitation of the Instagram web UI, with `/__control` fault modes:

- login / checkpoint / rate-limit / restricted dialogs;
- notification dialog;
- UI drift;
- send failure;
- pending request.

The real Playwright agent is tested against it, including a live-environment end-to-end run.

## Data model (SQLite by default; any SQLAlchemy URL)

| Table | Purpose |
|---|---|
| `leads` | the business: observation, signals, facts, opportunities, score breakdown, status |
| `lead_sources` | provenance: strategy, query, seed, post, hints |
| `lead_identities` | dedupe keys |
| `profile_snapshots` | observations over time |
| `discovery_runs` | per query key, for cooldowns |
| `conversations` | owner, lease, pause state, thread ids |
| `messages` | direction, sender kind, hash, delivery state (`PENDING`/`SENT`/`FAILED`), intent |
| `actions`, `action_attempts` | the queue and every execution result (with evidence references) |
| `suppressions` | username / IGSID / domain / email / phone |
| `incidents` | barriers and anomalies for a human |
| `lanes` | lane state |
| `usage_events` | ledger for caps and pacing |
| `runtime_settings` | mode, pause, limit overrides (runtime-editable), browser-session verification marker |
| `audit_events` | append-only audit trail, written in the same transaction as each change or decision |
| `learned_locators` | resolver cache |
| `webhook_events` | verified raw deliveries, deduplicated, pruned after 7 days |
