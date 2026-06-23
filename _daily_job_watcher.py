"""
Daily Hong Kong early-career (intern / graduate) job watcher -- MAX BREADTH.
Multi-ATS fetch -> filter to HK student/intern -> tag data/tech -> diff vs yesterday
-> post each NEW role as a Discord EMBED (bot) with ✅/📌/❌ reactions pre-added
-> record message_id in state["pending"] for the reaction poller -> update tracker.

Source types: render (Playwright) | jpmc (Oracle) | workday | greenhouse | amazon
              | lever | ashby | smartrecruiters

Auth: bot token via env DISCORD_BOT_TOKEN (CI secret) or local _watcher_config.json {"bot_token"}.
Channels: bot_config.json {"alerts_channel_id", ...}.
Run:   python _daily_job_watcher.py          (post new roles)
       python _daily_job_watcher.py --dry     (print only, no posting/state change)
       python _daily_job_watcher.py --seed    (baseline silently: tracker + seen, no posting)
"""
import os, re, sys, time, datetime, json, urllib.request, urllib.parse
from playwright.sync_api import sync_playwright
import watcher_lib as wl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(ROOT, "_scrape_out"), exist_ok=True)
DRY = "--dry" in sys.argv
SEED = "--seed" in sys.argv
log = wl.log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

