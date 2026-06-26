"""
Financial news ingestion for the broad early-warning feed.

Providers (set via NEWS_PROVIDER):
  - "gdelt"   -> GDELT 2.0 DOC API. Free, no key, real-time (~15 min), commercial-OK.
                 This is the recommended source: it fixes NewsAPI's 24-hour delay and
                 non-commercial license at zero cost. (Default.)
  - "newsapi" -> NewsAPI.org (legacy; free tier is 24h-delayed + non-commercial).
  - "gnews"   -> GNews (legacy alternative).

Per-company news does NOT come through here -- that runs on Finnhub's per-symbol
company-news endpoint (see news pipeline). This module powers the firm-wide feed
and the daily brief.

Relevance strategy (tuned for "distressed / activist-attracting" public-company news):
  1. Ask the source to match distress/activist terms in the HEADLINE.
  2. Re-check each headline locally against DISTRESS_KEYWORDS.
  3. Drop noise: academic journals, sports, govt share sales, crypto promos,
     and law-firm "deadline alert" solicitations.
  4. De-duplicate the same story from multiple outlets.

We only store/display headline, source, date, and a link out -- never article text.
"""
import hashlib
import os
import re

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Financial outlets only (NewsAPI 'domains' filter). Override via NEWS_DOMAINS env.
DEFAULT_DOMAINS = ("reuters.com,bloomberg.com,wsj.com,ft.com,cnbc.com,marketwatch.com,"
                   "barrons.com,fortune.com,businessinsider.com,forbes.com,thestreet.com,"
                   "seekingalpha.com,fool.com,benzinga.com,investing.com,finance.yahoo.com,"
                   "businesswire.com,globenewswire.com,prnewswire.com,nasdaq.com")
NEWS_DOMAINS = os.getenv("NEWS_DOMAINS", DEFAULT_DOMAINS)

# Sent to NewsAPI/GNews; matched against the headline only.
QUERY = ('"profit warning" OR "guidance cut" OR "cuts guidance" OR "lowers guidance" '
         'OR "earnings miss" OR "misses estimates" OR "activist investor" OR "proxy fight" '
         'OR "short seller" OR "strategic review" OR "explores sale" OR restructuring '
         'OR impairment OR "write-down" OR downgraded OR "profit warning" OR selloff '
         'OR plunges OR tumbles OR "steps down"')

# Sent to GDELT. GDELT's full-text engine is sensitive to very long boolean
# strings, so this is a tighter, high-signal set of quoted phrases. Each phrase
# is finance-specific enough to keep political/other "activist" noise out, and
# the local DISTRESS_KEYWORDS re-check below is the second gate.
GDELT_QUERY = os.getenv(
    "GDELT_QUERY",
    '("activist investor" OR "proxy fight" OR "proxy battle" OR "short seller" '
    'OR "profit warning" OR "cuts guidance" OR "lowers guidance" OR "earnings miss" '
    'OR "strategic review" OR "explores sale" OR "goodwill impairment") sourcelang:english',
)
# How far back GDELT looks each pull (its feed is real-time; we keep a few days).
GDELT_TIMESPAN = os.getenv("GDELT_TIMESPAN", "3d")

# A headline must contain at least one of these to be kept.
DISTRESS_KEYWORDS = [
    "activist", "proxy fight", "proxy battle", "short seller", "short-seller",
    "shareholder", "profit warning", "guidance cut", "cuts guidance",
    "lowers guidance", "lowered guidance", "cuts outlook", "lowers outlook",
    "cuts forecast", "slashes", "earnings miss", "misses estimates",
    "misses expectations", "falls short", "disappointing", "disappoints",
    "write-down", "writedown", "impairment", "restructuring", "layoff",
    "job cuts", "strategic review", "explores sale", "exploring sale",
    "considering sale", "steps down", "stepping down", "ousted", "to resign",
    "plunge", "plunges", "tumble", "tumbles", "slump", "slumps", "sinks",
    "plummets", "sell-off", "selloff", "downgrade", "downgraded", "warns",
    "weak guidance", "turnaround", "scraps", "halts", "slashed",
]

# Drop if the headline contains any of these (noise / off-topic).
EXCLUDE_PATTERNS = [
    # law-firm solicitations
    "deadline alert", "investor alert", "class action", "law firm", "lead plaintiff",
    "rosen law", "pomerantz", "bragar", "kessler", "levi & korsinsky", "schall law",
    "robbins", "securities fraud", "reminds investors", "encourages investors",
    "investigation on behalf", "notifies investors", "shareholder rights",
    # academic / health journals
    "plos", "journal", "study", "accumulation", "glycation", "renal", "peer-review",
    "clinical study", "examination population", "doi.org",
    # sports
    "premier league", "west ham", "wycombe", "f.c.", " fc ", "footbal", "soccer",
    "nba", " nfl ", " mlb ", " afc ", " cfc ", "midfielder", "striker",
    # govt / non-US share sales
    "crore", " ofs ", "nlc india", " sebi ", "lakh", "disinvestment", " rs ",
    # crypto promo
    "airdrop", "memecoin", "presale", "token sale",
]


