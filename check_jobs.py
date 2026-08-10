"""
Free web3/web2 Product Manager job alert bot.

Checks a few free, no-key-required job APIs/feeds for new Product Manager
roles matching your keywords, and pushes a free notification (via ntfy.sh)
for anything new since the last run. Designed to run on a schedule via
GitHub Actions (see .github/workflows/job-alert.yml).

Aggregator sources (web2 + general remote):
  - RemoteOK         (https://remoteok.com/api)                -> JSON, no key
  - Remotive         (https://remotive.com/api/remote-jobs)     -> JSON, no key
  - We Work Remotely (RSS)                                      -> XML, no key
  - Himalayas        (https://himalayas.app/jobs/api/search)    -> JSON, no key
  - Arbeitnow        (https://www.arbeitnow.com/api/job-board-api) -> JSON, no key
  - Jobicy           (https://jobicy.com/api/v2/remote-jobs)    -> JSON, no key

Web3-native sources:
  - CryptoJobsList   (https://api.cryptojobslist.com/jobs.rss)  -> XML, no key
  - DIRECT COMPANY CAREERS PAGES for ~1,000 web3/DeFi companies,
    sourced live each run from a daily-updated public list
    (github.com/tonisives/defi-jobs-list). For every company whose
    careers page runs on Greenhouse, Lever, or Ashby -- all of which
    expose a free public JSON API -- we query that company's own job
    board directly. This is the most complete and lowest-latency
    source: you hear about a new PM role the moment the company posts
    it, not whenever an aggregator re-crawls it.

State:
  - seen_jobs.json in the repo tracks job IDs/links already alerted on,
    so you only get pinged once per new posting. The GitHub Action commits
    this file back after each run.

Notifications:
  - ntfy.sh: free, no signup. Pick any unique topic name (treat it like a
    password -- anyone who knows the exact topic name can see your alerts),
    set it as the NTFY_TOPIC repo secret, and subscribe to that topic in
    the ntfy app (iOS/Android) or at https://ntfy.sh/<your-topic>.
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- Config -----------------------------------------------------------

# Keywords that must appear (case-insensitive) in the job title for it to
# count as a "product manager" role.
TITLE_KEYWORDS = ["product manager", "product lead", "head of product", "product owner"]

# Bonus keywords -- if these appear in title OR description, we tag the
# job as "web3" in the notification (informational only, doesn't filter).
WEB3_KEYWORDS = [
    "web3", "blockchain", "crypto", "defi", "nft", "dao", "token",
    "ethereum", "solana", "smart contract", "on-chain", "onchain",
]

STATE_FILE = "seen_jobs.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # set via repo secret


# ---- Fetchers -----------------------------------------------------------

def fetch_remoteok():
    """RemoteOK public API - returns list of dicts."""
    url = "https://remoteok.com/api"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue  # first element is metadata, not a job
        title = item.get("position", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        jobs.append({
            "source": "RemoteOK",
            "id": f"remoteok-{item['id']}",
            "title": title,
            "company": item.get("company", ""),
            "url": item.get("url", ""),
            "salary": item.get("salary_min") and f"${item['salary_min']}-${item.get('salary_max','?')}" or "",
            "text": (item.get("description") or "")[:2000],
        })
    return jobs


def fetch_remotive():
    """Remotive public API - returns list of dicts."""
    url = "https://remotive.com/api/remote-jobs?search=product%20manager"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        jobs.append({
            "source": "Remotive",
            "id": f"remotive-{item.get('id')}",
            "title": title,
            "company": item.get("company_name", ""),
            "url": item.get("url", ""),
            "salary": item.get("salary", "") or "",
            "text": (item.get("description") or "")[:2000],
        })
    return jobs


def fetch_wwr():
    """We Work Remotely RSS feed for the 'product' category."""
    url = "https://weworkremotely.com/categories/remote-product-jobs.rss"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    jobs = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "")[:2000]
        jobs.append({
            "source": "WeWorkRemotely",
            "id": f"wwr-{link}",
            "title": title,
            "company": "",
            "url": link,
            "salary": "",
            "text": desc,
        })
    return jobs


def fetch_cryptojobslist():
    """CryptoJobsList RSS feed -- web3-native board, all categories/locations.
    We filter to PM titles ourselves since the feed isn't filterable by URL params."""
    url = "https://api.cryptojobslist.com/jobs.rss"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    jobs = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "")[:2000]
        # Titles are often "Job Title at Company"
        company = ""
        if " at " in title:
            company = title.rsplit(" at ", 1)[-1].strip()
        jobs.append({
            "source": "CryptoJobsList",
            "id": f"cjl-{link}",
            "title": title,
            "company": company,
            "url": link,
            "salary": "",
            "text": desc,
        })
    return jobs


