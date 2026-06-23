# HK Job Watcher (cloud / always-on)

Scrapes ~17 employers daily for **Hong Kong early-career (intern / graduate) roles**,
filters + tags data/tech, diffs against yesterday, and posts **new** roles to a Discord/Slack
webhook. Runs on GitHub Actions at **08:00 Hong Kong time** every day — no laptop required.

## One-time setup
1. Create a GitHub repo (private is fine) and push this folder to it.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `WATCHER_WEBHOOK`
   - Value: your Discord (or Slack) webhook URL
3. Open the **Actions** tab → enable workflows → run **HK Job Watcher** once via
   **Run workflow** to confirm it posts to your channel.

That's it. After that it runs daily on its own and commits the updated
`_watcher_state.json` + `applications_tracker.csv` + `JOB_TRACKER.md` back to the repo.

## Files
- `_daily_job_watcher.py` — the watcher (reads webhook from the `WATCHER_WEBHOOK` env secret).
- `.github/workflows/job-watch.yml` — daily schedule + Playwright setup + state persistence.
- `applications_tracker.csv` — your living pipeline; edit the **status** column.
- `JOB_TRACKER.md` — readable dashboard, regenerated each run.

## Local use
`python _daily_job_watcher.py --dry`  → preview without sending
`python _daily_job_watcher.py --seed` → reset the baseline silently
