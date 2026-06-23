"""
Daily Hong Kong early-career (intern / graduate) job watcher -- MAX BREADTH.
Multi-ATS fetch -> filter to HK student/intern -> tag data/tech -> diff vs yesterday
-> post NEW roles to Discord/Slack -> maintain a local application tracker.

Source types: render (Playwright) | jpmc (Oracle) | workday | greenhouse | amazon
              | lever | ashby | smartrecruiters

Webhook: _watcher_config.json {"webhook_url": "..."}  OR env WATCHER_WEBHOOK (for CI).
Run:   pythonw _daily_job_watcher.py        (scheduled / CI)
       python  _daily_job_watcher.py --dry  (print only)
       python  _daily_job_watcher.py --seed (save baseline silently, no message)
"""
import os, re, json, sys, csv, datetime, urllib.request, urllib.parse
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "_scrape_out"); os.makedirs(OUT, exist_ok=True)
STATE_FILE = os.path.join(ROOT, "_watcher_state.json")
CONFIG_FILE = os.path.join(ROOT, "_watcher_config.json")
LOG_FILE = os.path.join(OUT, "watcher.log")
TRACKER_CSV = os.path.join(ROOT, "applications_tracker.csv")
TRACKER_MD = os.path.join(ROOT, "JOB_TRACKER.md")
DRY = "--dry" in sys.argv
SEED = "--seed" in sys.argv

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

SOURCES = [
    # rendered JS boards
    {"name": "Jefferies",      "type": "render",
     "url": "https://jefferies.tal.net/candidate/jobboard/vacancy/2/adv"},
    {"name": "Morgan Stanley", "type": "render",
     "url": "https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/brand-2/candidate/jobboard/vacancy/1/adv/"},
    {"name": "Goldman Sachs",  "type": "render",
     "url": "https://higher.gs.com/results?LOCATION=Hong%20Kong&page=1&sort=RELEVANCE"},
    {"name": "UBS",            "type": "render",
     "url": "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5131&PageType=searchResults&SearchType=linkquery&LinkID=6168"},
    {"name": "HSBC",           "type": "render",
     "url": "https://mycareer.hsbc.com/en_GB/external/SearchJobs/?searchByCity=Hong+Kong"},
    # Oracle CE API
    {"name": "JPMorgan",       "type": "jpmc"},
    # Workday cxs API
    {"name": "Citi",           "type": "workday", "host": "citi", "wd": "wd5", "site": "2"},
    {"name": "Bank of America", "type": "workday", "host": "ghr", "wd": "wd1", "site": "lateral-us"},
    # Greenhouse boards
    {"name": "Jane Street",    "type": "greenhouse", "token": "janestreet"},
    {"name": "IMC",            "type": "greenhouse", "token": "imc"},
    {"name": "Point72",        "type": "greenhouse", "token": "point72"},
    {"name": "Jump Trading",   "type": "greenhouse", "token": "jumptrading"},
    {"name": "DRW",            "type": "greenhouse", "token": "drweng"},
    {"name": "Squarepoint",    "type": "greenhouse", "token": "squarepointcapital"},
    {"name": "Stripe",         "type": "greenhouse", "token": "stripe"},
    {"name": "AQR",            "type": "greenhouse", "token": "aqr"},
    # Amazon.jobs API
    {"name": "Amazon",         "type": "amazon"},
    # --- generic adapters ready for future tokens (Lever/Ashby/SmartRecruiters) ---
    # {"name": "X", "type": "lever", "token": "..."},
    # {"name": "Y", "type": "ashby", "token": "..."},
    # {"name": "Z", "type": "smartrecruiters", "token": "..."},
]

HK_RE   = re.compile(r"hong\s*kong|\bHK\b", re.I)
ROLE_RE = re.compile(r"\bintern(?:ship)?s?\b|summer analyst|\bgraduate\b|\btrainee\b|\bcampus\b|"
                     r"off[\s-]?cycle|new analyst|apprentice|placement|working student|"
                     r"summer associate|early career|\bstudent\b", re.I)
