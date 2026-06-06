# Activist Vulnerability — Early Warning Dashboard

A proof-of-concept web dashboard that scans U.S. public companies for early signs
of activist-investor vulnerability **before** an activist shows up — so your firm
can reach out proactively.

It pulls **only free, public data** (SEC EDGAR, a free news API, Yahoo Finance),
scores every company on a simple point system, and surfaces a daily **"Companies
to Pitch"** shortlist. It also sends a **daily email digest at 4:00 PM ET**.

---

## What you'll end up with

* A live web page (your own URL) your team can open in any browser, showing:
  * **Top half** — a live news feed (left) and live SEC EDGAR filings feed (right), refreshing every 30 minutes.
  * **Bottom half** — a ranked table of the most vulnerable companies, updated daily.
  * An email sign-up box for the daily digest.
* A **daily digest email** with the day's top 5 headlines and top 5 companies.

> **Want to see it first?** Open `demo_preview.html` (double-click it) in any
> browser. It's a self-contained snapshot with sample data — no setup needed.
> This is exactly what the live dashboard looks like.

---

## How vulnerability is scored

Each company earns points on a rolling 90-day window. Reach **3 points** and it's
flagged. The 10–15 highest scores become the shortlist.

| Signal | Points | Source |
|---|---|---|
| CEO / C-suite departure (8-K) | 2 | EDGAR |
| Stock drops 5%+ on the CEO-change announcement | +1 bonus | Yahoo Finance |
| Earnings miss or guidance cut (8-K / 10-Q) | 2 | EDGAR |
| Goodwill impairment / write-down (8-K / 10-K) | 2 | EDGAR |
| Layoff announcement (8-K) | 1 | EDGAR |
| Negative activist / restructuring headline | 1 | News API |
| 1-year total shareholder return negative | 1 | Yahoo Finance |
| 3-year TSR in bottom quartile of the universe | 1 | Yahoo Finance |
| Low price-to-book (below 1.5x) | 1 | Yahoo Finance |

You can change the threshold and weights via settings (no coding) — see
**Settings** below.

---

## The 3 free accounts you need (≈15 minutes)

You don't strictly need all of them — the dashboard runs with none — but EDGAR is
free with no signup, and the other two unlock news headlines and email.

### 1. SEC EDGAR — no account, free
Nothing to sign up for. The SEC only asks that the app identify itself with your
firm name + an email. You'll paste that in as `SEC_USER_AGENT` later, e.g.
`Acme Activist Defense (jane@acme.com)`.

