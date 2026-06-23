"""
Shared helpers for the HK Job Watcher (NO Playwright import -> safe for the lightweight poller).

Provides:
  * Discord bot REST: post_embed / post_text / add_reaction / reaction_users / bot_user_id / make_embed
  * Application tracker IO: load_tracker / update_tracker / set_tracker_status (+ JOB_TRACKER.md)
  * State IO: load_state / save_state   (state now carries a "pending" map of message_id -> job)

Auth: bot token from env DISCORD_BOT_TOKEN (CI secret) or local _watcher_config.json {"bot_token": "..."}.
Channel IDs: bot_config.json {"alerts_channel_id","starred_channel_id","poll_stale_days"}.
"""
import os, sys, csv, json, time, datetime, urllib.request, urllib.parse, urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "_watcher_state.json")
TRACKER_CSV = os.path.join(ROOT, "applications_tracker.csv")
TRACKER_MD = os.path.join(ROOT, "JOB_TRACKER.md")
LOG_FILE = os.path.join(ROOT, "_scrape_out", "watcher.log")

API = "https://discord.com/api/v10"
EMOJI = {"interested": "✅", "applied": "📌", "skip": "❌"}
GREEN, GREY = 0x2ECC71, 0x95A5A6


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------- config / token ----------------

def load_bot_config():
    cfg = load_json(os.path.join(ROOT, "bot_config.json"), {})
    local = load_json(os.path.join(ROOT, "_watcher_config.json"), {})  # local-only overrides (gitignored)
    cfg.update({k: v for k, v in local.items() if v})
    return cfg


def ensure_token(cfg=None):
    """Resolve bot token (env first, then local config) and export to env for _discord()."""
    tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not tok and cfg:
        tok = str(cfg.get("bot_token", "")).strip()
    if tok:
        os.environ["DISCORD_BOT_TOKEN"] = tok
    return tok


# ---------------- Discord bot REST ----------------

def _discord(method, path, body=None, expect_json=True):
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    elif method in ("PUT", "POST", "PATCH", "DELETE"):
        data = b""
    else:
        data = None
    for _ in range(4):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "HK-Job-Watcher (https://github.com/trihieu0510/hk-job-watcher, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if (expect_json and raw) else None
        except urllib.error.HTTPError as ex:
            if ex.code == 429:  # rate limited -> wait and retry
                try:
                    wait = float(json.loads(ex.read().decode("utf-8", "replace")).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait + 0.1, 5)); continue
            raise
    raise RuntimeError(f"Discord {method} {path} failed after retries")


def post_text(channel_id, content):
    return _discord("POST", f"/channels/{channel_id}/messages", {"content": content[:1990]})["id"]


def post_embed(channel_id, embed, content=None):
    body = {"embeds": [embed]}
    if content:
        body["content"] = content[:1990]
    return _discord("POST", f"/channels/{channel_id}/messages", body)["id"]


def add_reaction(channel_id, message_id, emoji):
    e = urllib.parse.quote(emoji)
    _discord("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{e}/@me", expect_json=False)


def reaction_users(channel_id, message_id, emoji):
    e = urllib.parse.quote(emoji)
    res = _discord("GET", f"/channels/{channel_id}/messages/{message_id}/reactions/{e}?limit=100")
    return [u["id"] for u in (res or [])]


def bot_user_id():
    return _discord("GET", "/users/@me")["id"]


def make_embed(job):
    is_dt = job.get("data_tech") in (True, "yes")
    loc = (job.get("location") or "Hong Kong").strip() or "Hong Kong"
    return {
        "title": (job.get("title") or "Role")[:250],
        "url": job.get("url") or None,
        "color": GREEN if is_dt else GREY,
        "fields": [
            {"name": "Company", "value": (job.get("source") or "?")[:250], "inline": True},
            {"name": "Location", "value": loc[:250], "inline": True},
            {"name": "Type", "value": "🟢 Data / Tech" if is_dt else "⚪ Other", "inline": True},
        ],
        "footer": {"text": f"{job.get('source','')} · found {job.get('posted') or job.get('date_found') or ''}"},
    }


# ---------------- application tracker ----------------
TRACK_FIELDS = ["key", "date_found", "source", "title", "location", "data_tech", "status", "url"]


def load_tracker():
    rows = {}
    if os.path.exists(TRACKER_CSV):
        with open(TRACKER_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["key"]] = r
    return rows


def _write_tracker(rows):
    with open(TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACK_FIELDS); w.writeheader()
        for r in rows.values():
            w.writerow({k: r.get(k, "") for k in TRACK_FIELDS})
    today = datetime.date.today().isoformat()
    vals = list(rows.values())
    dt = [r for r in vals if r.get("data_tech") == "yes"]
    other = [r for r in vals if r.get("data_tech") != "yes"]
    star = [r for r in vals if r.get("status") in ("interested", "applied")]

    def line(r):
        s = r.get("status", "new")
        mark = {"interested": "⭐", "applied": "📌", "skip": "🚫"}.get(s, "")
        return (f"- {mark} [{r['title']}]({r['url']}) — **{r['source']}** · "
                f"{r.get('location','') or 'HK'} · _{s}_ (found {r['date_found']})")
    md = [f"# 📋 Job Tracker — updated {today}",
          f"\nTotal **{len(vals)}**  ·  🟢 Data/Tech **{len(dt)}**  ·  ⭐ Starred/Applied **{len(star)}**\n"]
    if star:
        md += ["## ⭐ Shortlist (interested / applied)"]
        md += [line(r) for r in sorted(star, key=lambda r: r["date_found"], reverse=True)]
    md += ["\n## 🟢 Data / Tech"]
    md += [line(r) for r in sorted(dt, key=lambda r: r["date_found"], reverse=True)] or ["_none yet_"]
    md += ["\n## ⚪ Other early-career"]
    md += [line(r) for r in sorted(other, key=lambda r: r["date_found"], reverse=True)] or ["_none yet_"]
    md += ["\n---", "_React in Discord (✅ interested · 📌 applied · ❌ skip) or edit the **status** "
           "column in `applications_tracker.csv`._"]
    with open(TRACKER_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


def update_tracker(entries, today):
    rows = load_tracker()
    added = 0
    for e in entries:
        if e["_key"] not in rows:
            rows[e["_key"]] = {"key": e["_key"], "date_found": today, "source": e["source"],
                               "title": e["title"], "location": e.get("location", ""),
                               "data_tech": "yes" if e["data_tech"] else "no",
                               "status": "new", "url": e["url"]}
            added += 1
    _write_tracker(rows)
    return added, len(rows)


def set_tracker_status(key, status):
    rows = load_tracker()
    if key in rows:
        rows[key]["status"] = status
        _write_tracker(rows)
        return True
    return False


# ---------------- state ----------------

def load_state():
    s = load_json(STATE_FILE, {})
    s.setdefault("seen", [])
    s.setdefault("pending", {})
    return s


def save_state(state):
    state["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
