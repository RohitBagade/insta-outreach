# Research: official APIs vs. browser agent (September 2026)

This document answers two questions:

- what Meta's official Instagram APIs can do for B2B outreach;
- which browser technology to use for the rest.

It explains why the routing table in `execution/executor.py` looks the way it does.

**How this was sourced.** This environment's network policy blocked `developers.facebook.com`. So the Meta facts below come from:

- search-result snippets of the live Meta pages (Sept 2026);
- Meta's official Postman collection (`fbsamples/messenger-platform-samples`);
- recent GitHub mirrors of the Meta docs.

Items that could not be confirmed are marked **[unverified]**. Re-check them against the live pages when setting up the Meta app. Every source link is listed at the end.

## 1. What the official Instagram API can and cannot do

**Two setups** [S1]:

- *Instagram API with Instagram Login*: `graph.instagram.com`, no Facebook Page needed. Scopes: `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments`.
- *Instagram API with Facebook Login*: `graph.facebook.com`, the Instagram account must be linked to a Page. It is the only one with **Business Discovery** and **Hashtag Search**.

The current Graph API version is **v26.0**, released 2026-07-29 [S19]. Serving accounts you don't own, and receiving webhooks, needs Advanced Access. That requires App Review, Business Verification and the app set to Live [S1, S5].

