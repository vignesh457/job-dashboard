"""
job_tracker.py

Reads companies_matched.json (produced by discover_ats.py), pulls current
job postings from each company's Greenhouse/Lever/SmartRecruiters board,
filters for relevant titles, diffs against previously-seen postings, and:
  - appends new matches to data/new_jobs.md
  - optionally sends a Telegram notification

Run:
  python job_tracker.py
"""

import json
import os
import re
from datetime import datetime, timezone

import requests

# ---- Config -----------------------------------------------------------

KEYWORDS = [
    "frontend", "front end", "backend", "back end", "developer",
    "software engineer", "software developer", "sde",
    "software development engineer", "full stack", "fullstack",
    "full stack developer", "frontend developer", "backend developer",
    "web developer", "application developer", "mern", "react",
    "react developer", "javascript", "typescript", "node", "node developer",
    "java", "java developer", "spring", "spring boot", "ui",
    "ui developer", "ui engineer",
]

# Set EXCLUDE_SENIOR=false as an env var if you don't want this filter
EXCLUDE_KEYWORDS = [
    "staff", "principal", "director", "manager", "vp ", "head of",
    "architect", "lead ", " sr.", "senior director", "senior staff",
]

# Matched against the job's location field. "india" catches most explicit
# labels; the city list catches postings that only list a city.
LOCATION_KEYWORDS = [
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "chennai",
    "mumbai", "delhi", "gurgaon", "gurugram", "noida", "kolkata",
    "ahmedabad", "jaipur", "kochi", "coimbatore", "indore", "chandigarh",
    "nagpur", "gandhinagar", "trivandrum", "thiruvananthapuram",
]

# How many days old a posting can be and still count as "fresh" on the
# very first run (when everything currently open would otherwise show up
# as "new"). Set MAX_POSTING_AGE_DAYS=0 as an env var to disable this and
# show everything regardless of age.
MAX_POSTING_AGE_DAYS = int(os.environ.get("MAX_POSTING_AGE_DAYS", "30"))

MATCHED_FILE = "companies_matched.json"
SEEN_FILE = "data/seen_jobs.json"
OUTPUT_FILE = "data/new_jobs.md"
BOARD_FILE = "data/board.md"
DASHBOARD_DATA_FILE = "docs/data.json"

# Precompute word-boundary regexes once so short keywords like "ui" or
# "node" only match whole words, not substrings inside other words.
_KEYWORD_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b") for k in KEYWORDS]
# EXCLUDE_KEYWORDS already use manual spacing (" sr.", "vp ") as delimiters,
# so plain substring matching here — word-boundary regex breaks on the
# trailing period in " sr." and would let it slip through.
_EXCLUDE_PATTERNS = EXCLUDE_KEYWORDS
_LOCATION_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b") for k in LOCATION_KEYWORDS]


# ---- Helpers ------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def matches_filter(title: str) -> bool:
    # Leading space lets EXCLUDE_KEYWORDS like " sr." match even when that
    # word is the very first thing in the title.
    t = " " + title.lower()
    if not any(p.search(t) for p in _KEYWORD_PATTERNS):
        return False
    if os.environ.get("EXCLUDE_SENIOR", "true").lower() == "true":
        if any(k in t for k in _EXCLUDE_PATTERNS):
            return False
    return True


def matches_location(location: str) -> bool:
    if not location:
        return False  # no location data -> can't confirm India, so skip
    loc = location.lower()
    return any(p.search(loc) for p in _LOCATION_PATTERNS)