EXCLUDE_RE = re.compile(r"recruit|talent acquisition|coordinator|vice president|"
                        r"executive director|managing director|\bhead of\b|chief|supervis", re.I)
DATA_RE = re.compile(r"\bdata\b|analytic|software|engineer|technolog|quant|machine learning|"
                     r"developer|\bAI\b|computer|\bML\b|\bplatform\b|infrastructure|research|scien", re.I)
LINK_RE = re.compile(r"(job|vacancy|intern|analyst|requisition|position|opp/|/roles/)", re.I)
JUNK_RE = re.compile(r"^(share|show more|save|apply|sign in|register|back|view all|see more|"
                     r"next|previous|home|filter)\b", re.I)
# render pages that are roughly location-scoped but still leak other cities/UI noise
HK_IMPLIED = {"UBS", "HSBC"}
OTHER_CITY_RE = re.compile(r"\b(sydney|melbourne|london|singapore|tokyo|new york|shanghai|beijing|"
                           r"mumbai|paris|frankfurt|zurich|geneva|dubai|seoul|sao paulo|toronto|"
                           r"chicago|bangalore|manila|jakarta|kuala lumpur|taipei|osaka)\b", re.I)


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def http_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# ---------------- fetchers: each returns [{title, location, url, source}] ----------------

def fetch_render(page, src):
    out = []
    try:
        page.goto(src["url"], wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"[warn] {src['name']} goto: {e}")
    page.wait_for_timeout(6000)
    for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')",
                "button:has-text('Agree')", "button:has-text('OK')"]:
        try:
            page.click(sel, timeout=1200); page.wait_for_timeout(400)
        except Exception:
            pass
    page.wait_for_timeout(1500)
    base = re.match(r"https?://[^/]+", src["url"])
    base = base.group(0) if base else ""
    for a in page.query_selector_all("a"):
        try:
            t = (a.inner_text() or "").strip().replace("\n", " ")
            h = a.get_attribute("href") or ""
        except Exception:
            continue
        if not t or len(t) > 140 or not LINK_RE.search(t + " " + h):
            continue
        if h.startswith("#") or JUNK_RE.search(t):   # skip UI buttons (Share / Show more / #0)
            continue
        if h.startswith("/"):
            h = base + h
        out.append({"title": re.sub(r"\s+", " ", t), "location": "", "url": h, "source": src["name"]})
    return out


def fetch_jpmc(src):
    finder = 'findReqs;siteNumber=CX_1001,limit=200,keyword="Hong Kong",sortBy="POSTING_DATES_DESC"'
    url = ("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           "?onlyData=true&expand=requisitionList.workLocation&finder="
           + urllib.parse.quote(finder, safe=';=,"'))
    j = json.loads(http_get(url))
    items = j.get("items", [])
    reqs = items[0].get("requisitionList", []) if items else []
    return [{"title": it.get("Title", ""), "location": it.get("PrimaryLocation", ""),
             "url": f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{it.get('Id','')}/",
             "source": "JPMorgan"} for it in reqs]


def fetch_workday(src):
    url = f"https://{src['host']}.{src['wd']}.myworkdayjobs.com/wday/cxs/{src['host']}/{src['site']}/jobs"
    out = []
    for offset in (0, 20, 40):
        try:
            j = json.loads(http_post(url, {"limit": 20, "offset": offset, "searchText": "intern Hong Kong"}))
        except Exception as e:
            log(f"[warn] {src['name']} wd offset {offset}: {e}"); break
        posts = j.get("jobPostings", [])
        if not posts:
            break
        for p in posts:
            out.append({"title": p.get("title", ""), "location": p.get("locationsText", ""),
                        "url": f"https://{src['host']}.{src['wd']}.myworkdayjobs.com/en-US/{src['site']}{p.get('externalPath','')}",
                        "source": src["name"]})
        if len(posts) < 20:
            break
    return out


def fetch_greenhouse(src):
    j = json.loads(http_get(f"https://boards-api.greenhouse.io/v1/boards/{src['token']}/jobs"))
    return [{"title": x.get("title", ""), "location": (x.get("location") or {}).get("name", ""),
             "url": x.get("absolute_url", ""), "source": src["name"]} for x in j.get("jobs", [])]