| Capability | Official API? | How | Constraints |
|---|---|---|---|
| Find businesses by keyword or location | **No** | – | Only the Creator Marketplace API, which is creators-only, needs brand eligibility and Advanced Access, and is meant for partnership ads [S18] |
| Look up a known @username | Yes | `business_discovery.username()` | Facebook Login; professional accounts only; **no category field**; personal or missing accounts give 110/2207013 [S6] |
| Read a business's recent posts | Yes | `business_discovery{media{…}}` | Public fields only [S6, S8] |
| Hashtag discovery | Partial | `ig_hashtag_search` → `top_media`/`recent_media` | 30 tags per 7 days; **no username/owner field**, so it can't identify accounts; oEmbed dropped `author_name` (Nov 2025) [S9, S15] |
| Followers / following / similar accounts | **No** | – | Counts only [S7] |
| **Start a DM (cold outreach)** | **No** | – | "Only after an Instagram user has sent … a message can your app send a message … 24 hours to respond" [S2] |
| DM someone who commented on our post | Partial | `POST /{IG_ID}/messages` with `recipient.comment_id` | **One** message per comment, within **7 days**; further messages only after they reply; 750/h [S3, S12] |
| Reply within 24 h of their message | Yes | `POST /{IG_ID}/messages` | Text ≤ 1000 bytes; 100 calls/s [S2] |
| Reply 24 h – 7 days | Partial | `tag: HUMAN_AGENT` | Human-typed replies only; separate App Review [S20] |
| Read DM history | Partial | `/conversations`, `/{conversation_id}?fields=messages` | Only the **20 most recent** messages in detail; Requests threads inactive for 30 days are hidden; 2 calls/s [S4, S14] |
| Inbound events | Yes | webhooks `messages`, `message_echoes`, `comments`, … | Signed `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret); retried for about 36 h [S5] |
| Tell our API sends from human sends | Partial | echo `mid` vs. the `message_id` our send returned | Instagram echoes have `is_echo` for *any* send from the account; no documented `app_id` **[unverified]** [S5, S16] |
| Sender profile (username) | Yes | `GET /{IGSID}` | Only after that person has messaged us ("User consent is required") [S24] |
| Moderate / reply to comments | Yes | `/{media}/comments`, `/{comment}/replies` | Manage-comments permission [S11] |

**Rate limits and errors** [S12, S13, S14] (implemented in `execution/api/client.py`):

- Business Use Case limits are reported in `X-Business-Use-Case-Usage`, including `estimated_time_to_regain_access`.
- Error codes and how they are mapped:
  - rate limits 4, 17, 32, 613 and 80002 → `RATE_LIMITED`;
  - token 190 → `LOGIN_REQUIRED`;
  - outside the window, 10/2534022 → `NOT_PERMITTED`;
  - unreachable recipient 551, 100/2534014;
  - comment not eligible for a private reply, 100/2534025;
  - business blocked from messaging, 100/2534029;
  - DM access disabled in the app settings, 200/2534041.

**2025–2026 changes** [S15, S16, S21]:

- **Removed:** Basic Display API (Dec 2024), the old `share` attachment (Feb 2026), Messenger-only tags on Instagram (Apr 2026).
- **Added:** `message_edit` webhooks, typing indicators, multi-image DMs.
- **No new cold-messaging entry point.** Every entry point still needs the user to act first: click-to-Direct ads, `ig.me` links, ice breakers, story mentions, or a private reply to a comment.

### Consequences for this system

1. **First messages go through the browser lane.** The one exception is people who commented on a LemmeDeliver post: they get an official **private reply** (`PRIVATE_REPLY`, API-only route, with the 7-day and one-message rules enforced in `execution/base.py` and the worker).
2. **Discovery goes through the browser lane:** search, hashtags, locations, followers, similar accounts. Business Discovery is used for inspection when the app uses Facebook Login; otherwise the browser inspects.
3. **Replies prefer the API** inside the 24 h window, since that is the sanctioned path, and fall back to the browser only when the API certainly did not send.
4. **Webhooks are the primary inbound source.** The API shows only 20 messages, so every event is stored when it arrives and deduplicated by payload hash. Echoes are matched to our own sends by `mid` or by the pre-recorded intent text. Anything unmatched means Rohit sent it himself.
5. **Human takeover also comes from thread reads**, as a backstop for missed webhooks.

### Terms of Use

Instagram's [Terms of Use](https://help.instagram.com/581066165581870/) prohibit accessing or collecting information in automated ways without Meta's express permission. The browser lane is therefore **not an authorised integration**. Expect the account to face rate limits, checkpoints or restrictions if activity looks automated.

The design does three things about this:

- keeps activity low and paced;
- stops at every barrier without trying to bypass it;
- keeps the approval workflow available.

It cannot make the browser lane compliant. Meta's docs also ask that automated messaging be disclosed where the law requires it [S1].

## 2. Browser technology decision

Criteria, in the order they were ranked:

1. recoverability after failures;
2. observability;
3. persisted sessions;
4. low maintenance;
5. tolerance of UI variation;
6. Python integration with the orchestrator;
7. deterministic safety boundaries.

Scores are 1–5 (judgment from the evidence below):

| Option (Sept 2026) | Recover | Observe | Persist | Low-maint | UI var. | Python | Safety |
|---|---|---|---|---|---|---|---|
| Playwright, deterministic only | 4 | 5 | 5 | 2 | 2 | 5 | 5 |
| **Playwright + constrained Claude resolver (chosen)** | **5** | **5** | **5** | **4** | **4** | **5** | **5** |
| Claude browser-use tool driven by our Playwright code | 4 | 4 | 5 | 4 | 4 | 4 | 4 |
| Stagehand 4.1 (Python, local) | 3 | 3 | 4 | 2 | 4 | 3 | 3 |
| browser-use 0.13 | 2 | 3 | 4 | 2 | 4 | 2 | 2 |
| Claude computer use | 3 | 3 | – | 4 | 5 | 4 | 2 |
| Playwright MCP / Chrome DevTools MCP | 2 | 4 | 3 | 3 | 4 | 2 | 2 |

**Why not browser-use.** It is pre-1.0, drives Chrome over CDP (it dropped Playwright), and runs an autonomous loop in which the model chooses every action. Its default tools include arbitrary JavaScript evaluation and navigation. It has open hang bugs [B2, B3], pins an old `anthropic` SDK version, and has telemetry on by default. "Resolve an element, never decide" works against how it is built.

**Why not Stagehand.** v4 removed local caching: with a local browser every `act` / `observe` call is an inference call. It uses its own browser driver and extension rather than Playwright, and its selectors are absolute XPaths [B4].

**Why not computer use as the main driver.** It clicks by coordinates from screenshots, with several thousand tokens per step and latency on every action. Anthropic lists "creating content on social platforms" as a limitation, and advises confirming messaging actions in code [B5]. The same goes for the browser-use tool: acceptable as a bounded fallback, never for sending [B6].

**Why not the MCP servers.** Playwright MCP's own README says `--allowed-origins` "does not serve as a security boundary" [B7]. They are good development tools for writing locators, not a runtime.

**What was built** (`execution/browser/`):

- **Playwright** with a persistent context per account, `launch_persistent_context` [B1].
  - Pinned to `playwright>=1.56` because that release matches the Chromium pre-installed in this environment.
  - The newer AI snapshot / `aria-ref` features (1.59+) are not required. `aria-ref` is an internal selector engine, and its refs go stale as soon as the page changes.
  - Instead the resolver tags candidate elements itself with `data-io-cand`.
- **Deterministic first.** Each intent has an ordered list of role / label / placeholder locators. Page-state detectors and benign-dialog rules are data (`ui_map.py`) and can be overridden from YAML.
- **Constrained Claude fallback**, used only when the deterministic path fails. It either:
  - picks one indexed element for a *fixed* intent, which is then validated in code (role, name pattern, visibility, editability, deny-list), converted to a semantic locator, checked to be the same element, and cached; or
  - classifies an unknown page into a fixed set, biased towards stopping.
- **The send path is pure code:**
  1. verify the thread identity;
  2. check history for idempotency and ownership;
  3. type the exact approved text and verify the composer;
  4. re-check the lease;
  5. send, and confirm the message appears.
- **Observability:** screenshots, text evidence and Playwright traces on failure (`playwright show-trace`), every attempt stored in `action_attempts`, and incidents carrying the URL and screenshot.

## Sources

Meta / Instagram platform:

- S1 https://developers.facebook.com/docs/instagram-platform/overview/
- S2 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/
- S3 https://developers.facebook.com/docs/instagram-platform/private-replies/
- S4 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api/
- S5 https://developers.facebook.com/docs/instagram-platform/webhooks
- S6 https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery/ and https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/
- S7 https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/
- S8 https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/
- S9 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/hashtag-search/ , https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-hashtag/top-media/ , https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-hashtag/recent-media/
- S10 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/mentions and https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/mentions
- S11 https://developers.facebook.com/docs/instagram-platform/comment-moderation
- S12 https://developers.facebook.com/docs/graph-api/overview/rate-limiting/
- S13 https://developers.facebook.com/docs/graph-api/guides/error-handling/
- S14 https://developers.facebook.com/docs/messenger-platform/error-codes
- S15 https://developers.facebook.com/docs/instagram-platform/changelog/
- S16 https://developers.facebook.com/docs/messenger-platform/changelog/
- S17 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
- S18 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/creator-marketplace/
- S19 https://developers.facebook.com/blog/post/2026/07/29/introducing-graph-api-v26-and-marketing-api-v26/
- S20 https://github.com/fbsamples/messenger-platform-samples/blob/main/postman/instagram-platform-api.postman_collection.json
- S21 https://developers.facebook.com/blog/post/2024/09/04/update-on-instagram-basic-display-api/
- S22 https://developers.facebook.com/docs/messenger-platform/policy/policy-overview/
- S23 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/ice-breakers and https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/ig-me
- S24 https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/user-profile
- S25 https://developers.facebook.com/docs/pages-api/search-pages/
- S26 https://developers.facebook.com/community/threads/310930884925810/
- Instagram Terms of Use: https://help.instagram.com/581066165581870/

Browser tooling:

- B1 Playwright for Python: https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context , https://playwright.dev/python/docs/auth , https://playwright.dev/python/docs/trace-viewer , https://playwright.dev/python/docs/aria-snapshots , releases https://github.com/microsoft/playwright-python/releases
- B2 browser-use: https://github.com/browser-use/browser-use (issues #5067, #4579), https://browser-use.com/posts/playwright-to-cdp
- B3 browser-use references: https://github.com/browser-use/browser-use/tree/main/skills/open-source/references
- B4 Stagehand: https://github.com/browserbase/stagehand , https://docs.stagehand.dev/v4/best-practices/caching , https://docs.stagehand.dev/v4/migrations/v3
- B5 Claude computer use: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- B6 Claude browser use tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool
- B7 Playwright MCP: https://github.com/microsoft/playwright-mcp ; Chrome DevTools MCP: https://github.com/ChromeDevTools/chrome-devtools-mcp
- Structured outputs (used by the composer and resolver): https://platform.claude.com/docs/en/build-with-claude/structured-outputs