def is_fresh(posted_date: str) -> bool:
    """True if posted_date is within MAX_POSTING_AGE_DAYS, or if we have no
    date to check (some ATS responses omit it) — in which case we don't
    penalize the job, we just can't vouch for its age."""
    if MAX_POSTING_AGE_DAYS <= 0 or not posted_date:
        return True
    try:
        posted = datetime.strptime(posted_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_days = (datetime.now(timezone.utc) - posted).days
    return age_days <= MAX_POSTING_AGE_DAYS


_BOARD_LINE_RE = re.compile(r"^- \[(x| )\] .*<!-- id:(\S+) -->\s*$")


def read_checked_ids(path: str) -> set:
    """Parse a previously-generated board.md and return the set of job ids
    that were checked off, so regenerating the board doesn't lose progress."""
    checked = set()
    if not os.path.exists(path):
        return checked
    with open(path) as f:
        for line in f:
            m = _BOARD_LINE_RE.match(line.rstrip("\n"))
            if m and m.group(1) == "x":
                checked.add(m.group(2))
    return checked


def write_board(path: str, jobs: list):
    """Overwrite the board with the full current set of open matching jobs,
    freshest first, as a checklist grouped by company. Checked state from
    the previous version of this file is preserved via the hidden id
    comment at the end of each line."""
    checked = read_checked_ids(path)

    def sort_key(j):
        return (j.get("posted_date") or "0000-00-00", j["company"].lower())

    jobs_sorted = sorted(jobs, key=sort_key, reverse=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Job Application Board\n\n")
        f.write(f"_Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
                f"— {len(jobs_sorted)} open matching roles_\n\n")
        f.write("Tick a box after you apply — your progress is preserved across daily runs.\n\n")
        for j in jobs_sorted:
            box = "x" if j["id"] in checked else " "
            loc = f" · {j['location']}" if j["location"] else ""
            posted = f" · posted {j['posted_date']}" if j.get("posted_date") else ""
            f.write(
                f"- [{box}] **{j['company']}** — [{j['title']}]({j['url']}){loc}{posted} "
                f"<!-- id:{j['id']} -->\n"
            )
    return checked


def write_dashboard_data(path: str, jobs: list, new_ids: set, applied_ids: set):
    """Write docs/data.json — consumed by docs/index.html on GitHub Pages."""
    def sort_key(j):
        return j.get("posted_date") or "0000-00-00"

    jobs_sorted = sorted(jobs, key=sort_key, reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "jobs": [
            {
                "id": j["id"],
                "company": j["company"],
                "title": j["title"],
                "location": j["location"],
                "url": j["url"],
                "posted_date": j.get("posted_date", ""),
                "is_new": j["id"] in new_ids,
                "applied": j["id"] in applied_ids,
            }
            for j in jobs_sorted
        ],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


# ---- ATS fetchers ---------------------------------------------------------

def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    jobs = []
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            jobs.append({
                "id": f"greenhouse-{slug}-{j['id']}",
                "title": j["title"],
                "location": (j.get("location") or {}).get("name", ""),
                "url": j["absolute_url"],
                "posted_date": (j.get("updated_at") or "")[:10],  # YYYY-MM-DD
            })
    except Exception as e:
        print(f"  [error] greenhouse/{slug}: {e}")
    return jobs


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    jobs = []
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        for j in r.json():
            created_ms = j.get("createdAt")
            posted_date = ""
            if created_ms:
                try:
                    posted_date = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    pass
            jobs.append({
                "id": f"lever-{slug}-{j['id']}",
                "title": j["text"],
                "location": (j.get("categories") or {}).get("location", ""),
                "url": j["hostedUrl"],
                "posted_date": posted_date,
            })
    except Exception as e:
        print(f"  [error] lever/{slug}: {e}")
    return jobs


def fetch_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    jobs = []
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        for j in r.json().get("content", []):
            jobs.append({
                "id": f"smartrecruiters-{slug}-{j['id']}",
                "title": j["name"],
                "location": (j.get("location") or {}).get("city", ""),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "posted_date": (j.get("releasedDate") or "")[:10],
            })
    except Exception as e:
        print(f"  [error] smartrecruiters/{slug}: {e}")
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
}


# ---- Notification --------------------------------------------------------

def notify_telegram(new_jobs):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot_token and chat_id):
        return

    def send(text):
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as e:
            print(f"Telegram notify failed: {e}")

    if not new_jobs:
        return

    # Group by company so it's scannable, and chunk messages to stay well
    # under Telegram's 4096-char limit per message.
    by_company = {}
    for j in new_jobs:
        by_company.setdefault(j["company"], []).append(j)

    header = f"🎯 <b>{len(new_jobs)} new matching role(s) today</b>\n"
    chunk = header
    for company, jobs in sorted(by_company.items()):
        block = f"\n<b>{company}</b>\n" + "\n".join(
            f"• <a href=\"{j['url']}\">{j['title']}</a> — {j['location'] or 'location n/a'}"
            for j in jobs
        ) + "\n"
        if len(chunk) + len(block) > 3800:
            send(chunk)
            chunk = block
        else:
            chunk += block
    if chunk.strip():
        send(chunk)


# ---- Main -----------------------------------------------------------------

def main():
    companies = load_json(MATCHED_FILE, [])
    if not companies:
        print(f"No {MATCHED_FILE} found or it's empty — run discover_ats.py first.")
        return

    seen = set(load_json(SEEN_FILE, []))
    all_current_ids = set()
    new_jobs = []
    all_open_matches = []

    for c in companies:
        fetcher = FETCHERS.get(c["ats"])
        if not fetcher:
            continue
        jobs = fetcher(c["slug"])
        for j in jobs:
            all_current_ids.add(j["id"])
            if not (matches_filter(j["title"]) and matches_location(j["location"]) and is_fresh(j.get("posted_date", ""))):
                continue
            full_job = {**j, "company": c["name"]}
            all_open_matches.append(full_job)
            if j["id"] not in seen:
                new_jobs.append(full_job)

    save_json(SEEN_FILE, sorted(all_current_ids))
    applied_ids = write_board(BOARD_FILE, all_open_matches)
    new_ids = {j["id"] for j in new_jobs}
    write_dashboard_data(DASHBOARD_DATA_FILE, all_open_matches, new_ids, applied_ids)
    print(f"Board updated: {len(all_open_matches)} open matching roles -> {BOARD_FILE}")
    print(f"Dashboard data updated -> {DASHBOARD_DATA_FILE}")

    if new_jobs:
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        with open(OUTPUT_FILE, "a") as f:
            f.write(f"\n## {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — {len(new_jobs)} new matching roles\n\n")
            for j in new_jobs:
                loc = f" ({j['location']})" if j["location"] else ""
                posted = f" — posted {j['posted_date']}" if j.get("posted_date") else ""
                f.write(f"- **{j['company']}** — [{j['title']}]({j['url']}){loc}{posted}\n")
        print(f"Found {len(new_jobs)} new matching jobs. Written to {OUTPUT_FILE}")
    else:
        print("No new matching jobs this run.")

    notify_telegram(new_jobs)


if __name__ == "__main__":
    main()