def fetch_himalayas():
    """Himalayas public JSON search API."""
    url = "https://himalayas.app/jobs/api/search?keywords=product%20manager&limit=20"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        jobs.append({
            "source": "Himalayas",
            "id": f"himalayas-{item.get('guid') or item.get('applicationLink')}",
            "title": title,
            "company": (item.get("companyName") or ""),
            "url": item.get("applicationLink", ""),
            "salary": item.get("minSalary") and f"${item['minSalary']}-${item.get('maxSalary','?')}" or "",
            "text": (item.get("excerpt") or "")[:2000],
        })
    return jobs


def fetch_arbeitnow():
    """Arbeitnow public JSON API (EU-heavy but includes remote roles)."""
    url = "https://www.arbeitnow.com/api/job-board-api"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("data", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        jobs.append({
            "source": "Arbeitnow",
            "id": f"arbeitnow-{item.get('slug')}",
            "title": title,
            "company": item.get("company_name", ""),
            "url": item.get("url", ""),
            "salary": "",
            "text": (item.get("description") or "")[:2000],
        })
    return jobs


def fetch_jobicy():
    """Jobicy public JSON API."""
    url = "https://jobicy.com/api/v2/remote-jobs?count=50&tag=product%20manager"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("jobTitle", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        salary = ""
        if item.get("annualSalaryMin"):
            salary = f"${item['annualSalaryMin']}-${item.get('annualSalaryMax','?')}"
        jobs.append({
            "source": "Jobicy",
            "id": f"jobicy-{item.get('id')}",
            "title": title,
            "company": item.get("companyName", ""),
            "url": item.get("url", ""),
            "salary": salary,
            "text": (item.get("jobExcerpt") or "")[:2000],
        })
    return jobs


# ---- Web3 company careers pages (Greenhouse / Lever / Ashby) -----------

DEFI_LIST_URL = "https://raw.githubusercontent.com/tonisives/defi-jobs-list/main/README.md"

# ATS types we can query directly via a free public JSON API.
# (bamboohr/recruitee/workable/workday/getro/deel/custom are skipped --
# they don't expose a simple no-key JSON endpoint the same way.)
SUPPORTED_ATS = {"greenhouse", "lever", "ashby"}

TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")


def load_web3_company_boards():
    """Download the daily-updated defi-jobs-list README and parse out
    (company_name, ats, slug) for every company on a supported ATS."""
    req = urllib.request.Request(DEFI_LIST_URL, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")

    companies = []
    for line in text.splitlines():
        line = line.strip()
        m = TABLE_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) < 3:
            continue
        name_cell, ats_cell, url_cell = cells[0], cells[1], cells[2]
        ats = ats_cell.lower().strip()
        if ats not in SUPPORTED_ATS:
            continue
        name_match = re.match(r"\[([^\]]+)\]", name_cell)
        company_name = name_match.group(1) if name_match else name_cell
        if not company_name or company_name.lower() == "company":
            continue
        url_match = re.search(r"https?://[^\s\)>]+", url_cell)
        if not url_match:
            continue
        board_url = url_match.group(0)

        slug = None
        if ats == "greenhouse":
            sm = re.search(r"greenhouse\.io/([^/?#]+)", board_url)
            slug = sm.group(1) if sm else None
        elif ats == "lever":
            sm = re.search(r"lever\.co/([^/?#]+)", board_url)
            slug = sm.group(1) if sm else None
        elif ats == "ashby":
            sm = re.search(r"ashbyhq\.com/([^/?#]+)", board_url)
            slug = sm.group(1) if sm else None

        if slug:
            companies.append({"name": company_name, "ats": ats, "slug": slug})

    seen = set()
    unique = []
    for c in companies:
        key = (c["ats"], c["slug"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def fetch_greenhouse_company(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs?content=false"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        location = (item.get("location") or {}).get("name", "")
        jobs.append({
            "source": f"{company['name']} (Greenhouse)",
            "id": f"gh-{company['slug']}-{item.get('id')}",
            "title": f"{title} ({location})" if location else title,
            "company": company["name"],
            "url": item.get("absolute_url", ""),
            "salary": "",
            "text": "",
        })
    return jobs


def fetch_lever_company(company):
    url = f"https://api.lever.co/v0/postings/{company['slug']}?mode=json"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data:
        title = item.get("text", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        location = (item.get("categories") or {}).get("location", "")
        jobs.append({
            "source": f"{company['name']} (Lever)",
            "id": f"lever-{company['slug']}-{item.get('id')}",
            "title": f"{title} ({location})" if location else title,
            "company": company["name"],
            "url": item.get("hostedUrl", ""),
            "salary": "",
            "text": "",
        })
    return jobs


def fetch_ashby_company(company):
    # includeCompensation=true asks Ashby to return salary bands where the
    # employer has made them public -- useful since we care about a $150k+ floor.
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}?includeCompensation=true"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        location = item.get("location", "")
        comp = item.get("compensation", {}) or {}
        salary = comp.get("compensationTierSummary", "") or ""
        jobs.append({
            "source": f"{company['name']} (Ashby)",
            "id": f"ashby-{company['slug']}-{item.get('id')}",
            "title": f"{title} ({location})" if location else title,
            "company": company["name"],
            "url": item.get("jobUrl", ""),
            "salary": salary,
            "text": "",
        })
    return jobs


def fetch_all_web3_companies(max_workers=25):
    """Fan out to every supported-ATS company board in parallel."""
    try:
        companies = load_web3_company_boards()
    except Exception as e:
        print(f"Failed to load web3 company list: {e}", file=sys.stderr)
        return []

    fetch_fn = {
        "greenhouse": fetch_greenhouse_company,
        "lever": fetch_lever_company,
        "ashby": fetch_ashby_company,
    }

    jobs = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_fn[c["ats"]], c): c for c in companies}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                jobs.extend(fut.result())
            except Exception as e:
                print(f"  (skipped {c['name']} / {c['ats']}: {e})", file=sys.stderr)
    return jobs


# ---- Helpers -----------------------------------------------------------

def tag_web3(job):
    blob = f"{job['title']} {job.get('company','')} {job.get('text','')}".lower()
    return any(k in blob for k in WEB3_KEYWORDS)


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def notify(job):
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC set] Would notify: {job['title']} @ {job['company']} -> {job['url']}")
        return
    web3_tag = "🌐 web3" if tag_web3(job) else "web2"
    title = f"New PM role: {job['title']}"
    message = f"{job['company']} ({job['source']}, {web3_tag})\n{job['salary']}\n{job['url']}"
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Click": job["url"],
            "Priority": "default",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Failed to notify for {job['url']}: {e}", file=sys.stderr)


# ---- Main -----------------------------------------------------------

def main():
    all_jobs = []

    # Aggregator boards (fast, single request each)
    for fetcher in (
        fetch_remoteok, fetch_remotive, fetch_wwr, fetch_cryptojobslist,
        fetch_himalayas, fetch_arbeitnow, fetch_jobicy,
    ):
        try:
            jobs = fetcher()
            print(f"{fetcher.__name__}: {len(jobs)} matches")
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"Fetcher {fetcher.__name__} failed: {e}", file=sys.stderr)

    # Direct web3 company careers pages (~1,000 companies, parallelized)
    try:
        company_jobs = fetch_all_web3_companies()
        print(f"web3 company boards: {len(company_jobs)} matches")
        all_jobs.extend(company_jobs)
    except Exception as e:
        print(f"Company board sweep failed: {e}", file=sys.stderr)

    seen = load_seen()
    new_jobs = [j for j in all_jobs if j["id"] not in seen]

    print(f"Checked {len(all_jobs)} matching listings, {len(new_jobs)} new.")

    for job in new_jobs:
        notify(job)
        seen.add(job["id"])

    save_seen(seen)


if __name__ == "__main__":
    main()