def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def is_relevant(title):
    t = " " + _norm(title) + " "
    if not t.strip():
        return False
    if any(bad in t for bad in EXCLUDE_PATTERNS):
        return False
    return any(kw in t for kw in DISTRESS_KEYWORDS)


def fetch_headlines(limit=40):
    provider = (config.NEWS_PROVIDER or "").lower()
    if provider == "gdelt":
        return _fetch_gdelt(limit)
    # Legacy providers below require an API key.
    if not config.NEWS_API_KEY:
        return []
    if provider == "gnews":
        return _fetch_gnews(limit)
    return _fetch_newsapi(limit)


# --------------------------------------------------------------------------- #
# GDELT 2.0 DOC API (default)                                                  #
# --------------------------------------------------------------------------- #
def _fetch_gdelt(limit):
    params = {
        "query": GDELT_QUERY,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": min(max(int(limit), 1), 250),
        "sort": "DateDesc",
        "timespan": GDELT_TIMESPAN,
    }
    try:
        r = requests.get(
            GDELT_URL, params=params, timeout=25,
            headers={"User-Agent": "activist-dashboard/1.0 (+internal research tool)"},
        )
        r.raise_for_status()
        # GDELT returns HTML (not JSON) when a query is malformed/empty; that
        # raises ValueError here and we fail closed to an empty list.
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    return _normalize_gdelt(data.get("articles", []) or [])


def _gdelt_date(s):
    """GDELT seendate '20260625T120000Z' -> ISO '2026-06-25T12:00:00Z'."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", s)
    if not m:
        return s
    y, mo, d, h, mi, se = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{se}Z"


def _normalize_gdelt(articles):
    out = []
    for a in articles:
        url = a.get("url") or ""
        title = a.get("title") or ""
        if not url or not is_relevant(title):
            continue
        out.append({
            "id": _hash(url),
            "headline": title,
            "source": a.get("domain") or "",
            "published_at": _gdelt_date(a.get("seendate")),
            "url": url,
        })
    return out


# --------------------------------------------------------------------------- #
# Legacy providers (NewsAPI / GNews)                                           #
# --------------------------------------------------------------------------- #
def _fetch_newsapi(limit):
    params = {
        "q": QUERY,
        "searchIn": "title",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": min(limit, 100),
        "apiKey": config.NEWS_API_KEY,
    }
    if NEWS_DOMAINS:
        params["domains"] = NEWS_DOMAINS
    try:
        r = requests.get(NEWSAPI_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    return _normalize(data.get("articles", []))


def _fetch_gnews(limit):
    params = {
        "q": QUERY, "in": "title", "lang": "en",
        "max": min(limit, 100), "sortby": "publishedAt",
        "apikey": config.NEWS_API_KEY,
    }
    try:
        r = requests.get(GNEWS_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    return _normalize(data.get("articles", []))


def _normalize(articles):
    out = []
    for a in articles:
        url = a.get("url") or ""
        title = a.get("title") or ""
        if not url or not is_relevant(title):
            continue
        out.append({
            "id": _hash(url), "headline": title,
            "source": (a.get("source") or {}).get("name") or "",
            "published_at": a.get("publishedAt") or "", "url": url,
        })
    return out


def _match_tickers(headline, companies):
    text = headline.lower()
    matched = []
    for c in companies:
        name = (c.get("name") or "").lower()
        short = name.split()[0] if name else ""
        ticker = (c.get("ticker") or "").lower()
        if short and len(short) > 3 and short in text:
            matched.append(c["ticker"])
        elif ticker and f" {ticker} " in f" {text} ":
            matched.append(c["ticker"])
    return matched


def _prune_stored():
    """Re-check already-stored headlines against the current filter and delete
    any that no longer qualify (cleans out old noise when the filter tightens).
    Also drops anything older than 21 days to keep the feed fresh."""
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=21)).isoformat()
    try:
        with database.get_conn() as conn:
            rows = conn.execute("SELECT id, headline, published_at FROM news").fetchall()
            drop = [r["id"] for r in rows
                    if not is_relevant(r["headline"]) or (r["published_at"] or "") < cutoff]
            conn.executemany("DELETE FROM news WHERE id=?", [(i,) for i in drop])
        return len(drop)
    except Exception:
        return 0


def ingest(companies, limit=40):
    provider = (config.NEWS_PROVIDER or "newsapi").lower()
    articles = fetch_headlines(limit)
    seen, kept = set(), 0
    for a in articles:
        key = _norm(a["headline"])
        if key in seen:
            continue
        seen.add(key)
        a["matched_tickers"] = ",".join(_match_tickers(a["headline"], companies))
        database.upsert_news(a)
        kept += 1
    pruned = _prune_stored()
    # Visible in the deploy logs so you can confirm the feed is live each run,
    # which provider served it, and how much survived the relevance filter.
    print(f"[news] provider={provider} fetched={len(articles)} kept={kept} "
          f"pruned={pruned}", flush=True)
    return kept