def fetch_amazon(src):
    url = "https://www.amazon.jobs/en/search.json?loc_query=Hong+Kong&country=HKG&result_limit=100"
    j = json.loads(http_get(url))
    return [{"title": x.get("title", ""), "location": x.get("normalized_location", ""),
             "url": "https://www.amazon.jobs" + x.get("job_path", ""), "source": "Amazon"}
            for x in j.get("jobs", [])]


def fetch_lever(src):
    j = json.loads(http_get(f"https://api.lever.co/v0/postings/{src['token']}?mode=json"))
    return [{"title": x.get("text", ""), "location": (x.get("categories") or {}).get("location", ""),
             "url": x.get("hostedUrl", ""), "source": src["name"]} for x in j]


def fetch_ashby(src):
    j = json.loads(http_get(f"https://api.ashbyhq.com/posting-api/job-board/{src['token']}"))
    return [{"title": x.get("title", ""), "location": x.get("location", ""),
             "url": x.get("jobUrl") or x.get("applyUrl", ""), "source": src["name"]}
            for x in j.get("jobs", [])]


def fetch_smartrecruiters(src):
    j = json.loads(http_get(f"https://api.smartrecruiters.com/v1/companies/{src['token']}/postings?limit=100"))
    out = []
    for x in j.get("content", []):
        loc = x.get("location") or {}
        out.append({"title": x.get("name", ""),
                    "location": f"{loc.get('city','')} {loc.get('country','')}".strip(),
                    "url": f"https://jobs.smartrecruiters.com/{src['token']}/{x.get('id','')}",
                    "source": src["name"]})
    return out


def is_hk_student(e):
    t = e["title"]
    if EXCLUDE_RE.search(t) or not ROLE_RE.search(t):
        return False
    blob = f"{t} {e['location']}"
    if HK_RE.search(blob):
        return True
    # location-scoped render pages: accept unless the title names a different city
    if e["source"] in HK_IMPLIED and not OTHER_CITY_RE.search(blob):
        return True
    return False


# ---------------- application tracker ----------------
TRACK_FIELDS = ["key", "date_found", "source", "title", "location", "data_tech", "status", "url"]

