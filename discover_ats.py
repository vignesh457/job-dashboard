"""
discover_ats.py

Reads companies_raw.txt (one company name per line) and tries to figure out
whether each company posts jobs on Greenhouse, Lever, or SmartRecruiters —
by actually calling their public job-board APIs with guessed slugs.

Output:
  companies_matched.json    -> confirmed {name, ats, slug, job_count}
  companies_unmatched.txt   -> companies we couldn't confirm (check manually)

Run:
  python discover_ats.py
"""

import difflib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

INPUT_FILE = "companies_raw.txt"
MATCHED_FILE = "companies_matched.json"
UNMATCHED_FILE = "companies_unmatched.txt"

# Legal-entity / filler words to strip when generating slug guesses
SUFFIX_PATTERN = re.compile(
    r"\b(inc|ltd|llp|llc|corp|corporation|technologies|technology|pvt|private|"
    r"limited|co|company|group|solutions|systems|holdings|worldwide|global|india)\b",
    re.IGNORECASE,
)
PAREN_PATTERN = re.compile(r"\(.*?\)")

MAX_WORKERS = 12
REQUEST_TIMEOUT = 8
MIN_SLUG_LEN = 4          # skip degenerate guesses like "d", "the", "sp", "bank"
NAME_MATCH_THRESHOLD = 0.5


def slug_candidates(name: str):
    """Generate a handful of plausible ATS slugs for a company name."""
    base = PAREN_PATTERN.sub("", name).strip()
    cleaned = SUFFIX_PATTERN.sub("", base).strip()

    candidates = set()
    for text in {base, cleaned}:
        if not text:
            continue
        no_space = re.sub(r"[^a-zA-Z0-9]+", "", text).lower()
        hyphenated = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
        first_word = re.sub(r"[^a-zA-Z0-9]+", "", text.split()[0]).lower() if text.split() else ""
        candidates.update([no_space, hyphenated, first_word])

    candidates = {c for c in candidates if len(c) >= MIN_SLUG_LEN}
    return candidates


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity between two company names, robust to legal suffixes/spacing."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ---- ATS checkers -----------------------------------------------------
# IMPORTANT: several ATS "job list" endpoints return HTTP 200 with an empty
# result for almost ANY identifier instead of a 404 (SmartRecruiters does
# this reliably). So "got a 200" is not proof the company exists. We treat
# job_count == 0 as inconclusive/reject, and where the API exposes a company
# name field, we cross-check it against the input name to avoid a generic
# slug silently attaching to the wrong org.

def check_greenhouse(slug: str, company_name: str):
    try:
        meta = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=REQUEST_TIMEOUT)
        if meta.status_code != 200:
            return None
        board_name = meta.json().get("name", "")
        if board_name and name_similarity(board_name, company_name) < NAME_MATCH_THRESHOLD:
            return None

        jobs = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=REQUEST_TIMEOUT)
        if jobs.status_code != 200:
            return None
        count = len(jobs.json().get("jobs", []))
        if count == 0:
            return None
        return {"ats": "greenhouse", "slug": slug, "job_count": count, "matched_name": board_name}
    except Exception:
        return None


def check_lever(slug: str, company_name: str):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list):
            return None
        count = len(data)
        if count == 0:
            return None

        # Lever's postings endpoint has no company-name field, so do a
        # lightweight cross-check against the hosted careers page instead.
        verified = False
        try:
            page = requests.get(f"https://jobs.lever.co/{slug}", timeout=REQUEST_TIMEOUT)
            first_token = re.split(r"[^a-zA-Z0-9]+", company_name)[0].lower()
            if page.status_code == 200 and len(first_token) >= 3 and first_token in page.text.lower():
                verified = True
        except Exception:
            pass

        return {"ats": "lever", "slug": slug, "job_count": count, "verified": verified}
    except Exception:
        return None


def check_smartrecruiters(slug: str, company_name: str):
    try:
        postings = requests.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", timeout=REQUEST_TIMEOUT
        )
        if postings.status_code != 200:
            return None
        content = postings.json().get("content", [])
        count = len(content)
        if count == 0:
            return None  # this is what was silently matching everything before

        matched_name = ""
        try:
            profile = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}", timeout=REQUEST_TIMEOUT)
            if profile.status_code == 200:
                matched_name = profile.json().get("name", "")
                if matched_name and name_similarity(matched_name, company_name) < NAME_MATCH_THRESHOLD:
                    return None
        except Exception:
            pass

        return {"ats": "smartrecruiters", "slug": slug, "job_count": count, "matched_name": matched_name}
    except Exception:
        return None


CHECKERS = [
    ("greenhouse", check_greenhouse),
    ("lever", check_lever),
    ("smartrecruiters", check_smartrecruiters),
]


def probe_company(name: str):
    slugs = slug_candidates(name)
    # Try every slug variant on Greenhouse first, then Lever, then
    # SmartRecruiters — rather than cycling ATS-per-slug — since
    # SmartRecruiters is the noisiest signal and should only be reached
    # for companies genuinely not found on the other two.
    for ats_name, check_fn in CHECKERS:
        for slug in slugs:
            result = check_fn(slug, name)
            if result:
                return {"name": name, **result}
    return None


def main():
    with open(INPUT_FILE, encoding="utf-8", errors="replace") as f:
        companies = [line.strip() for line in f if line.strip()]

    matched = []
    unmatched = []

    print(f"Probing {len(companies)} companies across Greenhouse / Lever / SmartRecruiters...")
    print("(This calls each ATS's public API — it does not scrape or log in anywhere.)\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(probe_company, c): c for c in companies}
        for i, future in enumerate(as_completed(futures), 1):
            name = futures[future]
            result = future.result()
            if result:
                matched.append(result)
                extra = result.get("matched_name") or ("verified" if result.get("verified") else "")
                tag = f" [{extra}]" if extra else ""
                print(f"[{i}/{len(companies)}] MATCH  {name:35s} -> {result['ats']}/{result['slug']} ({result['job_count']} jobs){tag}")
            else:
                unmatched.append(name)
                print(f"[{i}/{len(companies)}] miss   {name}")

    matched.sort(key=lambda x: x["name"].lower())
    unmatched.sort(key=str.lower)

    with open(MATCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(matched, f, indent=2)
    with open(UNMATCHED_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(unmatched) + "\n")

    print(f"\nDone. Matched {len(matched)}/{len(companies)} companies.")
    print(f"  -> {MATCHED_FILE} (confirmed, ready for job_tracker.py)")
    print(f"  -> {UNMATCHED_FILE} (check these manually — custom career sites,")
    print("     Workday/Taleo instances, or a slug variant we didn't guess)")


if __name__ == "__main__":
    main()