SOURCES = [
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
    {"name": "JPMorgan",       "type": "jpmc"},
    {"name": "Citi",           "type": "workday", "host": "citi", "wd": "wd5", "site": "2"},
    {"name": "Bank of America", "type": "workday", "host": "ghr", "wd": "wd1", "site": "lateral-us"},
    {"name": "Jane Street",    "type": "greenhouse", "token": "janestreet"},
    {"name": "IMC",            "type": "greenhouse", "token": "imc"},
    {"name": "Point72",        "type": "greenhouse", "token": "point72"},
    {"name": "Jump Trading",   "type": "greenhouse", "token": "jumptrading"},
    {"name": "DRW",            "type": "greenhouse", "token": "drweng"},
    {"name": "Squarepoint",    "type": "greenhouse", "token": "squarepointcapital"},
    {"name": "Stripe",         "type": "greenhouse", "token": "stripe"},
    {"name": "AQR",            "type": "greenhouse", "token": "aqr"},
    {"name": "Amazon",         "type": "amazon"},
    {"name": "OKX",            "type": "greenhouse", "token": "okx"},
    {"name": "Bybit",          "type": "greenhouse", "token": "bybit"},
    {"name": "Lalamove",       "type": "lever",      "token": "lalamove"},
    # JobsDB (HK's main board, SEEK API) -- whole-market coverage, data/tech keywords only
    {"name": "JobsDB",         "type": "jobsdb",
     "keywords": ["data", "software engineer", "data analyst", "machine learning",
                  "analytics", "quantitative", "business intelligence", "data scientist"]},
    # generic adapters ready for future tokens:
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
HK_IMPLIED = {"UBS", "HSBC", "JobsDB"}  # query is already Hong-Kong-scoped
OTHER_CITY_RE = re.compile(r"\b(sydney|melbourne|london|singapore|tokyo|new york|shanghai|beijing|"
                           r"mumbai|paris|frankfurt|zurich|geneva|dubai|seoul|sao paulo|toronto|"
                           r"chicago|bangalore|manila|jakarta|kuala lumpur|taipei|osaka)\b", re.I)


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
        if h.startswith("#") or JUNK_RE.search(t):
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


def fetch_jobsdb(src):
    """JobsDB (SEEK) HK board -- run several data/tech keyword searches, HK-scoped."""
    out = []
    for kw in src.get("keywords", ["data"]):
        url = ("https://hk.jobsdb.com/api/jobsearch/v5/search?siteKey=HK-Main&sourcesystem=houston"
               f"&keywords={urllib.parse.quote(kw)}&where=Hong+Kong&page=1&pageSize=30")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json", "seek-request-country": "HK"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            log(f"[warn] JobsDB '{kw}': {repr(e)[:100]}"); continue
        for x in j.get("data", []):
            locs = x.get("locations") or []
            loc = (locs[0].get("label") if locs else "") or "Hong Kong"
            out.append({"title": x.get("title", ""), "location": loc,
                        "url": f"https://hk.jobsdb.com/job/{x.get('id','')}",
                        "source": "JobsDB"})
    return out


def is_hk_student(e):
    t = e["title"]
    if EXCLUDE_RE.search(t) or not ROLE_RE.search(t):
        return False
    blob = f"{t} {e['location']}"
    if HK_RE.search(blob):
        return True
    if e["source"] in HK_IMPLIED and not OTHER_CITY_RE.search(blob):
        return True
    return False


def discover():
    fetchers = {"jpmc": fetch_jpmc, "workday": fetch_workday, "greenhouse": fetch_greenhouse,
                "amazon": fetch_amazon, "lever": fetch_lever, "ashby": fetch_ashby,
                "smartrecruiters": fetch_smartrecruiters, "jobsdb": fetch_jobsdb}
    all_entries = []
    need_render = any(s["type"] == "render" for s in SOURCES)
    browser = pw = page = None
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
    return uniq


def main():
    cfg = wl.load_bot_config()
    token = wl.ensure_token(cfg)
    alerts = str(cfg.get("alerts_channel_id", "")).strip()

    state = wl.load_state()
    seen = set(state.get("seen", []))

    uniq = discover()
    new_roles = [e for e in uniq if e["_key"] not in seen]
    new_roles.sort(key=lambda e: (not e["data_tech"], e["source"], e["title"]))
    today = datetime.date.today().isoformat()

    if SEED:
        wl.update_tracker(uniq, today)
        state["seen"] = sorted(seen | {e["_key"] for e in uniq})
        wl.save_state(state)
        log(f"[seed] baseline saved: {len(uniq)} roles, tracker populated, no message sent.")
        return

    n_dt = sum(1 for e in new_roles if e["data_tech"])
    if new_roles:
        header = (f"🌅 **HK Internship Watch — {today}**\n"
                  f"**{len(new_roles)} new** role(s) ({n_dt} data/tech 🟢) · Tracking {len(uniq)} live\n"
                  f"React on each: ✅ interested → #starred-jobs · 📌 applied · ❌ skip")
    else:
        header = (f"🌅 **HK Internship Watch — {today}** — no new roles today "
                  f"(tracking {len(uniq)}). Still watching.")

    if DRY:
        log("[DRY] header:\n" + header)
        for e in new_roles:
            log(f"[DRY] would post: {e['source']} | {e['title']} | {e['url']}")
        print(f"\n[DRY RUN] {len(new_roles)} new role(s); nothing posted, state/tracker unchanged.")
        return

    if not token or not alerts:
        log("[error] missing DISCORD_BOT_TOKEN or alerts_channel_id -- cannot post. "
            "Set the secret and bot_config.json."); return

    # post header, then one embed per new role with reactions pre-added
    try:
        wl.post_text(alerts, header)
    except Exception as ex:
        log(f"[error] header post failed: {ex}"); return

    posted = 0
    for e in new_roles:
        try:
            mid = wl.post_embed(alerts, wl.make_embed({**e, "posted": today}))
            for emo in (wl.EMOJI["interested"], wl.EMOJI["applied"], wl.EMOJI["skip"]):
                wl.add_reaction(alerts, mid, emo); time.sleep(0.3)
            state["pending"][str(mid)] = {"key": e["_key"], "title": e["title"], "url": e["url"],
                                          "source": e["source"], "location": e.get("location", ""),
                                          "data_tech": e["data_tech"], "posted": today}
            posted += 1
            time.sleep(0.3)
        except Exception as ex:
            log(f"[error] post '{e['_key']}': {repr(ex)[:140]}")

    add, tot = wl.update_tracker(new_roles, today)
    state["seen"] = sorted(seen | {e["_key"] for e in uniq})
    wl.save_state(state)
    log(f"[done] posted {posted}/{len(new_roles)} new; tracker +{add} ({tot} total); "
        f"{len(state['pending'])} pending reactions")


if __name__ == "__main__":
    main()
