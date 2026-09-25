# Deploying: where it runs, how you watch it, how it reaches you

This page covers running the system day to day:

- keeping it running on your computer;
- watching Mission Control from your phone;
- receiving webhooks;
- getting alerts on your phone.

Setting up the Instagram account, Meta and the go-live order (OBSERVE → DRAFT → APPROVAL) is in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md). Do that first.

Items marked ⚠ are third-party menus or commands that change between versions. Check them against what you see.

---

## 1. Where it runs: your own always-on computer

Run it on a computer at home or in the office: a Mac, a Windows PC, a Linux box or a mini PC that stays on during the browser hours (09:30–21:30 IST). **Not a cloud server.**

- **The browser lane uses @lemmedeliver's own logged-in session.** Instagram links a session to the device and network it was created on. A session that suddenly appears on a data-centre IP is a classic trigger for "Confirm it's you" checkpoints. On the computer and home connection you normally use, the lane looks like you.
- **The first login is interactive.** `insta-outreach browser login` opens a visible browser window and you log in yourself, including 2FA. The system never types your password.
- **Everything stays on your machine:**
  - the database (`data/live.db`);
  - the session cookies (`data/browser_profiles/`);
  - screenshots (`data/evidence/`);
  - secrets (`.env`).

Nothing is sent anywhere except to Instagram/Meta, to Anthropic when `ANTHROPIC_API_KEY` is set, and to your alert channel when you configure one.

## 2. Install once

```bash
git clone https://github.com/RohitBagade/insta-outreach.git && cd insta-outreach
python3 -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
insta-outreach setup --service
insta-outreach scenario --check                              # must print IDENTICAL
```

`setup` does the following, and is safe to re-run:

- creates `.env` from `.env.example`;
- generates a long random `CONTROL_API_TOKEN` and prints it once. It never replaces one you already set;
- creates `config/settings.yaml`, the data folders and the database;
- installs Playwright's Chromium;
- with `--service`, writes a start-at-log-in service for your OS (§4) using this folder's real paths, and prints the command to start it.

The rest of `.env` is filled in during [LIVE_CHECKLIST](LIVE_CHECKLIST.md) Phase 1.

## 3. Start it and open Mission Control

```bash
insta-outreach run
```

This starts the orchestrator plus **Mission Control** at <http://127.0.0.1:8765>. The first time, paste the `CONTROL_API_TOKEN`; the tab remembers it until you close it.

| Area | What it shows | What you can do |
|---|---|---|
| **Header** | environment (SIMULATION / LIVE), account, mode, connection, clock | switch mode (OBSERVE, DRAFT, APPROVAL, AUTONOMOUS), **Pause all** |
| **Attention strip** | halted lanes, critical incidents, messages waiting for you, conversations that are yours | jump straight to each |
| **Workflow** | Discover → Analyze → Qualified → Draft → Gate → Send → Conversations, with live counts. A step pulses when something happens in it. The Gate shows *why* queued messages wait (send hours, caps, pacing). Send shows both lanes. | click a step to open its list |
| **Live activity** | every decision, send, reply, handoff, incident and lane change, plus every agent action (discovery searches, profile inspections, inbox reads) | filter: Sends, Decisions, Replies & people, Incidents, Agent activity; click a handle for its full story |
| **Today's limits** | usage vs caps, computed exactly as the gate enforces them; whether sending is allowed now, and when the next send may happen | – |
| **Lanes** | Official API and Browser agent: ACTIVE / COOLDOWN / HALTED, and why | Halt, Resume (after *you* resolved the problem on Instagram) |
| **Go-live readiness** | the `preflight` checks that gate live AUTONOMOUS | – |
| **Approvals** | every message waiting for you, with the facts it uses | edit, approve, reject & redraft, reject |
| **Incidents** | checkpoints, rate limits, restrictions, with the **screenshot** and page URL | resume the lane once resolved |
| **Conversations** | who owns each conversation (automation or you) | take over, hand back |
| **Lead drawer** | the conversation as chat bubbles, and the full decision trail | take over, never contact |

Every button goes through the same audited control service as the CLI. The audit trail records who did what, and nothing on the page can skip a safety check.

**Try it without Instagram first:**

