# Live integration checklist for @lemmedeliver

Work through the phases in order and tick each box. Every phase ends with a **pass condition**. Do not start the next phase until it holds. The system enforces the most important one itself: AUTONOMOUS is refused for the live account until every go-live check passes (Mission Control → Settings → *Go-live checks*; see [VERIFY.md §5](VERIFY.md#5-modes-and-why-autonomous-cannot-run-live-without-credentials)).

**Everything below is done in Mission Control.** Start it by double-clicking the *Mission Control* file that `insta-outreach setup` put in the project folder (`Mission Control.cmd` on Windows). It opens the page and signs you in. Closing its window stops the program. Each step also names the command-line equivalent, in case you prefer a terminal.

## Phase 0: accounts and machine

- [ ] **@lemmedeliver is a professional account.** Business is recommended. In the Instagram app: Settings → *Account type and tools* → *Switch to professional account*.
- [ ] **@lemmedeliver's app language is English.** The page-state detectors match English UI texts, e.g. "Confirm it's you" and "Try again later".
- [ ] **A second Instagram account you control, as the test target.** To be qualified by the normal pipeline rather than a bypass, set it up so it looks like a small business:
  - [ ] public;
  - [ ] switched to a **professional (Business)** account with a category such as *Cafe*;
  - [ ] a bio naming a niche and a target location plus a manual booking method, e.g. `Test café in Thane · DM to book`;
  - [ ] **no** link in bio;
  - [ ] at least one post.
  - Phase 1 lowers *Fewest followers worth a message* to 0, so a new account qualifies.
- [ ] **Run everything on your own computer** (macOS / Windows / Linux with a screen). The login window opens on the computer running the program. Keep `data/` on that machine: it holds the session cookies.
- [ ] **Setup verified.** You completed [VERIFY.md](VERIFY.md) §1: `scenario --check` says IDENTICAL, `pytest -q` passes.

## Phase 1: credentials, and switching to your real account

| Credential | Needed for | Where it goes |
|---|---|---|
| Your Instagram login for @lemmedeliver | browser lane | **Nowhere in files.** You type it yourself in the window opened by *Log in…* (Settings → *Instagram accounts*). Only the resulting session cookies are stored, in `data/browser_profiles/lemmedeliver/`. |
| `CONTROL_API_TOKEN` | required in live (kill switch, approvals, Mission Control) | `.env` in the project folder. `insta-outreach setup` creates it. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional: checkpoint and warm-lead alerts on your phone | Settings → *Phone alerts* writes them to `.env` for you ([DEPLOY.md §6](DEPLOY.md#6-alerts-on-your-phone-telegram)). |
| `IG_ACCESS_TOKEN`, `IG_USER_ID` | API lane (optional, Phase 3) | `.env` |
| `IG_APP_SECRET` | verifying webhook signatures | `.env` |
| `IG_WEBHOOK_VERIFY_TOKEN` | webhook handshake: any random string you choose | `.env` |
| `ANTHROPIC_API_KEY` | optional: Claude writes the messages; without it, validated templates are used | `.env` |
| `NOTIFY_WEBHOOK_URL` | optional: incidents and warm leads posted to n8n / Slack relay | `.env` |
| `DASHBOARD_URL` | optional: your Tailscale address for Mission Control, linked in every alert | Settings → *Phone alerts* → *Mission Control link*, or `.env` |

- [ ] **Switch to your real account in test mode.** Settings → *Where it runs* → **Switch to my real Instagram…**:
  - *Test mode*: enter your test account's username. Outbound messages to any other handle become impossible.
  - Keep **Don't look for new businesses yet** ticked: no searching at all during the sandbox.
  - Keep **Show the browser window** ticked to watch the first tests.
  - Press *Switch and restart*. The program restarts on your real account, in **Observe** mode: it sends nothing.
- [ ] **Sandbox limits.** Settings → *Daily limits*: *First messages per day* 1, *First messages per hour* 1, *Follow-ups per business* 0. Settings → *Your messages*: *Fewest followers worth a message* 0, so a brand-new test account can qualify.
- [ ] **Check:** Settings → *Test mode* shows `only @YOUR_TEST_ACCOUNT`; *Safety rules* → *Show every safety rule* lists `new conversations: 1/day, 1/hour`.

Settings you change in Mission Control are saved in `config/settings.dashboard.yaml`, next to `settings.yaml` (which is never rewritten). Delete that file to go back to `settings.yaml` alone. Command line alternative: edit `config/settings.yaml` (sandbox values: `environment: live`, `browser.enabled: true`, `campaigns: []`, `rollout.allowed_targets: [YOUR_TEST_ACCOUNT]`, `scoring.min_followers: 0`, `limits.outreach_per_day: 1`), then `insta-outreach safety`.

**Pass condition:** Settings → *Go-live checks* shows *Control token* ok. *Instagram login* and *Messages you approved* are *not yet*: expected at this point. Until you log in, the bot leaves the browser alone and the dashboard says **Log in to Instagram to start**.

## Phase 2: browser session

- [ ] **Log in.** Settings → *Instagram accounts* → **Log in…** next to @lemmedeliver. A Chromium window opens on Instagram's login page, on the computer running the program.
  - Log in to @lemmedeliver **yourself**, including 2FA or any "confirm it's you" step. The program never types credentials and never answers security checks.
  - The window closes by itself once the home feed loads; the account shows *Done. logged in; session saved…*.
- [ ] **Test the login (read-only).** Press **Test login**. It opens @lemmedeliver's own profile through the normal lane, with the normal pacing, and never messages anyone. Expect *The login works*.
- [ ] **Optional deeper check (command line).** `insta-outreach browser probe --target YOUR_TEST_ACCOUNT --query "cafe Thane"` also compares the profile fields it reads with what you see on Instagram (name, bio, category, followers, website, message button) and runs a search. Any mismatch is UI drift: stop and share the output. The fix is a selector override in a YAML `ui_map`, not a code change.
- [ ] **Go-live checks.** *Instagram login (your account)* is now ok. Login and Test login both record a verified session.

**Pass condition:** Test login says the login works; *Go-live checks* shows only *Messages you approved* not yet passing.

## Phase 2b: research account (recommended)

A second Instagram account does all the searching and profile reading, in its own browser session and lane. @lemmedeliver then only sends messages and reads its own chats.
- If Instagram challenges the research account, finding new businesses pauses but sending continues.
- If it challenges @lemmedeliver, sending stops but research continues.
- Research never falls back to @lemmedeliver.

Instagram can still link accounts used from the same computer or Wi-Fi. This lowers the risk to your brand account; it does not hide the automation.

- [ ] **Create the account.** Use a normal-looking personal account: a real name, a photo, a few follows. Log in on your phone once and use it by hand for a few days before the bot does.
- [ ] **Turn it on.** Settings → *Instagram accounts* → *Use a separate research account*: tick it, enter its username, *Save*, then *Restart now* in the blue bar.
- [ ] **Log in and test.** The research account now has its own row: **Log in…** (log in to the **second** account yourself, including 2FA), then **Test login**. It gets its own browser profile in `data/browser_profiles/research/`.
- [ ] **Go-live checks** show *Instagram login (research account)* ok. The Dashboard's *Account health* lists the *Research account* with its own page-view meter.

Command line alternative: `research.enabled: true` and `research.account.username` in `config/settings.yaml`, then `insta-outreach browser login --account research` and `insta-outreach browser probe --account research`.

**Pass condition:** in Activity (with *Technical details* ticked), searches and profile checks show `via RESEARCH`, never `via BROWSER`.

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
- [ ] **Expose the webhook endpoint.** `insta-outreach run` serves the webhook on `127.0.0.1:8765`. Meta needs a public HTTPS URL, so expose **only** `/webhooks/instagram` through a tunnel or reverse proxy. [DEPLOY.md §7](DEPLOY.md#7-instagram-webhooks-optional) has a Cloudflare Tunnel config that allows exactly that path. Keep `/api/*` and Mission Control private.
- [ ] **Configure the webhook** ⚠. In the app's Webhooks settings:
  - Callback URL: `https://<your-public-host>/webhooks/instagram`
  - Verify token: the value of `IG_WEBHOOK_VERIFY_TOKEN`
  - Subscribe to **`messages`**, **`message_echoes`**, **`comments`**
- [ ] **Enable the lane.** Set `api.enabled: true` in `config/settings.yaml`, then Settings → *Program* → *Restart the program*.
- [ ] **Go-live checks** show *Official Meta API* and *Official API webhooks* ok (command line: `insta-outreach preflight`).

**Pass condition:** Meta's "Verify and save" succeeds. After you send a DM from the test account (Phase 4), Activity shows it was received. Deliveries with a bad signature are rejected (HTTP 403).

## Phase 4: sandbox test with your own test account (OBSERVE → DRAFT → APPROVAL)

This whole phase is rehearsed automatically against the mock site by `pytest tests/test_live_sandbox.py -v`.

Work between **10:00 and 20:00 IST**. Outside the send hours (and the browser hours, 09:30-21:30) everything waits; the Dashboard's top box says what it is waiting for.

- [ ] **Add the test account.** Businesses → **Add a business** → your test account's username, note "sandbox test".
- [ ] **OBSERVE.** The mode switch (top right) is on *Observe*. Within a few minutes the test account is checked.
  - Click it in Businesses: expect *good fit*, correct facts, and opportunities such as *new website* and *online booking*. *Why the bot did what it did* shows every step.
  - Approvals is empty: Observe never writes messages.
- [ ] **DRAFT.** Switch the mode to *Draft*.
  - Approvals shows exactly **one** draft, addressed to your test account.
  - Nothing arrived in the test account's Instagram inbox.
- [ ] **APPROVAL.** Switch the mode to *Approval*, then press **Approve** on the draft. Edit the text first if you like; your edit is checked again.
  - Watch the browser window: it opens the test account's profile, opens the chat, types the exact approved text and sends it.
  - The message arrives in the test account's **Requests** folder.
  - Dashboard → *Latest messages* shows it as *sent*; the business's details show it in the chat, with a screenshot in its trail.
- [ ] **One inbound reply.** From the test account, reply *"yes interested, tell me more"*.
  - With webhooks, it arrives within a minute. Without them, the browser checks the inbox every half hour.
  - Expect the Dashboard's *Today's progress* to say someone is waiting for your reply, and Chats to show the test account as *your turn*. The business is marked *Interested*.
- [ ] **Human takeover.** In Chats, press *Hand back to the bot*. Then send a message to the test account **yourself** from the Instagram app as @lemmedeliver.
  - It is noticed at the next inbox check (or at once with webhooks); any automated send first reads the chat and refuses if it finds a message the bot did not send.
  - Expect the chat to show *you handle this chat* again. Activity says *You messaged @… yourself, so the bot leaves that chat to you*.
- [ ] **Opt-out.** Reply *"please stop"* from the test account.
  - Expect the business *closed*, and Settings → *Never contact* to list the handle (and its IGSID if known).
  - To keep testing, press *Remove* on those rows. Both removals are recorded in the history.
- [ ] **Checkpoint behaviour.** Do **not** try to trigger a real checkpoint. It is proven in simulation and against the mock (VERIFY.md §4).
  - If Instagram shows one on its own, the browser stops, a red strip says *Instagram needs you*, and Problems shows the screenshot.
  - Complete the check yourself in the Instagram app, then press **Resume**.

Command line alternative for this phase: `add-lead`, `mode`, `tick`, `approvals` / `approve`, `messages`, `release`, `suppressions` / `unsuppress`, `lane resume browser`.

**Pass condition, all of the following:**
- exactly the approved message arrived, once;
- the reply was detected and handed to you;
- your own message paused automation;
- the opt-out was suppressed;
- no problem is open;
- Activity tells the whole story.

## Phase 5: the real account (OBSERVE → DRAFT → APPROVAL)

Lift the sandbox in Mission Control:
- Settings → *Test mode*: clear the list, *Save* (it asks you to confirm);
- Campaigns: switch your campaign(s) back on, and check the kinds of business and places;
- Settings → *Your messages*: *Fewest followers worth a message* back to 100;
- Settings → *Daily limits*: keep them low at first, e.g. *First messages per day* 5.

- [ ] **5a. OBSERVE, 1-3 days.** Mode *Observe*. Press **Find businesses now** once if you do not want to wait for the first search.
  - Daily: Businesses and Problems.
  - Spot-check 10 businesses (click them): are the facts true? Are the *not a fit* decisions right?
  - **Pass:** no factual errors in 10 spot checks, no problems, browser activity only within its hours (Activity).
- [ ] **5b. DRAFT, 1-2 days.** Mode *Draft*.
  - Read at least 20 drafts in Approvals.
  - **Pass:** every claim in every draft is true and the tone is right. Use *Reject & rewrite* if not; your reasons go into the history.
- [ ] **5c. APPROVAL, about a week.** Mode *Approval*. Approve drafts one by one in Approvals.
  - Mark how conversations go in each business's details (*Sales stage*: interested, meeting booked, proposal sent, client). Analytics then shows interested and clients per 100 messaged.
  - **Pass:**
    - at least 3 approved sends succeeded (*Go-live checks* → *Messages you approved*);
    - replies were handled correctly;
    - no conversation you handled yourself was touched;
    - no open problem, no stopped account.

## Phase 6: AUTONOMOUS (only when you decide, and only if every go-live check passes)

- [ ] **Check.** Settings → *Go-live checks* shows no *not yet*.
- [ ] **Switch.** Press *Autonomous* in the mode switch. It asks for confirmation, and the attempt is recorded either way.
- [ ] **Watch the first day.** Keep limits low (Settings → *Daily limits*, e.g. 5 a day, 2 an hour). Watch Activity and Problems. Raise limits gradually, if at all.

## Stop and roll back, at any time

| Situation | In Mission Control | Command line |
|---|---|---|
| Stop everything now | **Pause all** (top right) | `insta-outreach pause on` |
| Stop sending, keep preparing | Mode **Approval** (auto-approved items go back to the approval queue) | `insta-outreach mode APPROVAL` |
| Stop the browser only | Dashboard → *Account health* → Browser → **Stop** | `insta-outreach lane halt browser --note "…"` |
| Never contact someone | The business's details → **Never contact**, or Settings → *Never contact* | `insta-outreach suppress @handle` (or `--kind DOMAIN/PHONE/EMAIL`) |
| Take a conversation over | Chats → **Take over** | `insta-outreach claim @handle` |
| Back to the simulation | Settings → *Where it runs* → **Back to the simulation** | `environment: local` in `config/settings.yaml` |