def load_tracker():
    rows = {}
    if os.path.exists(TRACKER_CSV):
        with open(TRACKER_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["key"]] = r
    return rows

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
    with open(TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACK_FIELDS); w.writeheader()
        for r in rows.values():
            w.writerow({k: r.get(k, "") for k in TRACK_FIELDS})
    # markdown dashboard
    vals = list(rows.values())
    dt = [r for r in vals if r.get("data_tech") == "yes"]
    other = [r for r in vals if r.get("data_tech") != "yes"]
    def line(r):
        return (f"- [{r['title']}]({r['url']}) — **{r['source']}** · {r.get('location','') or 'HK'} · "
                f"_{r.get('status','new')}_ (found {r['date_found']})")
    md = [f"# 📋 Job Tracker — updated {today}",
          f"\nTotal tracked: **{len(vals)}**  ·  🟢 Data/Tech: **{len(dt)}**  ·  ⚪ Other: **{len(other)}**\n",
          "## 🟢 Data / Tech"]
    md += [line(r) for r in sorted(dt, key=lambda r: r["date_found"], reverse=True)] or ["_none yet_"]
    md += ["\n## ⚪ Other early-career"]
    md += [line(r) for r in sorted(other, key=lambda r: r["date_found"], reverse=True)] or ["_none yet_"]
    md += ["\n---", "_Edit the **status** column in `applications_tracker.csv` to track progress:_",
           "_new → interested → applied → interview → offer / rejected / skip_"]
    with open(TRACKER_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return added, len(rows)


def send(webhook, text):
    if not webhook:
        log("[warn] no webhook configured -- not sending"); return False
    is_slack = "hooks.slack.com" in webhook
    payload = {"text": text} if is_slack else {"content": text[:1990]}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"), headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HK-Job-Watcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"[ok] webhook sent ({r.status})"); return True
    except Exception as e:
        log(f"[error] webhook send failed: {e}"); return False


def chunk_send(webhook, header, blocks):
    buf, ok = header + "\n\n", True
    for b in blocks:
        if len(buf) + len(b) + 2 > 1900:
            ok = send(webhook, buf) and ok; buf = ""
        buf += b + "\n\n"
    if buf.strip():
        ok = send(webhook, buf) and ok
    return ok


def main():
    cfg = load_json(CONFIG_FILE, {})
    webhook = (cfg.get("webhook_url", "").strip() or os.environ.get("WATCHER_WEBHOOK", "").strip())
    state = load_json(STATE_FILE, {"seen": []})
    seen = set(state.get("seen", []))

    fetchers = {"jpmc": fetch_jpmc, "workday": fetch_workday, "greenhouse": fetch_greenhouse,
                "amazon": fetch_amazon, "lever": fetch_lever, "ashby": fetch_ashby,
                "smartrecruiters": fetch_smartrecruiters}

    all_entries = []
    need_render = any(s["type"] == "render" for s in SOURCES)
    page = browser = pw = None
    if need_render:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 1400}, locale="en-US")
        page = ctx.new_page()

    for src in SOURCES:
        try:
            got = fetch_render(page, src) if src["type"] == "render" else fetchers[src["type"]](src)
            hits = [e for e in got if is_hk_student(e)]
            log(f"[ok] {src['name']}: {len(got)} fetched, {len(hits)} HK student-role(s)")
            all_entries += hits
        except Exception as e:
            log(f"[error] {src['name']}: {repr(e)[:160]}")
    if browser:
        browser.close()
    if pw:
        pw.stop()

    # de-dup with a STABLE key (source + normalized title; URLs carry rotating tokens)
    uniq, keyset = [], set()
    for e in all_entries:
        norm = re.sub(r"\s+", " ", e["title"]).strip().lower()
        k = f"{e['source']}|{norm}"
        if norm and k not in keyset:
            keyset.add(k); e["_key"] = k; e["data_tech"] = bool(DATA_RE.search(e["title"]))
            uniq.append(e)

    new_roles = [e for e in uniq if e["_key"] not in seen]
    new_roles.sort(key=lambda e: (not e["data_tech"], e["source"], e["title"]))
    today = datetime.date.today().isoformat()

    if SEED:
        update_tracker(uniq, today)
        state["seen"] = sorted(seen | {e["_key"] for e in uniq})
        state["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        log(f"[seed] baseline saved: {len(uniq)} roles, tracker populated, no message sent.")
        return

    if new_roles:
        n_dt = sum(1 for e in new_roles if e["data_tech"])
        header = (f"**🌅 HK Internship Watch — {today}**\n"
                  f"**{len(new_roles)} new** early-career role(s) in Hong Kong "
                  f"({n_dt} data/tech 🟢). Tracking {len(uniq)} live.")
        blocks = []
        for e in new_roles:
            tag = "🟢 DATA/TECH" if e["data_tech"] else "⚪ other"
            loc = f" · {e['location']}" if e["location"] else ""
            blocks.append(f"{tag} · {e['source']}{loc}\n{e['title']}\n{e['url']}")
        log("---- message ----\n" + header + "\n\n" + "\n\n".join(blocks) + "\n----")
        if DRY:
            print("\n[DRY RUN] would send the above; state/tracker NOT updated."); return
        ok = chunk_send(webhook, header, blocks)
    else:
        msg = (f"🌅 HK Internship Watch — {today}\n"
               f"No new Hong Kong early-career roles today (tracking {len(uniq)}). Still watching.")
        log("---- message ----\n" + msg + "\n----")
        if DRY:
            print("\n[DRY RUN] would send the above; state/tracker NOT updated."); return
        ok = send(webhook, msg)

    if ok or not new_roles:
        add, tot = update_tracker(new_roles, today)
        log(f"[tracker] +{add} new, {tot} total in applications_tracker.csv")
        state["seen"] = sorted(seen | {e["_key"] for e in uniq})
        state["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    else:
        log("[warn] send failed -- state/tracker NOT updated; will retry next run")


if __name__ == "__main__":
    main()
