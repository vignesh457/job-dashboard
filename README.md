# Job Tracker

Automatically tracks new job postings from your target company list on
Greenhouse, Lever, and SmartRecruiters, and keeps a running log of new
matches in `data/new_jobs.md`.

## How it works

1. **`discover_ats.py`** — reads `companies_raw.txt` (your company list) and
   probes each name against the public Greenhouse / Lever / SmartRecruiters
   APIs to figure out which ATS each company uses and its "slug" (the id in
   the job-board URL). Writes `companies_matched.json` (confirmed) and
   `companies_unmatched.txt` (couldn't confirm — usually custom career
   sites, Workday, or SAP SuccessFactors, which don't have simple public
   APIs).

2. **`job_tracker.py`** — reads `companies_matched.json`, pulls current
   postings for each company, filters titles against `KEYWORDS` in the
   script, diffs against `data/seen_jobs.json` (what it saw last run), and
   appends any *new* matching postings to `data/new_jobs.md`. Optionally
   pings you on Telegram.

## First-time setup

```bash
pip install -r requirements.txt
python discover_ats.py
```

This takes a few minutes for ~300 companies (it's making live API calls).
When it's done:

- Open `companies_matched.json` — sanity check a few entries, e.g. click
  through to `https://boards.greenhouse.io/<slug>` or
  `https://jobs.lever.co/<slug>` to confirm it's the right company (common
  false-ish positives: generic slugs like "team" or "careers" occasionally
  match a *different* org's board).
- Open `companies_unmatched.txt` — these companies either use a different
  ATS (Workday, iCIMS, SAP SuccessFactors, Oracle Taleo, or a fully custom
  site), or the slug guesser didn't hit the right variant. For any of these
  you care about, find the real slug manually:
  - Visit their careers page and look at the URL bar — if it's not a custom
    domain, it likely redirects to `boards.greenhouse.io/<slug>`,
    `jobs.lever.co/<slug>`, or `jobs.smartrecruiters.com/<slug>`.
  - Or open browser dev tools → Network tab → reload the careers page →
    search for `greenhouse`, `lever.co`, or `smartrecruiters` in the
    requests.
  - Add confirmed ones to `companies_matched.json` by hand, following the
    existing format.

## Run the tracker

```bash
python job_tracker.py
```

First run will report a lot of "new" jobs (since nothing's been seen yet)
— that's expected, it's establishing the baseline. Every run after that
only reports genuinely new postings.

## Automating with GitHub Actions

1. Push this folder to a GitHub repo. (See the "Deploying the dashboard" section
   below for a note on public vs. private repos.)
2. Go to **Actions → Discover ATS Mapping → Run workflow** once, to
   generate `companies_matched.json` in the repo (or just commit the one
   you generated locally).
3. The **Job Tracker** workflow runs automatically every day at **11:00 AM
   IST** (05:30 UTC) and commits the results back to `data/board.md` and
   `docs/data.json`. You can also trigger it manually from the Actions tab.

## Deploying the dashboard (GitHub Pages)

`docs/index.html` is a self-contained dashboard that reads `docs/data.json`
and renders a searchable, filterable, sortable list of every open matching
role — this is the "nice clear view" instead of a wall of Telegram
messages. It updates automatically every time the daily workflow runs.

**Steps to turn it on:**

1. In your repo on GitHub, go to **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **Deploy from a
   branch**.
3. Under **Branch**, select `main` (or whichever branch you push to) and
   the folder **`/docs`**, then click **Save**.
4. GitHub will give you a URL like
   `https://<your-username>.github.io/<repo-name>/` — that's your
   dashboard. It can take a minute or two to go live the first time.
5. Bookmark that URL on your phone. Every day after the 11 AM run, refresh
   it to see the current board — search by company, filter, sort by
   newest, and click straight through to apply.

**Important — public vs. private repos:** GitHub Pages on a free personal
account only publishes from **public** repositories. A private repo needs
GitHub Pro (or higher) to use Pages. Your company list and job titles
aren't sensitive information on their own, so making the repo public is
usually fine — just don't commit anything else sensitive into it (resumes,
personal notes, etc.). If you'd rather keep it private without paying,
two free alternatives that support private-repo deploys:
- **Netlify** (free tier, connects to a private GitHub repo, auto-deploys
  the `docs/` folder on every push)
- **Vercel** (same idea, also free for personal projects)

Either way, your `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` stay safe as
encrypted GitHub Actions secrets regardless of whether the repo itself is
public — secrets are never exposed in logs or checked into the repo.

**Checking off applications:** `data/board.md` is the source of truth for
"have I applied" — check a box there (via the GitHub app, web UI, or a
local edit + push) and it persists across daily regenerations, and shows
up as a greyed-out ✓ on the dashboard too. The dashboard's own checkbox is
a *device-local* shortcut (saved in your browser's storage) for quickly
marking things while you browse — it won't sync across devices or back
into the repo, so treat `board.md` as the durable record if that matters
to you.

## Optional: Telegram notifications

Now that there's a dashboard, Telegram is optional — useful as a nudge that
something new showed up, without needing to be your primary way of
browsing.

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts →
   copy the bot token it gives you.
2. Message your new bot anything (so it can find your chat), then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   find your `chat.id` in the JSON response.
3. In your GitHub repo: **Settings → Secrets and variables → Actions** →
   add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Next scheduled run will DM you when there's something new.

## Tuning what counts as a "match"

Edit `KEYWORDS` and `EXCLUDE_KEYWORDS` at the top of `job_tracker.py`.
Given you're at ~2 YOE, the default excludes titles containing "staff",
"principal", "director", "manager", "lead", "architect", etc. Set the repo
secret/env var `EXCLUDE_SENIOR=false` to turn that filter off if you want
to see everything.

## Coverage / limitations

- This only covers companies on **Greenhouse, Lever, and SmartRecruiters**.
  A large chunk of your list — Google, Amazon, Microsoft, most large Indian
  IT services firms, and companies on Workday/SAP/Taleo/custom portals —
  won't be picked up by this script. For those, your best bet is:
  - LinkedIn job alerts set up per-company
  - Aggregators like Simplify.jobs or hiring.cafe, which cover a broader
    set of ATS platforms
- Rate limits: these are public APIs but hammering 300 companies daily is
  polite, not abusive — the script makes one request per company per run.
  If you see repeated errors for a specific company, it's usually because
  the slug guess was wrong, not a block.