### 2. News headlines — NewsAPI (free) **(optional)**
1. Go to **https://newsapi.org/register** and sign up (free "Developer" plan).
2. Copy your **API key** from the dashboard.
3. You'll paste it in as `NEWS_API_KEY`.
   *(Alternative: GNews at https://gnews.io — set `NEWS_PROVIDER=gnews`.)*

### 3. Email digest — Resend (free) **(optional)**
1. Go to **https://resend.com/signup** and create a free account.
2. In the Resend dashboard, **API Keys → Create API Key**, copy it. → `EMAIL_API_KEY`.
3. To send from your own domain, add it under **Domains** and follow Resend's DNS
   steps. For quick testing, Resend lets you send from `onboarding@resend.dev`
   to your own verified email — set `EMAIL_FROM=onboarding@resend.dev`.
   *(Alternative: SendGrid at https://sendgrid.com — set `EMAIL_PROVIDER=sendgrid`.)*

Keep these keys somewhere safe. You'll paste them into the hosting dashboard in
the next step — you never edit the code.

---

## Deploy to a live URL — Render.com (easiest, free)

This repo includes a `render.yaml` blueprint, so Render builds everything for you.

### Step A — Put the code on GitHub
1. Create a free account at **https://github.com** if you don't have one.
2. Click **+ → New repository**, name it `activist-dashboard`, click **Create**.
3. On the new repo page, click **uploading an existing file**, then drag in the
   **entire contents of this folder** (the `app` folder, `requirements.txt`,
   `render.yaml`, `README.md`, etc.). Click **Commit changes**.
   *(Don't upload a `.env` file if you made one — keep keys out of GitHub.)*

### Step B — Create the service on Render
1. Create a free account at **https://render.com** and connect your GitHub.
2. Click **New + → Blueprint**.
3. Pick your `activist-dashboard` repo. Render reads `render.yaml` and proposes a
   web service. Click **Apply**.
4. Render will ask you to fill in the secret values (the ones marked "sync:
   false"). Paste in:
   * `SEC_USER_AGENT` → `Your Firm Name (you@yourfirm.com)`
   * `NEWS_API_KEY` → your NewsAPI key (or leave blank)
   * `EMAIL_API_KEY` → your Resend key (or leave blank)
   * `EMAIL_FROM` → `onboarding@resend.dev` (or your verified domain address)
5. Click **Create / Deploy**. First build takes ~3–5 minutes.
6. When it finishes, Render shows a URL like
   `https://activist-dashboard.onrender.com`. **That's your live dashboard** —
   share it with the team.

> **Free-tier note:** Render's free web services sleep after ~15 minutes of no
> visitors and take ~30 seconds to wake on the next visit. The scheduler also
> pauses while asleep. For an always-on demo (so the 30-min refresh and 4 PM
> email always fire), upgrade that one service to Render's cheapest paid tier, or
> use Railway.app. No code changes needed either way.

### Alternative hosts
* **Railway.app** — New Project → Deploy from GitHub repo → add the same
  environment variables under *Variables*. Start command:
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
* **Heroku** — a `Procfile` is included; `git push heroku main`, then set the
  same variables under *Settings → Config Vars*.

---

## Run it on your own computer first (optional)

If you'd like to try it locally before deploying:

```bash
# 1. Install Python 3.11+ from python.org if you don't have it.
# 2. In a terminal, inside this folder:
pip install -r requirements.txt

# 3. Copy the example settings and open .env in any text editor to add your keys:
cp .env.example .env

# 4. (Optional) load sample data so the page looks full immediately:
python seed_demo.py

# 5. Start it:
uvicorn app.main:app --reload

# 6. Open http://127.0.0.1:8000 in your browser.
```

---

## Settings (change behavior without touching code)

All settings are environment variables — set them in Render's dashboard (or your
`.env` file). The useful ones:

| Setting | Default | What it does |
|---|---|---|
| `SCORE_THRESHOLD` | `3` | Minimum points to be flagged. |
| `MIN_MARKET_CAP` | `1000000000` | Smallest company watched ($1B). |
| `SCORE_WINDOW_DAYS` | `90` | Rolling window for signals. |
| `SHORTLIST_SIZE` | `15` | How many companies on the shortlist. |
| `REFRESH_MINUTES` | `30` | How often news + filings refresh. |
| `DIGEST_HOUR_ET` | `16` | Hour (ET) the digest sends. 16 = 4 PM. |
| `NEWS_PROVIDER` | `newsapi` | `newsapi` or `gnews`. |
| `EMAIL_PROVIDER` | `resend` | `resend` or `sendgrid`. |

### Which companies are watched
The monitored list lives in **`app/universe.csv`** (a simple two-column file:
`ticker,name`). It ships with the **full S&P 1500** — all 1,489 current
constituents of the S&P 500, S&P 400 (MidCap), and S&P 600 (SmallCap), covering
large, mid, and small cap (the brief's suggested proxy). The app automatically
looks up each ticker's SEC ID at startup. To add or remove companies, just edit
rows and re-deploy. The `$1B` market-cap filter (`MIN_MARKET_CAP`) still applies
on top, so smaller S&P 600 names below the threshold are automatically excluded
from the shortlist unless you lower it.

> **Performance note:** with ~1,500 companies, a full EDGAR sweep takes a few
> minutes and the daily Yahoo Finance refresh longer. Both run in the background
> on a schedule, so the dashboard stays responsive. On a small/free host the
> first full refresh after startup can take several minutes to populate — this
> is normal.

---

## Daily email digest — how it works

* Anyone can subscribe via the box on the dashboard. Addresses are stored in the
  app's database (`data.db`).
* Every day at the configured hour (default 4 PM ET) the app emails every
  subscriber the top 5 headlines and top 5 companies, with links.
* The subscriber list is yours to manage. To send a test digest immediately,
  visit `https://YOUR-URL/api/send-test-digest` once email is configured, or
  POST to that path.

---

## What's intentionally NOT in this demo

Per the brief, these need proprietary/paid data and are left for a future paid
version: SG&A vs. named proxy peers, sum-of-the-parts / conglomerate discount,
acquisition ROI / M&A track record, CEO pay vs. TSR over tenure, and full peer-
group benchmarking.

---

## How the pieces fit together (for a technical teammate)

```
app/
  config.py      All settings, read from environment variables.
  universe.py    Loads universe.csv and maps tickers -> SEC CIK numbers.
  edgar.py       Pulls & classifies 8-K / 10-K / 10-Q filings from SEC EDGAR.
  news.py        Pulls activist/distress headlines from NewsAPI or GNews.
  market.py      Market cap, P/B, and 1y/3y TSR via yfinance (Yahoo Finance).
  scoring.py     The point-based vulnerability model.
  database.py    SQLite storage (filings, news, scores, subscribers).
  emailer.py     Daily digest email via Resend or SendGrid.
  pipeline.py    Orchestrates the 30-min refresh and the daily rescore+digest.
  main.py        FastAPI web server + APScheduler jobs + JSON API.
  static/
    index.html   The single-file dashboard UI.
seed_demo.py     Loads sample data for an instant-looking demo.
render.yaml      One-click deploy config for Render.com.
requirements.txt Python dependencies.
.env.example     Template for your settings/keys.
```

Data flow: the scheduler calls `pipeline.refresh_data()` every 30 minutes
(EDGAR + news + rescore) and `pipeline.daily_rescore_and_digest()` once a day
(refresh market data, rescore, email). The browser polls `/api/feed`,
`/api/shortlist`, and `/api/status` for what to display.

---

## Costs & limits

Everything here fits in free tiers: EDGAR is free; NewsAPI/GNews allow ~100
requests/day free; Resend's free tier sends thousands of emails/month; Render's
free tier serves the site (with the sleep caveat noted above). No paid data
feeds are used anywhere.

## A note on data accuracy

This is a **proof-of-concept demo**. EDGAR item-code and keyword classification
is a reasonable first pass but will occasionally mis-tag or miss a filing, and
Yahoo Finance figures are unofficial. Treat the shortlist as a lead-generation
starting point a human reviews — not a final determination.
