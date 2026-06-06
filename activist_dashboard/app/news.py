"""
Financial news ingestion via a free-tier news API.

Supports two providers (set NEWS_PROVIDER in .env):
  * newsapi  -> https://newsapi.org  (free dev tier, 100 req/day)
  * gnews    -> https://gnews.io      (free tier, 100 req/day)

We only ever store and display: headline, source, date, and a link out to the
original article. Article body text is never reproduced (per the brief).
"""
import hashlib

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"

# A compact query that OR's the most useful distress/activist terms. Kept short
# because free tiers limit query length and request volume.
QUERY = ('"activist investor" OR "proxy fight" OR "earnings miss" OR '
         '"guidance cut" OR "CEO departure" OR "write-down" OR "short seller" OR '
         '"shareholder pressure" OR restructuring')


def _hash(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def fetch_headlines(limit=40):
    """Return a list of normalized news dicts. Empty list if no key configured."""
    if not config.NEWS_API_KEY:
        return []
    if config.NEWS_PROVIDER == "gnews":
        return _fetch_gnews(limit)
    return _fetch_newsapi(limit)


def _fetch_newsapi(limit):
    params = {
        "q": QUERY,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": min(limit, 100),
        "apiKey": config.NEWS_API_KEY,
    }
    try:
        r = requests.get(NEWSAPI_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for a in data.get("articles", []):
        url = a.get("url") or ""
        if not url:
            continue
        out.append({
            "id": _hash(url),
            "headline": a.get("title") or "(no title)",
            "source": (a.get("source") or {}).get("name") or "",
            "published_at": a.get("publishedAt") or "",
            "url": url,
        })
    return out


def _fetch_gnews(limit):
    params = {
        "q": QUERY,
        "lang": "en",
        "max": min(limit, 100),
        "sortby": "publishedAt",
        "apikey": config.NEWS_API_KEY,
    }
    try:
        r = requests.get(GNEWS_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for a in data.get("articles", []):
        url = a.get("url") or ""
        if not url:
            continue
        out.append({
            "id": _hash(url),
            "headline": a.get("title") or "(no title)",
            "source": (a.get("source") or {}).get("name") or "",
            "published_at": a.get("publishedAt") or "",
            "url": url,
        })
    return out


def _match_tickers(headline, companies):
    """Tag a headline with any monitored company whose name appears in it."""
    text = headline.lower()
    matched = []
    for c in companies:
        name = (c.get("name") or "").lower()
        # Use the first word of the company name (e.g. "Boeing") as a loose match,
        # plus an exact ticker token match.
        short = name.split()[0] if name else ""
        ticker = (c.get("ticker") or "").lower()
        if short and len(short) > 3 and short in text:
            matched.append(c["ticker"])
        elif ticker and f" {ticker} " in f" {text} ":
            matched.append(c["ticker"])
    return matched


def ingest(companies, limit=40):
    """Fetch headlines, tag them to companies, store. Returns count ingested."""
    articles = fetch_headlines(limit)
    for a in articles:
        a["matched_tickers"] = ",".join(_match_tickers(a["headline"], companies))
        database.upsert_news(a)
    return len(articles)
