# HK Job Watcher (cloud / always-on, react-to-star)

Scrapes ~17 employers daily for **Hong Kong early-career (intern / graduate) roles**, filters +
tags data/tech, and posts each **new** role as its own **Discord embed card** with reactions:

- **✅ interested** → forwarded to **#starred-jobs** + tracker status `interested`
- **📌 applied** → forwarded to **#starred-jobs** + tracker status `applied`
- **❌ skip** → tracker status `skip`, never shown again

Runs on GitHub Actions — **no laptop required**.

## How it works (two workflows, serverless)
- **`job-watch.yml`** — daily 08:00 HKT: discover → dedup → post embeds + pre-add ✅/📌/❌ →
  record `message_id`s in `_watcher_state.json` `pending` → update tracker → commit back.
- **`reaction-poll.yml`** — every 30 min: read reactions on `pending` messages, forward
  interested/applied to `#starred-jobs`, update tracker, drop handled/stale → commit back.

## One-time setup
1. **Create a Discord bot:** discord.com/developers/applications → New Application → **Bot** →
   Reset Token → copy it. Under **Privileged Gateway Intents** nothing special is needed
   (we use REST, not the gateway).
2. **Invite the bot:** OAuth2 → URL Generator → scope **`bot`** → permissions: *View Channels,
   Send Messages, Embed Links, Read Message History, Add Reactions* → open the URL, add to your server.
3. **Channels:** create **`#starred-jobs`**; pick your alerts channel. Enable Developer Mode
   (User Settings → Advanced) → right-click each channel → **Copy Channel ID**.
4. **Configure:**
   - GitHub repo → Settings → Secrets and variables → Actions → new secret **`DISCORD_BOT_TOKEN`**.
   - Put the two channel IDs in **`bot_config.json`** (`alerts_channel_id`, `starred_channel_id`).
5. Actions tab → run **HK Job Watcher** once to post the current roles as cards.

The old `WATCHER_WEBHOOK` secret is no longer used and can be deleted.

## Files
- `watcher_lib.py` — shared Discord-bot REST + tracker/state IO (no Playwright).
- `_daily_job_watcher.py` — discovery + posts embeds with reactions.
- `_reaction_poller.py` — reads reactions, forwards stars, updates tracker.
- `bot_config.json` — channel IDs (non-secret).
- `applications_tracker.csv` / `JOB_TRACKER.md` — your pipeline + dashboard.

## Local testing
Put a local `_watcher_config.json` (gitignored) with `{"bot_token":"...","alerts_channel_id":"...","starred_channel_id":"..."}`, then:
`python _daily_job_watcher.py --dry`  · `python _daily_job_watcher.py` · `python _reaction_poller.py`
