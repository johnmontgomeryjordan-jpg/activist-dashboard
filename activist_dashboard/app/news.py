"""
Financial news ingestion via a free-tier news API (NewsAPI or GNews).

Relevance strategy (tuned to cut noise):
  1. Ask the API to match our activist/distress terms in the HEADLINE only.
  2. Re-check each headline against TITLE_KEYWORDS locally (defense in depth).
  3. Drop law-firm "deadline alert / class action" solicitation spam.
  4. De-duplicate the same story coming from multiple outlets.

We only ever store/display headline, source, date, and a link out. Never the
article body.
"""
import hashlib
import re

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"

# Sent to the API; restricted to the headline via searchIn/in = title.
QUERY = ('"activist investor" OR "proxy fight" OR "short seller" OR '
         '"earnings miss" OR "guidance cut" OR "profit warning" OR '
         '"write-down" OR impairment OR restructuring OR '
         '"strategic review" OR "steps down" OR "stake in"')

# A headline must contain at least one of these to be kept.
TITLE_KEYWORDS = [
    "activist", "proxy fight", "proxy battle", "short seller", "short-seller",
    "shareholder", "earnings miss", "misses earnings", "guidance cut",
    "cuts guidance", "lowers guidance", "profit warning", "write-down",
    "writedown", "impairment", "restructuring", "layoff", "job cuts",
    "strategic review", "spinoff", "spin-off", "steps down", "ceo",
    "ousted", "stake in", "board seat", "breakup", "poison pill",
    "turnaround", "activist investor", "guidance",
]

# Drop these — mostly law-firm investor solicitations, not real news.
EXCLUDE_PATTERNS = [
    "deadline alert", "investor alert", "class action", "law firm", "lawsuit",
    "lead plaintiff", "encourages investors", "reminds investors",
    "investigation on behalf", "shareholder rights", "rosen law", "pomerantz",
    "bragar", "kessler", "levi & korsinsky", "robbins", "schall law",
    "securities fraud", "contact the firm", "notifies investors",
]


def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _is_relevant(title):
    t = _norm_title(title)
    if not t:
        return False
    if any(bad in t for bad in EXCLUDE_PATTERNS):
        return False
    return any(kw in t for kw in TITLE_KEYWORDS)


def fetch_headlines(limit=40):
    if not config.NEWS_API_KEY:
        return []
    if config.NEWS_PROVIDER == "gnews":
        return _fetch_gnews(limit)
    return _fetch_newsapi(limit)


def _fetch_newsapi(limit):
    params = {
        "q": QUERY,
        "searchIn": "title",          # require the term in the headline
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
        title = a.get("title") or ""
        if not url or not _is_relevant(title):
            continue
        out.append({
            "id": _hash(url),
            "headline": title,
            "source": (a.get("source") or {}).get("name") or "",
            "published_at": a.get("publishedAt") or "",
            "url": url,
        })
    return out


def _fetch_gnews(limit):
    params = {
        "q": QUERY,
        "in": "title",                # require the term in the headline
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
        title = a.get("title") or ""
        if not url or not _is_relevant(title):
            continue
        out.append({
            "id": _hash(url),
            "headline": title,
            "source": (a.get("source") or {}).get("name") or "",
            "published_at": a.get("publishedAt") or "",
            "url": url,
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


def ingest(companies, limit=40):
    """Fetch headlines, drop duplicates by title, tag to companies, store."""
    articles = fetch_headlines(limit)
    seen_titles = set()
    kept = 0
    for a in articles:
        key = _norm_title(a["headline"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        a["matched_tickers"] = ",".join(_match_tickers(a["headline"], companies))
        database.upsert_news(a)
        kept += 1
    return kept
