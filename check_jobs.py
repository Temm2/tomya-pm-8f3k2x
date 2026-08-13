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
    board directly.

Early-stage / YC-adjacent startups:
  - Hacker News "Who is Hiring?" (via the free Algolia HN API) -- the
    monthly thread where seed-to-Series-B startups, many YC-backed,
    post roles directly. This is the main lever for surfacing early-
    stage companies specifically, since the ~1,000-company sweep above
    is web3-focused and general job boards skew toward larger, more
    established employers.

Filters applied after fetching:
  - Remote-global only: keeps a role only if some field (location,
    title, or description) signals it's open to anyone, anywhere.
    Drops roles that are region-locked (e.g. "US only", "must be based
    in the UK"), hybrid, or onsite. Ambiguous/no-signal roles are
    dropped too, since the goal is a clean feed, not a maximal one.
  - $150k+ floor: keeps a role if its disclosed salary range reaches
    $150,000 or higher, OR if no salary is disclosed at all (most
    postings, especially outside the US, don't publish a number --
    dropping those would hide most of the market). Only drops a role
    when a number IS published and it's unambiguously below $150k.
    Every notification is tagged "verified $150k+" or "salary not
    listed" so you know which case you're looking at without opening
    the posting.

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

import html
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
TITLE_KEYWORDS = [
    "product manager", "product lead", "head of product", "product owner",
    "founding product manager", "founding pm", "first product manager",
    "first pm", "1st product hire", "founding product hire",
    "director of product", "vp of product", "vice president of product",
    "svp of product", "senior vice president of product",
    "chief product officer",
]

# Bonus keywords -- if these appear in title OR description, we tag the
# job as "web3" in the notification (informational only, doesn't filter).
WEB3_KEYWORDS = [
    "web3", "blockchain", "crypto", "defi", "nft", "dao", "token",
    "ethereum", "solana", "smart contract", "on-chain", "onchain",
]

# Minimum acceptable salary. Applied only when a number is actually
# disclosed (see module docstring for the reasoning).
SALARY_FLOOR = 150000

# Signals that a role is open globally / remote-first. Checked against
# location field + title + description, all lowercased.
REMOTE_INCLUDE_PATTERNS = [
    "remote", "anywhere", "worldwide", "world wide", "global", "globally",
    "distributed team", "distributed remote", "remote-first", "remote first",
    "work from home", "wfh", "fully remote", "100% remote", "remote friendly",
    "remote (global)", "remote - global", "remote global", "any location",
    "location independent", "location-independent", "international remote",
    "remote (worldwide)", "remote - worldwide",
]

# Signals that a role is region-locked, hybrid, or onsite -- these override
# a REMOTE_INCLUDE match (e.g. "Remote (US only)" contains "remote" but is
# not globally remote). Timezone-only requirements are deliberately NOT
# here -- you're open to German or US timezones (and others), so a role
# requiring e.g. EST overlap isn't a disqualifier on its own.
REMOTE_EXCLUDE_PATTERNS = [
    "us only", "u.s. only", "usa only", "united states only",
    "us-based only", "us based only", "us citizens only",
    "us applicants only", "us candidates only", "us residents only",
    "must be a us resident", "must be a us citizen",
    "uk only", "united kingdom only", "eu only", "european union only",
    "emea only", "apac only", "latam only", "canada only",
    "canada-based", "canada based",
    "must be based in", "must reside in", "must be located in",
    "must be authorized to work in", "authorized to work in the us",
    "authorized to work in the united states",
    "visa sponsorship is not available", "no visa sponsorship",
    "onsite", "on-site", "in-office", "in office", "hybrid",
    "residing in the us", "based in the united states",
    "located in the united states", "based in the uk", "based in europe",
    "remote (us)", "remote - us", "remote, us", "remote(us)",
    "remote (uk)", "remote - uk", "remote (eu)", "remote - eu",
    "remote (canada)", "us remote", "uk remote", "eu remote",
    "remote - north america", "remote (north america)",
]

# Strongest signal of all: explicit language saying the company/role welcomes
# applicants from anywhere, even when the posting is otherwise US-anchored
# (e.g. a US company that explicitly says "open to global applicants"). This
# tier is checked BEFORE the exclude patterns above, so it correctly rescues
# postings that would otherwise get dropped for mentioning "US-based company"
# or similar -- the explicit global-acceptance language wins.
GLOBAL_OVERRIDE_PATTERNS = [
    "open to global applicants", "global applicants welcome",
    "accepting applicants from anywhere", "applicants from anywhere",
    "accepting international applicants", "international applicants welcome",
    "candidates from any country", "candidates from anywhere",
    "regardless of location", "regardless of where you live",
    "we hire globally", "hiring globally", "hire talent globally",
    "open to candidates worldwide", "open to applicants worldwide",
    "remote from anywhere in the world", "work from anywhere in the world",
    "globally distributed", "location agnostic", "no location restrictions",
    "welcome applicants from around the world",
    "open to remote candidates worldwide",
]

STATE_FILE = "seen_jobs.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # set via repo secret


# ---- Fetchers -----------------------------------------------------------
# Every job dict carries: source, id, title, company, location, url,
# salary, text. `location` and `salary` may be empty strings when the
# source doesn't expose structured data for them.

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
            "location": item.get("location", "") or "",
            "url": item.get("url", ""),
            "salary": item.get("salary_min") and f"${item['salary_min']}-${item.get('salary_max','?')}" or "",
            "text": (item.get("description") or "")[:2000],
            "remote_native": True,  # RemoteOK only lists remote roles
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
            "location": item.get("candidate_required_location", "") or "",
            "url": item.get("url", ""),
            "salary": item.get("salary", "") or "",
            "text": (item.get("description") or "")[:2000],
            "remote_native": True,  # Remotive only lists remote roles
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
        # WWR region often shows up in a <region> tag on their feed
        region = (item.findtext("region") or "").strip()
        jobs.append({
            "source": "WeWorkRemotely",
            "id": f"wwr-{link}",
            "title": title,
            "company": "",
            "location": region,
            "url": link,
            "salary": "",
            "text": desc,
            "remote_native": True,  # WWR only lists remote roles
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
            "location": "",
            "url": link,
            "salary": "",
            "text": desc,
        })
    return jobs


def fetch_himalayas():
    """Himalayas public JSON search API."""
    url = "https://himalayas.app/jobs/api/search?keywords=product%20manager&limit=50"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        loc_parts = item.get("locationRestrictions") or []
        location = ", ".join(loc_parts) if loc_parts else ""
        jobs.append({
            "source": "Himalayas",
            "id": f"himalayas-{item.get('guid') or item.get('applicationLink')}",
            "title": title,
            "company": (item.get("companyName") or ""),
            "location": location,
            "url": item.get("applicationLink", ""),
            "salary": item.get("minSalary") and f"${item['minSalary']}-${item.get('maxSalary','?')}" or "",
            "text": (item.get("excerpt") or "")[:2000],
            "remote_native": True,  # Himalayas only lists remote roles
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
        location = item.get("location", "") or ""
        if item.get("remote"):
            location = (location + " Remote").strip()
        jobs.append({
            "source": "Arbeitnow",
            "id": f"arbeitnow-{item.get('slug')}",
            "title": title,
            "company": item.get("company_name", ""),
            "location": location,
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
            "location": item.get("jobGeo", "") or "",
            "url": item.get("url", ""),
            "salary": salary,
            "text": (item.get("jobExcerpt") or "")[:2000],
            "remote_native": True,  # Jobicy only lists remote roles
        })
    return jobs


def strip_html(html_text):
    """Minimal HTML-to-text for keyword matching (not for display)."""
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---- Hacker News "Who is Hiring?" (early-stage / YC-adjacent startups) --
# Free official Algolia API, no key, 10,000 req/hour limit. The monthly
# thread is dominated by seed-to-Series-B startups (many YC-backed) posting
# directly -- exactly the "early stage" gap the other sources don't cover.
# It only refreshes ~once a month (a new thread each month), so most runs
# will correctly find zero new postings here.

def fetch_hn_whoishiring():
    search_url = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=5"
    req = urllib.request.Request(search_url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        search_data = json.loads(resp.read().decode("utf-8"))

    story_id = None
    for hit in search_data.get("hits", []):
        title = (hit.get("title") or "").lower()
        if "who is hiring" in title:
            story_id = hit.get("objectID")
            break
    if not story_id:
        return []

    items_url = f"https://hn.algolia.com/api/v1/items/{story_id}"
    req2 = urllib.request.Request(items_url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req2, timeout=20) as resp:
        story_data = json.loads(resp.read().decode("utf-8"))

    jobs = []
    for child in story_data.get("children", []) or []:
        text = strip_html(child.get("text") or "")
        if not text or not any(k in text.lower() for k in TITLE_KEYWORDS):
            continue
        cid = child.get("id")
        # HN postings are freeform text, not structured title/company fields.
        # Most start "CompanyName | role | location | ..." -- best-effort
        # extraction of a company name for the notification; falls back to
        # a generic label since the raw text (in the notification body) has
        # the real details either way.
        company = text.split("|")[0].strip()[:60] if "|" in text[:120] else "See posting"
        jobs.append({
            "source": "HN Who Is Hiring",
            "id": f"hn-{cid}",
            "title": f"Product Manager role mentioned — {company}",
            "company": company,
            "location": "",
            "url": f"https://news.ycombinator.com/item?id={cid}",
            "salary": "",
            "text": text[:3000],
        })
    return jobs


# ---- Telegram web3 job channels -----------------------------------------
# Public Telegram channels expose a lightweight, unauthenticated preview
# page at t.me/s/{channel} -- real, well-established technique (Telegram's
# "widget" HTML, class names like tgme_widget_message have been stable for
# years), no login or API key needed. This is different from the Telegram
# Bot API, which can't passively read a channel's message history.
#
# Twitter/X has no equivalent free option -- its free API tier doesn't
# support search at all (only reading/posting your own bot's tweets), so
# there's no legitimate free way to monitor it and it's not included here.
#
# Caveat: unlike the other sources, these are unstructured chat messages,
# not structured job postings -- title/company are best-effort text
# extraction, and some messages render as "open in Telegram to view" in
# the static preview (can't be read without the app). This is honestly the
# least reliable source in the bot, included because it's the only way to
# reach this channel of postings at all, wrapped in error handling so a
# parsing miss just yields fewer results rather than breaking the run.

TELEGRAM_CHANNELS = ["remoteweb3jobs", "web3hiring", "laborx"]

TELEGRAM_MESSAGE_RE = re.compile(
    r'data-post="[^/"]+/(\d+)".*?class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)


def fetch_telegram_channel(channel):
    url = f"https://t.me/s/{channel}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (job-alert-bot)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw_html = resp.read().decode("utf-8", errors="ignore")

    jobs = []
    for msg_id, block in TELEGRAM_MESSAGE_RE.findall(raw_html):
        text = strip_html(block)
        if not text or not any(k in text.lower() for k in TITLE_KEYWORDS):
            continue
        # Best-effort company guess from "at CompanyName" phrasing, common
        # in these channels' posting format (e.g. "Product Manager at Acme").
        company = ""
        cm = re.search(r"\bat\s+([A-Z][\w&.\-\s]{1,40})", text)
        if cm:
            company = cm.group(1).strip()
        jobs.append({
            "source": f"Telegram @{channel}",
            "id": f"tg-{channel}-{msg_id}",
            "title": f"Product Manager role mentioned — {company or 'see posting'}",
            "company": company,
            "location": "",
            "url": f"https://t.me/{channel}/{msg_id}",
            "salary": "",
            "text": text[:3000],
        })
    return jobs


def fetch_all_telegram_channels():
    jobs = []
    for ch in TELEGRAM_CHANNELS:
        try:
            ch_jobs = fetch_telegram_channel(ch)
            jobs.extend(ch_jobs)
        except Exception as e:
            print(f"  (skipped Telegram channel @{ch}: {e})", file=sys.stderr)
    return jobs


# ---- Web3 VC/ecosystem job boards (Getro-powered) -----------------------
# Two flavors of Getro network, both worth having:
#  - VC portfolio boards (Dragonfly, Multicoin, Coinbase Ventures): all
#    postings from that fund's portfolio companies, which skew early-stage.
#  - Ecosystem boards (Solana, BNB Chain): run by the chain foundation,
#    aggregating postings from every project building on that chain --
#    Solana's alone spans 170+ companies (Phantom, Orca, Wormhole, etc.).
# Both types are dominated by smaller/earlier-stage teams that don't have
# their own polished direct ATS, which is exactly the gap the ~1,000-
# company defi-jobs-list sweep (dominated by larger, established protocols
# with their own Greenhouse/Lever/Ashby boards) doesn't fill.
# their own polished direct ATS, which is exactly the gap the ~1,000-
# company defi-jobs-list sweep (dominated by larger, established protocols
# with their own Greenhouse/Lever/Ashby boards) doesn't fill.
#
# Getro doesn't expose a documented public JSON API, but job URLs follow a
# stable, predictable pattern: /companies/{company-slug}/jobs/{id}-{title-slug}
# -- present in the raw HTML regardless of internal markup, since it's a
# real link href. We use that to (1) cheaply prefilter for PM-shaped slugs
# on the listing page, then (2) fetch only the matching job's own page for
# the real title/location/salary/description text.
#
# Caveat: Getro listing pages are paginated via client-side "Load more"
# calls we can't reach, so this only sees the first page (~20-25 postings,
# newest first) per network per run -- fine for an alert bot, since
# catching new postings is the goal, not exhaustive historical coverage.

GETRO_NETWORKS = [
    {"name": "Dragonfly Portfolio", "base": "https://jobs.dragonfly.xyz"},
    {"name": "Multicoin Capital Portfolio", "base": "https://jobs.multicoin.capital"},
    {"name": "Coinbase Ventures Portfolio", "base": "https://coinbase.getro.com"},
    {"name": "Electric Capital Portfolio", "base": "https://jobs.electriccapital.com"},
    {"name": "Blockchain Capital Portfolio", "base": "https://jobs.blockchaincapital.com"},
    {"name": "CoinFund Portfolio", "base": "https://jobs.coinfund.io"},
    {"name": "Framework Ventures Portfolio", "base": "https://jobs.framework.ventures"},
    {"name": "a16z Portfolio", "base": "https://portfoliojobs.a16z.com"},
    {"name": "Solana Ecosystem", "base": "https://jobs.solana.com"},
    {"name": "BNB Chain Ecosystem", "base": "https://jobs.bnbchain.org"},
    {"name": "Arbitrum Ecosystem", "base": "https://jobs.arbitrum.io"},
    {"name": "Avalanche Ecosystem", "base": "https://jobs.avax.network"},
    {"name": "Optimism Ecosystem", "base": "https://jobs.optimism.io"},
    {"name": "Blockchain Association", "base": "https://jobs.theblockchainassociation.org"},
]

GETRO_JOB_URL_RE = re.compile(r"/companies/([a-z0-9\-]+)/jobs/(\d+)-([a-z0-9\-]+)")


def fetch_getro_network(network):
    base = network["base"]
    url = f"{base}/jobs"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw_html = resp.read().decode("utf-8", errors="ignore")

    seen_ids = set()
    jobs = []
    for company_slug, job_id, title_slug in GETRO_JOB_URL_RE.findall(raw_html):
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        title_guess = title_slug.replace("-", " ")
        if not any(k in title_guess.lower() for k in TITLE_KEYWORDS):
            continue  # cheap prefilter on the URL slug before fetching the full page

        job_url = f"{base}/companies/{company_slug}/jobs/{job_id}-{title_slug}"
        text = title_guess
        try:
            jreq = urllib.request.Request(job_url, headers={"User-Agent": "job-alert-bot"})
            with urllib.request.urlopen(jreq, timeout=15) as jresp:
                job_html = jresp.read().decode("utf-8", errors="ignore")
            text = strip_html(job_html)[:4000]
        except Exception:
            pass  # fall back to the slug-derived text if the detail page fetch fails

        jobs.append({
            "source": f"{network['name']} (Getro)",
            "id": f"getro-{network['name']}-{job_id}",
            "title": title_guess.title(),
            "company": company_slug.replace("-", " ").title(),
            "location": "",
            "url": job_url,
            "salary": "",
            "text": text,
        })
    return jobs


def fetch_all_getro_networks(max_workers=14):
    jobs = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_getro_network, n): n for n in GETRO_NETWORKS}
        for fut in as_completed(futures):
            n = futures[fut]
            try:
                jobs.extend(fut.result())
            except Exception as e:
                print(f"  (skipped Getro network {n['name']}: {e})", file=sys.stderr)
    return jobs


# ---- Web3 company careers pages (Greenhouse / Lever / Ashby / Recruitee / Workable) ---

DEFI_LIST_URL = "https://raw.githubusercontent.com/tonisives/defi-jobs-list/main/README.md"

# ATS types we can query directly via a free public JSON API.
# (bamboohr, workday, deel, getro-via-this-list, and custom are skipped --
# bamboohr in particular has no genuine public jobs API, only an
# undocumented internal widget endpoint that changes without notice, so it
# doesn't meet the "stable free API" bar the others do.)
SUPPORTED_ATS = {"greenhouse", "lever", "ashby", "recruitee", "workable", "smartrecruiters", "personio"}

# Manually verified companies, added directly rather than relying on the
# auto-pulled defi-jobs-list -- guaranteed accurate since each slug was
# confirmed against the company's real, current careers page. Useful both
# as a safety net (covered even if the daily list ever drops or renames
# one of these) and for explicitly prioritizing ecosystems you work in
# directly. Add more here any time by giving the company name, its ATS
# (greenhouse/lever/ashby/recruitee/workable), and the slug from its
# careers URL.
EXTRA_COMPANIES = [
    {"name": "Zora", "ats": "greenhouse", "slug": "zora"},                 # Base
    {"name": "Phantom", "ats": "ashby", "slug": "phantom"},                # Solana
    {"name": "Magic Eden", "ats": "ashby", "slug": "magiceden"},           # Solana
    {"name": "Jito Labs", "ats": "lever", "slug": "jito.wtf"},             # Solana
    # SmartRecruiters -- confirmed real public customers, slugs verified
    # against their actual careers.smartrecruiters.com pages.
    {"name": "Visa", "ats": "smartrecruiters", "slug": "Visa"},
    {"name": "Equinox", "ats": "smartrecruiters", "slug": "Equinox"},
    # Personio -- infrastructure ready, but no company slugs could be
    # verified during development (no bulk Personio company list found,
    # and general search didn't surface a confirmed real subdomain).
    # Add specific companies here once you know a real one, e.g.:
    # {"name": "SomeCompany", "ats": "personio", "slug": "somecompany"},
]

# ---- Silicon Valley / major tech companies (priority) -------------------
# A community-maintained list of ~240 major tech companies (mostly Bay
# Area HQ'd -- Airbnb, Stripe, Figma, Notion, Coinbase, OpenAI, Anthropic,
# Databricks, and more, plus several already-web3 names like Alchemy,
# Coinbase, Ripple, Fireblocks, Figment) mapped directly to their real
# Greenhouse/Ashby/Lever API endpoints. Pulled live each run so it stays
# current as the community adds companies.
#
# Jobs sourced from here are flagged priority=True (see notify()), since
# these are the specific large, well-known companies worth surfacing more
# insistently than the broader long-tail sweep.
CLAUDE_JOBS_SKILL_URL = "https://raw.githubusercontent.com/jshchnz/claude-jobs/main/SKILL.md"
CLAUDE_JOBS_ROW_RE = re.compile(r"^\|\s*([a-z0-9_\-]+)\s*\|\s*(https?://\S+)\s*\|$", re.MULTILINE)


def load_priority_companies():
    """Download the live claude-jobs company table and parse out
    (name, ats, slug) for every entry on a supported ATS."""
    req = urllib.request.Request(CLAUDE_JOBS_SKILL_URL, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")

    companies = []
    for name, endpoint in CLAUDE_JOBS_ROW_RE.findall(text):
        ats = slug = None
        if "boards-api.greenhouse.io" in endpoint:
            m = re.search(r"boards-api\.greenhouse\.io/v1/boards/([^/]+)/jobs", endpoint)
            ats, slug = "greenhouse", (m.group(1) if m else None)
        elif "api.ashbyhq.com/posting-api/job-board" in endpoint:
            m = re.search(r"job-board/([^/?]+)", endpoint)
            ats, slug = "ashby", (m.group(1) if m else None)
        elif "jobs.ashbyhq.com" in endpoint:
            m = re.search(r"jobs\.ashbyhq\.com/([^/?]+)", endpoint)
            ats, slug = "ashby", (m.group(1) if m else None)
        elif "api.lever.co/v0/postings" in endpoint:
            m = re.search(r"postings/([^/?]+)", endpoint)
            ats, slug = "lever", (m.group(1) if m else None)
        else:
            continue  # one-off endpoints (e.g. sentry.io/jobs/list.json) -- skip
        if slug:
            companies.append({"name": name, "ats": ats, "slug": slug, "priority": True})
    return companies


TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")


# ---- General startup universe (all sizes, not just crypto/major tech) ---
# A community-maintained dataset indexing ~16,000 companies across
# Greenhouse/Lever/Ashby, built from Common Crawl -- this is the genuinely
# broad "startups of all sizes" net: seed-stage to large, any industry,
# not curated or hand-picked like the other lists. Much bigger than
# everything else in this bot combined, so it meaningfully increases
# coverage, at the real cost of a much longer run (~15k extra HTTP calls).
# Company display names are just the URL slug (this dataset doesn't carry
# proper names), lightly cleaned up for readability.
GENERAL_STARTUPS_URLS = {
    "greenhouse": "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/greenhouse_companies.json",
    "lever": "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/lever_companies.json",
    "ashby": "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/ashby_companies.json",
}


def load_general_startup_companies():
    companies = []
    for ats, url in GENERAL_STARTUPS_URLS.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                slugs = json.loads(resp.read().decode("utf-8"))
            for slug in slugs:
                display_name = re.sub(r"-\d+$", "", slug).replace("-", " ").title()
                companies.append({"name": display_name, "ats": ats, "slug": slug})
        except Exception as e:
            print(f"Failed to load general startup list for {ats}: {e}", file=sys.stderr)
    return companies


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
        elif ats == "recruitee":
            sm = re.search(r"https?://([a-z0-9\-]+)\.recruitee\.com", board_url)
            slug = sm.group(1) if sm else None
        elif ats == "workable":
            sm = re.search(r"workable\.com/([a-z0-9\-]+)", board_url)
            slug = sm.group(1) if sm else None
        elif ats == "smartrecruiters":
            sm = re.search(r"smartrecruiters\.com/([^/?#]+)", board_url)
            slug = sm.group(1) if sm else None
        elif ats == "personio":
            sm = re.search(r"https?://([a-z0-9\-]+)\.jobs\.personio\.(?:de|com)", board_url)
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
    # content=true pulls the full job description -- needed so
    # GLOBAL_OVERRIDE_PATTERNS (e.g. "open to applicants worldwide") can be
    # detected even on postings that are otherwise US-anchored.
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs?content=true"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        location = (item.get("location") or {}).get("name", "") or ""
        text = strip_html(item.get("content", ""))[:3000]
        jobs.append({
            "source": f"{company['name']} (Greenhouse)",
            "id": f"gh-{company['slug']}-{item.get('id')}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": item.get("absolute_url", ""),
            "salary": "",
            "text": text,
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
        location = (item.get("categories") or {}).get("location", "") or ""
        raw_desc = item.get("descriptionPlain") or strip_html(item.get("description", ""))
        text = (raw_desc or "")[:3000]
        jobs.append({
            "source": f"{company['name']} (Lever)",
            "id": f"lever-{company['slug']}-{item.get('id')}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": item.get("hostedUrl", ""),
            "salary": "",
            "text": text,
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
        location = item.get("location", "") or ""
        if item.get("isRemote"):
            location = (location + " Remote").strip()
        comp = item.get("compensation", {}) or {}
        salary = comp.get("compensationTierSummary", "") or ""
        text = strip_html(item.get("descriptionHtml", ""))[:3000]
        jobs.append({
            "source": f"{company['name']} (Ashby)",
            "id": f"ashby-{company['slug']}-{item.get('id')}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": item.get("jobUrl", ""),
            "salary": salary,
            "text": text,
        })
    return jobs


def fetch_recruitee_company(company):
    url = f"https://{company['slug']}.recruitee.com/api/offers"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("offers", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        city = item.get("city", "") or ""
        country = item.get("country", "") or ""
        location = ", ".join(p for p in (city, country) if p)
        if item.get("remote"):
            location = (location + " Remote").strip(", ").strip()
        salary = ""
        if item.get("min_salary") or item.get("max_salary"):
            currency = item.get("salary_currency", "") or ""
            salary = f"{currency}{item.get('min_salary','?')}-{currency}{item.get('max_salary','?')}"
        text = strip_html(item.get("description", "") or "")[:3000]
        job_url = item.get("careers_url") or f"https://{company['slug']}.recruitee.com/o/{item.get('slug','')}"
        jobs.append({
            "source": f"{company['name']} (Recruitee)",
            "id": f"recruitee-{company['slug']}-{item.get('id')}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": job_url,
            "salary": salary,
            "text": text,
        })
    return jobs


def fetch_workable_company(company):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{company['slug']}"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        loc = item.get("location", {}) or {}
        location_parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        location = ", ".join(p for p in location_parts if p)
        if loc.get("telecommuting"):
            location = (location + " Remote").strip(", ").strip()
        text = strip_html(item.get("description", "") or "")[:3000]
        job_url = item.get("url", "") or f"https://apply.workable.com/{company['slug']}/j/{item.get('shortcode','')}"
        jobs.append({
            "source": f"{company['name']} (Workable)",
            "id": f"workable-{company['slug']}-{item.get('id') or item.get('shortcode')}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": job_url,
            "salary": "",
            "text": text,
        })
    return jobs


def fetch_smartrecruiters_company(company):
    """SmartRecruiters Posting API -- officially documented as
    unauthenticated for public postings (developers.smartrecruiters.com).
    NOTE: unlike the other fetchers, this one could not be live-tested
    against a real response during development (api.smartrecruiters.com
    wasn't reachable from the build environment) -- built defensively
    from the documented schema, with safe fallbacks throughout, but worth
    keeping an eye on its "title matches" count in the run logs to
    confirm it's actually returning data."""
    url = f"https://api.smartrecruiters.com/v1/companies/{company['slug']}/postings"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = []
    for item in data.get("content", []):
        title = item.get("name", "") or ""
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        loc = item.get("location", {}) or {}
        location_parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        location = ", ".join(p for p in location_parts if p)
        if loc.get("remote"):
            location = (location + " Remote").strip(", ").strip()
        posting_id = item.get("id", "")
        job_url = item.get("applyUrl") or item.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company['slug']}/{posting_id}"
        jobs.append({
            "source": f"{company['name']} (SmartRecruiters)",
            "id": f"smartrecruiters-{company['slug']}-{posting_id}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": job_url,
            "salary": "",
            "text": "",
        })
    return jobs


def fetch_personio_company(company):
    """Personio's public XML career feed -- officially documented by
    Personio itself as requiring no credentials (support.personio.de).
    Same live-testing caveat as SmartRecruiters above."""
    url = f"https://{company['slug']}.jobs.personio.de/xml?language=en"
    req = urllib.request.Request(url, headers={"User-Agent": "job-alert-bot"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    jobs = []
    for pos in root.iter("position"):
        title = (pos.findtext("name") or "").strip()
        if not any(k in title.lower() for k in TITLE_KEYWORDS):
            continue
        office = (pos.findtext("office") or "").strip()
        remote_field = (pos.findtext("recruitingCategory") or "") + (pos.findtext("keywords") or "")
        location = office
        if "remote" in remote_field.lower() or "remote" in office.lower():
            location = (location + " Remote").strip()
        pos_id = pos.findtext("id") or ""
        job_url = (pos.findtext("jobPostingUrl") or "").strip() or f"https://{company['slug']}.jobs.personio.de/job/{pos_id}"
        desc_parts = [pos.findtext(tag) or "" for tag in ("jobDescriptions", "yourProfile", "whatWeOffer")]
        text = strip_html(" ".join(desc_parts))[:3000]
        jobs.append({
            "source": f"{company['name']} (Personio)",
            "id": f"personio-{company['slug']}-{pos_id}",
            "title": title,
            "company": company["name"],
            "location": location,
            "url": job_url,
            "salary": "",
            "text": text,
        })
    return jobs


def fetch_all_web3_companies(max_workers=60):
    """Fan out to every supported-ATS company board in parallel: the web3
    sweep, manually verified extras, priority Silicon Valley companies,
    and the general startup universe, all merged and deduped."""
    try:
        companies = load_web3_company_boards()
    except Exception as e:
        print(f"Failed to load web3 company list: {e}", file=sys.stderr)
        companies = []

    # Merge in the manually verified list, deduping by (ats, slug) so a
    # company already present in the auto-pulled list isn't fetched twice.
    seen_keys = {(c["ats"], c["slug"]) for c in companies}
    for extra in EXTRA_COMPANIES:
        key = (extra["ats"], extra["slug"])
        if key not in seen_keys:
            companies.append(extra)
            seen_keys.add(key)

    # Merge in the Silicon Valley / major tech company list. These carry
    # priority=True on the company dict, propagated onto each job below so
    # notify() can flag them for a more insistent notification.
    try:
        priority_companies = load_priority_companies()
    except Exception as e:
        print(f"Failed to load priority company list: {e}", file=sys.stderr)
        priority_companies = []
    for pc in priority_companies:
        key = (pc["ats"], pc["slug"])
        if key not in seen_keys:
            companies.append(pc)
            seen_keys.add(key)
        # If a company is already present (e.g. also in the web3 sweep),
        # still mark it priority so it gets the elevated notification.
        else:
            for c in companies:
                if (c["ats"], c["slug"]) == key:
                    c["priority"] = True

    # Merge in the general startup universe (~16k companies, all sizes).
    # This is the largest source by far, so it's added last -- everything
    # above (web3, extras, priority SV/tech) keeps its distinct company
    # names and priority flags; this just fills in the long tail.
    try:
        general_companies = load_general_startup_companies()
    except Exception as e:
        print(f"Failed to load general startup list: {e}", file=sys.stderr)
        general_companies = []
    for gc in general_companies:
        key = (gc["ats"], gc["slug"])
        if key not in seen_keys:
            companies.append(gc)
            seen_keys.add(key)

    print(f"Total companies to check this run: {len(companies)}")

    fetch_fn = {
        "greenhouse": fetch_greenhouse_company,
        "lever": fetch_lever_company,
        "ashby": fetch_ashby_company,
        "recruitee": fetch_recruitee_company,
        "workable": fetch_workable_company,
        "smartrecruiters": fetch_smartrecruiters_company,
        "personio": fetch_personio_company,
    }

    jobs = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_fn[c["ats"]], c): c for c in companies}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                company_jobs = fut.result()
                if c.get("priority"):
                    for j in company_jobs:
                        j["priority"] = True
                jobs.extend(company_jobs)
            except Exception as e:
                # Individual company APIs fail sometimes (renamed slug, temp
                # outage, etc.) -- don't let one bad company kill the run.
                print(f"  (skipped {c['name']} / {c['ats']}: {e})", file=sys.stderr)
    return jobs


# ---- Filters -----------------------------------------------------------

def is_globally_remote(job):
    """True if the role reads as open to anyone, anywhere.

    Checked in four tiers, in order:
      1. Explicit global-acceptance language (GLOBAL_OVERRIDE_PATTERNS)
         wins outright -- this rescues US-anchored postings that
         explicitly say they accept applicants from anywhere.
      2. Region-lock / hybrid / onsite language (REMOTE_EXCLUDE_PATTERNS)
         disqualifies -- e.g. "Remote (US only)" contains "remote" but
         isn't globally remote. Applies regardless of source.
      3. Generic remote language (REMOTE_INCLUDE_PATTERNS) passes.
      4. No signal at all: for sources that are remote-only job boards by
         definition (job["remote_native"] is True -- RemoteOK, Remotive,
         WeWorkRemotely, Jobicy, Himalayas), trust that and pass, since
         every listing there is already remote by the board's own nature.
         For everything else (company career pages, HN, Telegram, Getro
         boards -- all of which mix remote and onsite postings), no
         signal means it's dropped, since it genuinely can't be confirmed.
    """
    blob = f"{job.get('location','')} {job.get('title','')} {job.get('text','')}".lower()
    for pat in GLOBAL_OVERRIDE_PATTERNS:
        if pat in blob:
            return True
    for pat in REMOTE_EXCLUDE_PATTERNS:
        if pat in blob:
            return False
    for pat in REMOTE_INCLUDE_PATTERNS:
        if pat in blob:
            return True
    if job.get("remote_native"):
        return True
    return False  # no remote signal at all -> can't confirm, so skip it


def parse_salary_range(salary_text):
    """Extract (min, max) USD figures from a structured salary string like
    '$150,000-$190,000' or '$150K - $200K'. Returns (None, None) if no
    number is found (i.e. undisclosed)."""
    if not salary_text:
        return None, None
    nums = []
    for m in re.finditer(r"\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?([kK])?", salary_text):
        digits, k_suffix = m.groups()
        if not digits:
            continue
        try:
            val = float(digits.replace(",", ""))
        except ValueError:
            continue
        if k_suffix:
            val *= 1000
        if val < 1000:  # filters out noise like a stray "5" from "5 weeks"
            continue
        nums.append(val)
    if not nums:
        return None, None
    return min(nums), max(nums)


def passes_salary_floor(job):
    """Keep if undisclosed, or if the disclosed range reaches the floor."""
    _, hi = parse_salary_range(job.get("salary", ""))
    if hi is None:
        return True
    return hi >= SALARY_FLOOR


def passes_filters(job):
    return is_globally_remote(job) and passes_salary_floor(job)


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
    web3_tag = "🌐 web3" if tag_web3(job) else "web2"
    lo, hi = parse_salary_range(job.get("salary", ""))
    salary_tag = f"✅ verified ${int(hi):,}+" if hi is not None else "❓ salary not listed"
    location = job.get("location", "") or "Remote"
    is_priority = bool(job.get("priority"))

    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC set] Would notify: {'⭐ PRIORITY ' if is_priority else ''}"
              f"{job['title']} @ {job['company']} ({salary_tag}) -> {job['url']}")
        return

    title = f"⭐ Priority PM role: {job['title']}" if is_priority else f"New PM role: {job['title']}"
    message = (
        f"{job['company']} ({job['source']}, {web3_tag})\n"
        f"{location}\n"
        f"{salary_tag}\n"
        f"{job['url']}"
    )
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Click": job["url"],
            # Priority jobs (major Silicon Valley companies) get ntfy's
            # "high" priority -- a more insistent notification sound/style
            # on your phone -- so they stand out from the broader sweep.
            "Priority": "high" if is_priority else "default",
            "Tags": "star" if is_priority else "briefcase",
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
        fetch_himalayas, fetch_arbeitnow, fetch_jobicy, fetch_hn_whoishiring,
    ):
        try:
            jobs = fetcher()
            print(f"{fetcher.__name__}: {len(jobs)} title matches")
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"Fetcher {fetcher.__name__} failed: {e}", file=sys.stderr)

    # Direct web3 company careers pages (~1,000 companies, parallelized)
    try:
        company_jobs = fetch_all_web3_companies()
        print(f"company boards (web3 + priority SV + general startups): {len(company_jobs)} title matches")
        all_jobs.extend(company_jobs)
    except Exception as e:
        print(f"Company board sweep failed: {e}", file=sys.stderr)

    # Web3 VC portfolio boards (Getro) -- early-stage-heavy
    try:
        getro_jobs = fetch_all_getro_networks()
        print(f"Getro VC portfolio boards: {len(getro_jobs)} title matches")
        all_jobs.extend(getro_jobs)
    except Exception as e:
        print(f"Getro network sweep failed: {e}", file=sys.stderr)

    # Telegram web3 job channels
    try:
        telegram_jobs = fetch_all_telegram_channels()
        print(f"Telegram channels: {len(telegram_jobs)} title matches")
        all_jobs.extend(telegram_jobs)
    except Exception as e:
        print(f"Telegram sweep failed: {e}", file=sys.stderr)

    print(f"Total title matches before remote/salary filters: {len(all_jobs)}")

    filtered_jobs = [j for j in all_jobs if passes_filters(j)]
    print(f"Passed remote-global + $150k+ filters: {len(filtered_jobs)}")

    seen = load_seen()
    new_jobs = [j for j in filtered_jobs if j["id"] not in seen]

    print(f"New (not previously seen): {len(new_jobs)}")

    for job in new_jobs:
        notify(job)
        seen.add(job["id"])

    save_seen(seen)


if __name__ == "__main__":
    main()
