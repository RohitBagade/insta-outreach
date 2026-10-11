# Getting started

This is the only page you need to run the outreach bot. Everything after the install happens in **Mission Control**, a page in your browser. No commands.

## 1. Install or update

**Windows:** double-click **`Install or update.cmd`** in the project folder.

It finds Python, installs everything, runs a self-check and opens Mission Control. Run it again any time to get the latest version. Close Mission Control's window first: the file refuses to update a running program.

- **No Python yet?** It tells you so. Install Python 3.12 from <https://www.python.org/downloads/windows/>, tick *Add python.exe to PATH*, then double-click the file again.
- **The folder came from an older version** without `Install or update.cmd`? Open PowerShell in the project folder once and run `git pull`. The file appears, and from then on you only double-click it.

**macOS / Linux:** run `sh install.sh` in the project folder. It does the same.

## 2. Start Mission Control

Double-click **`Mission Control.cmd`** (macOS: `Mission Control.command`). Your browser opens on Mission Control, already signed in.

- **Keep its black window open.** Closing it stops the bot.
- Double-clicking it again while it runs just opens the page again.

The first time, it runs a **simulation**: a pretend Instagram with fictional businesses. The header says **SIMULATION**. Click around, approve a message, try *Find businesses now*. Nothing you do here reaches Instagram.

## 3. Switch to your real Instagram, in test mode

You need a second Instagram account you own, as the **test account**. It is the only account the bot may message while you test.

1. Go to **Settings → Instagram → Switch to my real Instagram…**
2. Type your test account's username. Optionally add a **research account**: an ordinary second account that does the searching, so @lemmedeliver only sends.
3. Press **Switch and restart**.

After the restart, a browser window opens on Instagram's login page. **Log in yourself**, including any code or "confirm it's you" step. The bot never types your password and never answers a security check. When the home feed loads, the window closes and the bot checks the login by opening your own profile, read-only.

## 4. Follow the Get-started card

On the real account, the dashboard shows a **Get started with your real account** card. It lists each step with its own button, and ticks it off when done:

1. Log in to @lemmedeliver (and the research account, if you added one).
2. Test mode is on: only your test account can be messaged, one message a day, no searching.
3. Your test account is added as a business.
4. Switch to **Ask me before sending**.
5. Approve the first message to your test account, on the **Approvals** page.
6. Reply from the test account, and see the reply arrive in **Chats**.
7. **Leave test mode** when you are happy. Searches start, and real businesses get messages for your OK, five first messages a day to begin with. Change the pace in **Settings → Limits & hours**.

## The three modes

The buttons at the top of every page:

| Button | What the bot does |
|---|---|
| **Watch only** | finds and studies businesses; writes and sends nothing |
| **Ask me before sending** | writes each message and waits; nothing goes out until you press Approve |
| **Send by itself** | sends inside the daily limits without asking. 🔒 Locked until every check in **Settings → Before "Send by itself"** passes. Clicking it while locked opens that list. |

**Pause all** stops everything at once. Press it again to resume.

## When Instagram stops the bot

If Instagram shows a security check, a "try again later" or a restriction, the bot **stops that account at once** and a red strip appears on every page. It never tries to get past it.

1. Open Instagram yourself, on your phone or computer, and deal with it there.
2. Back in Mission Control, press **I fixed it on Instagram: resume**.

The **Problems** page shows what happened, with a screenshot.

If the bot was in *Send by itself* and something fails at start-up, it switches itself back to *Ask me before sending* and tells you why.

## Alerts on your phone

**Settings → Phone alerts** walks you through a free Telegram bot. You then get a message when Instagram stops the bot or a business replies with interest.

## If something goes wrong

- **The top of the page says *reconnecting…* for long:** the black window was closed. Double-click `Mission Control.cmd` again.
- **Mission Control.cmd says "Not installed yet":** double-click `Install or update.cmd` first.
- **Nothing opens at all:** open PowerShell in the project folder and run the line below. Send a photo of what it prints.

```
.\.venv\Scripts\python.exe -m insta_outreach run --open
```

## Further reading

- [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md): the full go-live checklist, phase by phase, with what to verify at each step.
- [DEPLOY.md](DEPLOY.md): starting it at log-on, Mission Control on your phone, Meta's official API, webhooks, backups.
- [OPERATIONS.md](OPERATIONS.md): the day-to-day runbook, including the command line.
