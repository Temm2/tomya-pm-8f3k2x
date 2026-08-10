# Free Product Manager Job Alert Bot

Checks:
- **Aggregator boards:** RemoteOK, Remotive, We Work Remotely, Himalayas,
  Arbeitnow, Jobicy
- **Web3-native board:** CryptoJobsList
- **~1,000 individual web3/DeFi company careers pages**, pulled live each
  run from a daily-updated public company list, for every company running
  Greenhouse, Lever, or Ashby (all three have free public JSON APIs)

...every 4 hours, for new "Product Manager" postings, and pushes a free
phone notification for each new one. 100% free — no paid APIs, no server
to rent.

## Setup (~10 minutes, no coding required)

1. **Create a free GitHub account** if you don't have one: github.com/join

2. **Create a new repository**
   - Go to github.com/new
   - Name it anything (e.g. `pm-job-alerts`)
   - Set it to **Public**. This matters now: the bot checks ~1,000
     company career pages per run, and public repos get **unlimited**
     free GitHub Actions minutes, while private repos only get 2,000/month
     (a private repo would likely run out partway through the month at
     this scale). Your ntfy topic name still lives only in the encrypted
     `NTFY_TOPIC` secret — secrets aren't visible even on public repos,
     and none of your personal info goes into the code itself.
   - Click "Create repository"

3. **Upload these 3 files**, keeping the folder structure:
   ```
   check_jobs.py
   .github/workflows/job-alert.yml
   README.md   (optional, just for your own reference)
   ```
   Easiest way: on the repo page, click "Add file" → "Upload files", drag
   all three in (GitHub will recreate the `.github/workflows/` folder
   automatically from the file path).

4. **Get free push notifications via ntfy.sh**
   - Install the ntfy app: [iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
     (or just use https://ntfy.sh/ in a browser tab you leave open)
   - Pick a random, hard-to-guess topic name, e.g. `tomya-pm-alerts-x7f2`
     (anyone who knows this exact name can see your alerts, so don't use
     something guessable like "tomya-jobs")
   - In the app, tap "+" and subscribe to that topic name

5. **Add the topic name as a GitHub secret**
   - In your repo: Settings → Secrets and variables → Actions → New repository secret
   - Name: `NTFY_TOPIC`
   - Value: the topic name you picked (e.g. `tomya-pm-alerts-x7f2`)

6. **Turn it on**
   - Go to the "Actions" tab in your repo → click "I understand my workflows, go ahead and enable them" if prompted
   - Click "Job Alert Check" → "Run workflow" to test it immediately
   - The first run checks ~1,000 companies in parallel and typically
     finishes in 1-3 minutes. Watch it under the Actions tab; if a lot of
     companies fail, that's normal (some slugs go stale as companies
     rename boards) — the run still completes and reports how many
     matches it found.
   - After that it runs automatically every 4 hours, for free, forever
     (public repos get unlimited GitHub Actions minutes)

## Adjusting it

- **Check more/less often:** edit the `cron:` line in `job-alert.yml`.
  `0 */3 * * *` = every 3 hours. `0 */6 * * *` = every 6 hours.
- **Change keywords:** edit `TITLE_KEYWORDS` / `WEB3_KEYWORDS` at the top
  of `check_jobs.py`.
- **Add more sources:** any site with a public RSS feed or JSON API can be
  added as another `fetch_...()` function — send me the URL and I can
  wire it in.

## Why this is close to "all possible sources"

Between the 6 aggregator boards, CryptoJobsList, and ~1,000 individual
web3 company career pages, this covers the large majority of where web3
PM roles actually get posted first — often before they even reach
aggregator boards, since it queries each company's own careers page
directly.

## What this still doesn't cover

- **web3.career** — one of the largest dedicated web3 boards, but its
  RSS/API access is paid-only. Use its free native "Job Alert" email
  signup as a manual supplement.
- **LinkedIn / Indeed / Wellfound** — no free programmatic access at all;
  their own free "job alert" email features are the only free option.
- **~150 companies** on the source list that use BambooHR, Recruitee,
  Workable, Workday, Deel, Getro, or a fully custom careers page. These
  don't expose a simple no-key JSON endpoint the way Greenhouse/Lever/
  Ashby do, so they're skipped for now. If there's a specific company
  in this bucket you really care about (e.g. Circle on Workday, Dragonfly
  on BambooHR), tell me and I can add a one-off fetcher for it —
  most of these ATSs *can* be queried, it just takes a bit more code
  per platform since they're not as standardized.
- **The company list updates daily but isn't perfect** — some slugs go
  stale when a company renames its board; the script just logs and skips
  those rather than failing the whole run.