```bash
insta-outreach demo --watch --checkpoint
```

This runs the 4-day simulation slowly (1 s per simulated 10 minutes; change it with `--tick-seconds`) while serving Mission Control on <http://127.0.0.1:8765>. You will see:

- discovery;
- the first DMs;
- a checkpoint halting the browser lane, with a CRITICAL incident;
- human takeovers;
- follow-ups.

It keeps serving until Ctrl+C.

## 4. Keep it running

`insta-outreach setup --service` writes the right one of these for your computer and prints the start and stop commands. The templates below are for reference, or for doing it by hand. Each restarts the program if it crashes and starts it after a reboot and log-in. On Windows, `setup --service` writes `data\run-outreach.cmd` and prints a `schtasks` command that runs it at log-on. For restart-on-failure, also tick *If the task fails, restart every 1 minute* in the task's *Settings* tab ⚠.

> If a start ever fails with `REFUSING TO START` (live + AUTONOMOUS while a preflight check fails, e.g. the browser session expired), run `insta-outreach mode APPROVAL`. The service then starts normally.

<details><summary><b>macOS (launchd)</b></summary>

Save as `~/Library/LaunchAgents/com.lemmedeliver.outreach.plist`, replacing `/Users/rohit/insta-outreach` with your path. `caffeinate -i` keeps the Mac from idle-sleeping while it runs.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.lemmedeliver.outreach</string>
  <key>WorkingDirectory</key><string>/Users/rohit/insta-outreach</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/Users/rohit/insta-outreach/.venv/bin/insta-outreach</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/Users/rohit/insta-outreach/data/outreach.log</string>
  <key>StandardErrorPath</key><string>/Users/rohit/insta-outreach/data/outreach.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lemmedeliver.outreach.plist   # start + at every log-in
launchctl bootout gui/$(id -u)/com.lemmedeliver.outreach                                  # stop
tail -f data/outreach.log
```
</details>

<details><summary><b>Linux (systemd user service)</b></summary>

Save as `~/.config/systemd/user/insta-outreach.service`:

```ini
[Unit]
Description=LemmeDeliver Instagram outreach
After=network-online.target

[Service]
WorkingDirectory=%h/insta-outreach
ExecStart=%h/insta-outreach/.venv/bin/insta-outreach run
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now insta-outreach
loginctl enable-linger "$USER"            # keep running when you are logged out
journalctl --user -u insta-outreach -f    # logs
```
</details>

<details><summary><b>Windows (Task Scheduler)</b> ⚠</summary>

1. Task Scheduler → *Create Task…*.
2. *General*: "Run only when user is logged on".
3. *Triggers*: **At log on**.
4. *Actions* → *Start a program*:
   - Program: `C:\Users\Rohit\insta-outreach\.venv\Scripts\insta-outreach.exe`
   - Arguments: `run`
   - Start in: `C:\Users\Rohit\insta-outreach`
5. *Settings*: "If the task fails, restart every 1 minute".
6. In *Power & sleep*, set sleep to **Never** when plugged in.
</details>

## 5. Watch it from your phone (Tailscale)

Mission Control listens on `127.0.0.1` only: nothing on the internet can reach it. To open it from your phone, use [Tailscale](https://tailscale.com), a free private network between your own devices:

1. Install Tailscale on the computer and on your phone. Log in to the same account on both.
2. In the Tailscale admin console → *DNS*: enable **MagicDNS** and **HTTPS certificates** ⚠.
3. On the computer:

   ```bash
   tailscale serve --bg 8765    # ⚠ Tailscale ≥ 1.52; prints https://<computer>.<tailnet>.ts.net
   tailscale serve status       # check; `tailscale serve reset` to stop
   ```

4. Open that `https://…ts.net` address on your phone and paste the token once. Add it to your home screen.
5. Put the same address in `.env` as `DASHBOARD_URL=…`, so every alert links straight to Mission Control.

Use `tailscale serve` (your devices only), **never `tailscale funnel`** for Mission Control: funnel publishes to the whole internet.

## 6. Alerts on your phone (Telegram)

Alerts go out when a human is needed:

