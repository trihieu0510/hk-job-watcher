# HK Job Watcher (cloud / always-on, react-to-star)

[![Tests](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/tests.yml/badge.svg)](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/tests.yml)
[![HK Job Watcher](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/job-watch.yml/badge.svg)](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/job-watch.yml)
[![Reaction Poller](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/reaction-poll.yml/badge.svg)](https://github.com/trihieu0510/hk-job-watcher/actions/workflows/reaction-poll.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

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

Both upload their run log as a workflow artifact (`_scrape_out/watcher.log`), retained 14 days —
that is where to look when a source silently returns nothing, since a failing board is logged
rather than raised.

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
- `tests/` — unit tests (stdlib `unittest`, no network).

## Local testing
Put a local `_watcher_config.json` (gitignored) with
`{"bot_token":"...","alerts_channel_id":"...","starred_channel_id":"..."}`, then:

```bash
python _daily_job_watcher.py --help    # the three modes
python _daily_job_watcher.py --dry     # print only; no Discord calls, no state/tracker writes
python _daily_job_watcher.py --seed    # baseline silently: populate tracker + seen, post nothing
python _daily_job_watcher.py           # live run
python _reaction_poller.py             # read reactions, forward stars
```

Unrecognised flags are rejected rather than ignored, so a mistyped `--dryrun` cannot
fall through to a live run that posts to Discord.

## Running the tests

```bash
python -m unittest discover -s tests -t . -v
```

No network, no bot token, no browser — the Discord API and the filesystem are stubbed, and the
tests never touch the real tracker or state file. `watcher_lib` and `_reaction_poller` are
stdlib-only; the only dependency is `playwright`, needed so `_daily_job_watcher` is importable
(the browser itself is never launched). CI runs the suite on every push and pull request.

## Licence
MIT — see [LICENSE](LICENSE).
