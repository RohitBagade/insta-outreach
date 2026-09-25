# Live integration checklist for @lemmedeliver

Work through the phases in order and tick each box. Every phase ends with a **pass condition**. Do not start the next phase until it holds. The system enforces the most important one itself: AUTONOMOUS is refused for the live account until `insta-outreach preflight` passes (see [VERIFY.md §5](VERIFY.md#5-modes-and-why-autonomous-cannot-run-live-without-credentials)).

> **Risk, once more.** Instagram's Terms of Use prohibit automated access without Meta's permission. The browser lane automates the normal web UI, so the account can be rate-limited, checkpointed or restricted. The API-only features (replies within 24 h, private replies to comments, webhooks) are within Meta's rules.

> **About the Meta steps.** Meta's documentation site was blocked from the environment where this was built. The steps below come from search snippets of Meta's pages and Meta's official Postman collection (sources in [RESEARCH.md](RESEARCH.md#sources)). Menu names in the Meta dashboard and the Instagram app change often, so items marked ⚠ need checking against what you see.

---

## Phase 0: accounts and machine

- [ ] **@lemmedeliver is a professional account.** Business is recommended. In the Instagram app: Settings → *Account type and tools* → *Switch to professional account*.
- [ ] **@lemmedeliver's app language is English.** The page-state detectors match English UI texts, e.g. "Confirm it's you" and "Try again later".
- [ ] **A second Instagram account you control, as the test target.** To be qualified by the normal pipeline rather than a bypass, set it up so it looks like a small business:
  - [ ] public;
  - [ ] switched to a **professional (Business)** account with a category such as *Cafe*;
  - [ ] a bio naming a niche and a target location plus a manual booking method, e.g. `Test café in Thane · DM to book`;
  - [ ] **no** link in bio;
  - [ ] at least one post.
  - The sandbox config below lowers `min_followers` to 0, so a new account qualifies.
- [ ] **Run everything on your own computer** (macOS / Windows / Linux with a screen). The first login opens a visible browser window. Keep `data/` on that machine: it holds the session cookies.
- [ ] **Setup verified.** You completed [VERIFY.md](VERIFY.md) §1: `scenario --check` says IDENTICAL, `pytest -q` passes.

## Phase 1: credentials and where they go

| Credential | Needed for | Where it goes |
|---|---|---|
| Your Instagram login for @lemmedeliver | browser lane | **Nowhere in files.** You type it yourself in the window opened by `insta-outreach browser login`. Only the resulting session cookies are stored, in `data/browser_profiles/lemmedeliver/`. |
| `CONTROL_API_TOKEN` | required in live (kill switch, approvals, console) | `.env` in the repository root. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `IG_ACCESS_TOKEN` | API lane (optional, Phase 3) | `.env` |
| `IG_USER_ID` | API lane: the Instagram professional account id | `.env` |
| `IG_APP_SECRET` | verifying webhook signatures | `.env` |
| `IG_WEBHOOK_VERIFY_TOKEN` | webhook handshake: any random string you choose | `.env` |
| `ANTHROPIC_API_KEY` | optional: Claude writes the messages; without it, validated templates are used | `.env` |
| `NOTIFY_WEBHOOK_URL` | optional: incidents and warm leads posted to n8n / Slack relay | `.env` |

- [ ] **Create `.env`.** Run `cp .env.example .env`, then fill in `CONTROL_API_TOKEN`.
  - The CLI loads `.env` automatically; variables already set in your shell win.
  - `.env` is git-ignored.
- [ ] **Create `config/settings.yaml`.** Run `insta-outreach init`, then replace its content with the **sandbox config**:

```yaml
environment: live
default_mode: OBSERVE
data_dir: data

account:
  id: lemmedeliver
  username: lemmedeliver

browser:
  enabled: true
  headless: false            # watch the browser during the first tests
api:
  enabled: false             # switch on in Phase 3

campaigns: []                # sandbox: no discovery at all
rollout:
  allowed_targets: [YOUR_TEST_ACCOUNT]   # outbound is IMPOSSIBLE to any other handle
scoring:
  min_followers: 0           # so a brand-new test account can qualify
limits:
  outreach_per_day: 1
  outreach_per_hour: 1
  max_followups_per_lead: 0  # no follow-ups during the sandbox test
```

- [ ] **Check:** `insta-outreach safety` shows `sandbox allowlist (rollout.allowed_targets): @YOUR_TEST_ACCOUNT` and `new conversations: 1/day, 1/hour`.

**Pass condition:** `insta-outreach preflight` shows `control_token PASS`. `browser_session` and `approved_sends` still FAIL, which is expected at this point.

## Phase 2: browser session

- [ ] **Log in.** Run `insta-outreach browser login`. A Chromium window opens on Instagram's login page.
  - Log in to @lemmedeliver **yourself**, including 2FA or any "confirm it's you" step. The tool never types credentials and never answers security checks.
  - The command ends with `logged in; session saved in data/browser_profiles/lemmedeliver`.
- [ ] **Probe (read-only).** Run `insta-outreach browser probe --target YOUR_TEST_ACCOUNT --query "cafe Thane"`. It opens pages but never messages anyone. Check that:
  - [ ] `session.state` is `ok`;
  - [ ] the extracted profile fields match what you see on Instagram: name, bio, category, followers, posts, website none, private false;
  - [ ] `intents` shows `profile.message_button` resolved;
  - [ ] `search.candidates` contains real accounts.
  - Any mismatch is UI drift: stop and share the output. The fix is a selector override in a YAML `ui_map`, not a code change.
- [ ] **Preflight.** Run `insta-outreach preflight`. `browser_session` should now be PASS. Login and probe both record a verified session.

**Pass condition:** the probe output matches reality. `preflight` shows only `approved_sends` failing.

## Phase 3: Meta app and webhooks (optional, recommended)

The browser lane works without this. The API adds, within Meta's rules:
- real-time replies and human-takeover detection (webhook echoes);
- API replies inside the 24 h window;
- private replies to comments on your posts.

You can come back to this phase after Phase 4.

- [ ] **Create the app.** At developers.facebook.com: *My Apps* → *Create app*. Use case ⚠: *Manage messaging & content on Instagram*, i.e. **Instagram API with Instagram Login**, which needs no Facebook Page.
- [ ] **Add @lemmedeliver and generate a token** ⚠. In the app dashboard: *Instagram* → *API setup with Instagram business login* → add the account and generate an access token. Grant these permissions:
  - `instagram_business_basic`
  - `instagram_business_manage_messages`
  - `instagram_business_manage_comments`
- [ ] **Fill in `.env`.**
  - Copy the token into `IG_ACCESS_TOKEN` and the account id into `IG_USER_ID`.
  - Put the *App secret* (App settings → Basic) into `IG_APP_SECRET`.
  - Long-lived tokens last about 60 days. When one expires the API lane halts with a `LOGIN_REQUIRED` incident; refresh the token and resume the lane.
- [ ] **Allow API access to messages** ⚠. In the Instagram app, as @lemmedeliver: Settings → *Messages and story replies* → *Message controls* → *Connected tools* → **Allow access to messages** ON. If it is off, the API returns error 2534041, which the system reports as `HUMAN_ACTION_REQUIRED`.
- [ ] **Development mode needs testers** ⚠. In development mode the API only serves accounts with a role on the app. Add your test account as an **Instagram tester** (App roles → Roles), then accept the invite as that account: instagram.com → Settings → *Apps and websites* → *Tester invites*.
  - For real prospects you will later need **Advanced Access**: App Review + Business Verification + app set to Live.
  - Outreach-style use may be hard to justify in App Review.
- [ ] **Expose the webhook endpoint.** `insta-outreach run` serves the webhook on `127.0.0.1:8765`. Meta needs a public HTTPS URL, so expose **only** `/webhooks/instagram` through a tunnel or reverse proxy (e.g. Cloudflare Tunnel, ngrok, Caddy). Keep `/api/*` and the console private.
- [ ] **Configure the webhook** ⚠. In the app's Webhooks settings:
  - Callback URL: `https://<your-public-host>/webhooks/instagram`
  - Verify token: the value of `IG_WEBHOOK_VERIFY_TOKEN`
  - Subscribe to **`messages`**, **`message_echoes`**, **`comments`**
- [ ] **Enable the lane.** Set `api.enabled: true` in `config/settings.yaml`.
- [ ] **Preflight.** `insta-outreach preflight` shows `api_credentials PASS` and `webhooks PASS`.

**Pass condition:** Meta's "Verify and save" succeeds. After you send a DM from the test account (Phase 4), `insta-outreach audit` shows it was received. Deliveries with a bad signature are rejected (HTTP 403).

## Phase 4: sandbox test with your own test account (OBSERVE → DRAFT → APPROVAL)

This whole phase is rehearsed automatically against the mock site by `pytest tests/test_live_sandbox.py -v`.

Work between **10:00 and 20:00 IST**. Outside the send hours (and the browser hours, 09:30-21:30) the gate defers everything, and `tick` does nothing visible. `insta-outreach action <id>` would show `DEFER - outside send hours`.

- [ ] **Add the test lead.** Run `insta-outreach add-lead @YOUR_TEST_ACCOUNT --note "sandbox test"`.
- [ ] **OBSERVE.** Run `insta-outreach mode OBSERVE`, then `insta-outreach tick -n 3`. Use `tick` so you can watch one step at a time.
  - Check `insta-outreach explain @YOUR_TEST_ACCOUNT`. Expect status `QUALIFIED` and correct facts. Opportunities should include `NEW_WEBSITE` and `ONLINE_BOOKING`.
  - Check `insta-outreach generated all`. Expect `(none)`.
- [ ] **DRAFT.** Run `insta-outreach mode DRAFT`, then `insta-outreach tick -n 2`.
  - `insta-outreach approvals` shows exactly **one** draft, addressed to your test account.
  - Nothing arrived in the test account's Instagram inbox.
- [ ] **APPROVAL.** Run `insta-outreach mode APPROVAL`.
  - Approve the draft with `insta-outreach approve <act_…id>`. To change the wording, add `--message "…"`; the edit is validated again.
  - Then run `insta-outreach tick -n 3` and watch the browser. It opens the test account's profile, opens the thread, types the exact approved text and sends it.
  - The message arrives in the test account's **Requests** folder.
  - `insta-outreach messages @YOUR_TEST_ACCOUNT` shows it `-> out BROWSER_AGENT SENT`.
  - `insta-outreach action <id>` shows the screenshot path, taken after sending.
- [ ] **One inbound reply.** From the test account, reply *"yes interested, tell me more"*.
  - With webhooks, it arrives within a tick.
  - Without them, the browser checks the inbox once per half hour. Run `insta-outreach tick` again after the next :00 or :30.
  - Expect lead `HANDED_OFF`. `insta-outreach conversations --paused` lists the test account.
  - `audit --kind reply` shows `reply classified INTERESTED … handed off to Rohit`.
- [ ] **Human takeover.** Run `insta-outreach release @YOUR_TEST_ACCOUNT`. Then send a message to the test account **yourself** from the Instagram app as @lemmedeliver.
  - With webhooks, it is noticed at the next tick; without them, at the next half-hourly inbox check.
  - Either way, any automated send first reads the thread and refuses if it finds a message automation did not send.
  - Expect owner `HUMAN` and automation paused (`audit --kind conversation`).
  - Run `release` again afterwards if you want to continue.
- [ ] **Opt-out.** Reply *"please stop"* from the test account.
  - Expect lead `CLOSED`. `insta-outreach suppressions` shows the handle (and IGSID if known).
  - To keep testing: `insta-outreach unsuppress YOUR_TEST_ACCOUNT`. If an IGSID row is listed too, also run `insta-outreach unsuppress <id> --kind IGSID`. Both are recorded in the audit trail.
- [ ] **Checkpoint behaviour.** Do **not** try to trigger a real checkpoint. It is proven in simulation and against the mock (VERIFY.md §4).
  - If Instagram shows one on its own, you will see a CRITICAL incident with a screenshot.
  - Complete the check yourself in the Instagram app, then run `insta-outreach lane resume browser --note "…"`.

**Pass condition, all of the following:**
- exactly the approved message arrived, once;
- the reply was detected and handed to you;
- your own message paused automation;
- the opt-out was suppressed;
- no incident is open;
- `insta-outreach audit` tells the whole story.

## Phase 5: the real account (OBSERVE → DRAFT → APPROVAL)

Remove the sandbox from `config/settings.yaml`:
- delete `rollout.allowed_targets`, `campaigns: []` and the `scoring`/`limits` overrides;
- or restore the campaign block from `config/settings.example.yaml`;
- keep limits low at first, e.g. `outreach_per_day: 5`.

- [ ] **5a. OBSERVE, 1-3 days.** Run `insta-outreach mode OBSERVE`, then `insta-outreach run` (orchestrator + console).
  - Daily: `insta-outreach leads`, `insta-outreach incidents`.
  - Spot-check 10 leads with `explain`: are the facts true? Are the disqualifications right?
  - **Pass:** no factual errors in 10 spot checks, no incidents, browser activity within hours (`audit`).
- [ ] **5b. DRAFT, 1-2 days.** Run `insta-outreach mode DRAFT`.
  - Read at least 20 drafts in `insta-outreach generated outreach`.
  - **Pass:** every claim in every draft is true and the tone is right. Reject with `--redraft` if not; the reasons go into the audit trail.
- [ ] **5c. APPROVAL, about a week.** Run `insta-outreach mode APPROVAL`. Approve drafts one by one: console or `approvals` / `approve` / `reject`.
  - **Pass:**
    - at least 3 approved sends succeeded (this is `preflight`'s `approved_sends`);
    - replies were handled correctly;
    - no conversation you handled yourself was touched;
    - no open incident, no halted lane.

## Phase 6: AUTONOMOUS (only when you decide, and only if preflight passes)

- [ ] **Check preflight.** `insta-outreach preflight` lists no FAIL and ends with `all required checks pass`.
- [ ] **Switch.** Run `insta-outreach mode AUTONOMOUS`. It asks for confirmation, and the attempt is audited either way.
- [ ] **Watch the first day.** Keep caps low, e.g. `insta-outreach limits --set outreach_per_day=5 outreach_per_hour=2`. Watch `audit --kind message.sent` and `incidents`. Raise caps gradually, if at all.

## Stop and roll back, at any time

| Situation | Command |
|---|---|
| Stop everything now | `insta-outreach pause on` |
| Stop sending, keep preparing | `insta-outreach mode APPROVAL` (auto-approved items go back to the approval queue) |
| Stop the browser only | `insta-outreach lane halt browser --note "…"` |
| Never contact someone | `insta-outreach suppress @handle` (or `--kind DOMAIN/PHONE/EMAIL`) |
| Take a conversation over | `insta-outreach claim @handle` |