- a **checkpoint**, restriction or login problem that halted a lane (CRITICAL);
- a rate-limit cooldown, or a lane halted because Instagram's page layout changed (WARNING);
- a **warm lead**: someone replied with interest or a question and is handed to you with a suggested answer (WARNING).

With `notifications.min_severity: INFO` you also get the routine ones: new neutral replies, opt-outs, and "you took over @…".

Everything is also in the log and the audit trail. Telegram is free, works on any phone, and needs no app of ours.

1. In Telegram, message **@BotFather** → `/newbot` → choose a name. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=…`.
2. Open your new bot and press **Start** (or send it anything).
3. Run the following, then copy the printed `TELEGRAM_CHAT_ID=…` line into `.env`:

   ```bash
   insta-outreach alerts find-chat
   ```

4. Check that alerts arrive:

   ```bash
   insta-outreach alerts test    # a "🛑 Test alert" arrives on your phone
   ```

5. Restart the service.

Notes:

- Set `notifications.min_severity: CRITICAL` in `config/settings.yaml` for checkpoint-type alerts only.
- **Privacy:** warm-lead alerts quote the prospect's reply, so that text passes through Telegram. If you prefer not to, use `min_severity: CRITICAL`.
- Alerts are plain text: a prospect's message can never turn into a link or formatting trick.

**n8n / Slack / Google Sheets instead (or as well):** set `NOTIFY_WEBHOOK_URL` to an n8n *Webhook* node (or any URL). Every alert is POSTed as JSON:

```json
{"source": "insta-outreach", "severity": "WARNING", "title": "...", "detail": "...", "data": {}}
```

From there n8n can log it to a sheet, post to Slack or create a CRM task. n8n is optional: all the logic stays in the tested Python gate, and n8n only receives notifications.

## 7. Instagram webhooks (optional)

Webhooks make replies and your own manual DMs show up **instantly**. Without them, the inbox is read every 30 minutes (`schedule.inbox_sync_interval_minutes`), so you can start without webhooks. They need a public HTTPS URL. Expose **only** `/webhooks/instagram`:

- the rest (Mission Control, `/api/*`) stays private;
- deliveries without a valid `X-Hub-Signature-256` from your app secret are rejected;
- in live mode, the endpoint refuses to work without `IG_APP_SECRET`.

### Cloudflare Tunnel

This needs a domain on Cloudflare ⚠.

```bash
cloudflared tunnel login
cloudflared tunnel create outreach-hooks
cloudflared tunnel route dns outreach-hooks hooks.<your-domain>
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: outreach-hooks
credentials-file: /Users/rohit/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: hooks.<your-domain>
    path: ^/webhooks/instagram$      # nothing else is reachable
    service: http://127.0.0.1:8765
  - service: http_status:404
```

```bash
cloudflared tunnel run outreach-hooks     # or install as a service: cloudflared service install ⚠
```

The callback URL for Meta is `https://hooks.<your-domain>/webhooks/instagram` (LIVE_CHECKLIST Phase 3).

## 8. Backups, updates, moving machines

- **What to back up:**
  - `data/live.db`: the database;
  - `.env`: secrets;
  - `config/settings.yaml`.
- **Session cookies** (`data/browser_profiles/`) are equivalent to being logged in as @lemmedeliver. Never put them in a shared or public folder. If you move machines, log in again with `insta-outreach browser login` instead of copying them.
- **Consistent database copy while running:**

  ```bash
  python -c "import sqlite3,datetime; sqlite3.connect('data/live.db').backup(sqlite3.connect(f'data/backup-{datetime.date.today()}.db'))"
  ```

  Then move the copy off the machine, e.g. to an encrypted drive.
- **Update:**

  ```bash
  git pull && pip install -e . && insta-outreach scenario --check
  ```

  Then restart the service and check `insta-outreach preflight`.
- **Stop everything instantly:** **Pause all** in Mission Control, or `insta-outreach pause on`. It takes effect before the next action.

## 9. What stays off until you decide

Live AUTONOMOUS is refused by code until `insta-outreach preflight` passes. That needs:

- a verified browser session;
- no open incidents;
- at least 3 human-approved live sends that succeeded;
- your explicit confirmation.

Follow OBSERVE → DRAFT → APPROVAL in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md) first.
