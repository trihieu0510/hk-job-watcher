"""
Reaction poller (lightweight, stdlib + watcher_lib only -- NO Playwright).
Runs every ~30 min via GitHub Actions. For each message in state["pending"]:
  read who reacted (excluding the bot's own pre-added reactions), resolve a status by precedence
  📌 applied  >  ✅ interested  >  ❌ skip
  -> set the job's tracker status; forward interested/applied to #starred-jobs
  -> drop handled (and >poll_stale_days old) entries from pending.
"""
import os, sys, datetime, time
import watcher_lib as wl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    cfg = wl.load_bot_config()
    token = wl.ensure_token(cfg)
    alerts = str(cfg.get("alerts_channel_id", "")).strip()
    starred = str(cfg.get("starred_channel_id", "")).strip()
    stale_days = int(cfg.get("poll_stale_days", 14))

    if not token or not alerts or not starred:
        wl.log("[poll][error] missing token / alerts_channel_id / starred_channel_id"); return

    state = wl.load_state()
    pending = state.get("pending", {})
    if not pending:
        wl.log("[poll] nothing pending"); return

    try:
        me = wl.bot_user_id()
    except Exception as ex:
        wl.log(f"[poll][error] bot auth failed: {ex}"); return

    today = datetime.date.today()
    handled, forwarded = [], 0

    for mid, job in list(pending.items()):
        try:
            def others(kind):
                return [u for u in wl.reaction_users(alerts, mid, wl.EMOJI[kind]) if u != me]
            status = None
            if others("applied"):
                status = "applied"
            elif others("interested"):
                status = "interested"
            elif others("skip"):
                status = "skip"

            if status:
                wl.set_tracker_status(job["key"], status)
                if status in ("interested", "applied"):
                    tag = "📌 **Applied**" if status == "applied" else "✅ **Starred**"
                    wl.post_embed(starred, wl.make_embed(job), content=tag)
                    forwarded += 1
                wl.log(f"[poll] {status}: {job['key']}")
                handled.append(mid)
            else:
                posted = job.get("posted", "")
                try:
                    if (today - datetime.date.fromisoformat(posted)).days > stale_days:
                        handled.append(mid); wl.log(f"[poll] expired (no reaction): {job['key']}")
                except Exception:
                    pass
            time.sleep(0.25)
        except Exception as ex:
            wl.log(f"[poll][error] msg {mid}: {repr(ex)[:140]}")

    for mid in handled:
        pending.pop(mid, None)
    state["pending"] = pending
    wl.save_state(state)
    wl.log(f"[poll] done; {len(handled)} handled ({forwarded} forwarded to #starred), "
           f"{len(pending)} still pending")


if __name__ == "__main__":
    main()
