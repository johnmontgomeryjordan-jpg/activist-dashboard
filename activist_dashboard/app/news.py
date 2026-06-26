"""
Financial news ingestion.

Two jobs, two sources:
  * BROAD early-warning feed (ingest)  -> GDELT 2.0 DOC API. Free, no key, real-time
    (~15 min), commercial-OK. Default provider. (Legacy NewsAPI/GNews kept as options.)
  * PER-COMPANY news (refresh_company_news) -> Finnhub's company-news endpoint, by ticker.
    Stored in the same `news` table tagged with the company's ticker so the company
    profile can show its recent headlines.

Relevance strategy for the BROAD feed:
  1. Query distress/activist terms (GDELT is broad, so we query each term and merge).
  2. Re-check each headline locally against DISTRESS_KEYWORDS.
  3. Drop noise: academic journals, sports, govt share sales, crypto promos,
     law-firm "deadline alert" solicitations.
  4. De-duplicate the same story from multiple outlets.

Per-company headlines are NOT relevance-filtered (any recent news about a tracked
company is useful context) and are NOT pruned by the broad-feed prune.

We only store/display headline, source, date, and a link out -- never article text.
"""
import hashlib
import os
import re
import time
from datetime import datetime, timedelta

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"

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

# GDELT's full-text engine rejects long multi-clause boolean queries (returns an empty
# HTML page), which is why a single giant OR-query came back with 0 results. So we query
# each high-signal phrase SEPARATELY and merge + de-dupe the results. Each of these is a
# short, well-formed query GDELT reliably answers. Override the list via env if needed.
GDELT_TERMS = [t.strip() for t in os.getenv(
    "GDELT_TERMS",
    '"activist investor"|"proxy fight"|"short seller"|"profit warning"|'
    '"cuts guidance"|"strategic review"|"explores sale"|"goodwill impairment"|'
    '"earnings miss"|"steps down"'
).split("|") if t.strip()]
# How far back GDELT looks each pull. Its feed is real-time, but a wider window keeps the
# feed from going empty on quiet days (the local relevance filter still trims it).
GDELT_TIMESPAN = os.getenv("GDELT_TIMESPAN", "7d")
# Max articles to request per term per pull.
GDELT_PER_TERM = int(os.getenv("GDELT_PER_TERM", "50"))
# Pause between per-term GDELT calls. GDELT throttles rapid bursts (what produced the
# intermittent fetched=0), so we space the calls out and retry a throttled term once.
GDELT_SLEEP = float(os.getenv("GDELT_SLEEP", "0.7"))

# Per-company (Finnhub) news settings.
COMPANY_NEWS_DAYS = int(os.getenv("COMPANY_NEWS_DAYS", "21"))
COMPANY_NEWS_PER = int(os.getenv("COMPANY_NEWS_PER", "6"))

# A headline must contain at least one of these to be kept (broad feed only).
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
    # analyst / retail-promo noise (chart setups, "is it a buy", target-price clickbait)
    "breakout", "momentum", "stocks to buy", "best stocks", "top stocks",
    "stock a buy", "a buy before", "buy before", "should you buy", "should i buy",
    "is it time to buy", "time to buy", "price target", "chartmill", "zacks",
    "motley fool", "here's why", "this stock", "magnificent seven",
    "dividend aristocrat", "dividend king", "best dividend", "to watch",
    # political / non-corporate "ousted/steps down/fight" false positives
    "worker party", "workers' party", "party chief", "cadres", "prime minister",
    "parliament", "general election", "lawmaker",
]


def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _is_noise(title):
    """Promo / off-topic / non-corporate headline that should never be shown, regardless
    of whether it also trips a distress keyword (e.g. 'Worker Party chief ousted')."""
    t = " " + _norm(title) + " "
    return any(bad in t for bad in EXCLUDE_PATTERNS)


def is_relevant(title):
    t = " " + _norm(title) + " "
    if not t.strip():
        return False
    if _is_noise(title):
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
# GDELT 2.0 DOC API (default broad feed) — one query per term, merged          #
# --------------------------------------------------------------------------- #
def _gdelt_one(term, maxrecords):
    params = {
        "query": f"{term} sourcelang:english",
        "mode": "ArtList",
        "format": "json",
        "maxrecords": min(max(int(maxrecords), 1), 250),
        "sort": "DateDesc",
        "timespan": GDELT_TIMESPAN,
    }
    try:
        r = requests.get(
            GDELT_URL, params=params, timeout=25,
            headers={"User-Agent": "activist-dashboard/1.0 (+internal research tool)"},
        )
        if r.status_code != 200:
            return None                       # throttled / transient -> caller can retry
        # GDELT returns HTML (not JSON) when throttled or the query is malformed; that
        # raises ValueError -> treat as a transient miss the caller can retry, not a
        # genuine empty.
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    return _normalize_gdelt(data.get("articles", []) or [])


def _fetch_gdelt(limit):
    out, seen = [], set()
    for term in GDELT_TERMS:
        res = _gdelt_one(term, GDELT_PER_TERM)
        if res is None:                       # throttled/failed -> one slower retry
            time.sleep(2.0)
            res = _gdelt_one(term, GDELT_PER_TERM)
        for a in (res or []):
            if a["url"] in seen:
                continue
            seen.add(a["url"])
            out.append(a)
        time.sleep(GDELT_SLEEP)               # space calls so GDELT doesn't throttle
    return out


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


# --------------------------------------------------------------------------- #
# Per-company news (Finnhub)                                                   #
# --------------------------------------------------------------------------- #
def refresh_company_news(tickers, key, days=COMPANY_NEWS_DAYS, per_symbol=COMPANY_NEWS_PER):
    """Recent headlines for each tracked ticker from Finnhub's company-news endpoint,
    stored in the shared `news` table tagged with that ticker (so the company profile
    can show them). Real-time and ticker-clean — no fuzzy name matching. Returns the
    number of headlines stored. No-op without a key or tickers."""
    if not key or not tickers:
        return 0
    frm = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    to = datetime.utcnow().date().isoformat()
    kept = 0
    for tk in tickers:
        try:
            r = requests.get(FINNHUB_COMPANY_NEWS_URL,
                             params={"symbol": tk, "from": frm, "to": to, "token": key},
                             timeout=20)
            arts = r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            arts = []
        if not isinstance(arts, list):
            arts = []
        arts.sort(key=lambda a: a.get("datetime") or 0, reverse=True)
        n = 0
        for a in arts:
            url = a.get("url") or ""
            head = a.get("headline") or ""
            if not url or not head:
                continue
            if _is_noise(head):       # skip analyst-promo / chart-setup / off-topic clutter
                continue
            dt = a.get("datetime")
            try:
                pub = datetime.utcfromtimestamp(int(dt)).isoformat() if dt else ""
            except (ValueError, TypeError, OSError):
                pub = ""
            database.upsert_news({
                "id": _hash(url), "headline": head,
                "source": a.get("source") or "",
                "published_at": pub, "url": url,
                "matched_tickers": tk,
            })
            kept += 1
            n += 1
            if n >= per_symbol:
                break
        time.sleep(0.15)           # under Finnhub's 60/min free limit
    return kept


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
    """Re-check the BROAD-feed headlines against the current filter and delete any that
    no longer qualify (cleans out old noise when the filter tightens). Per-company news
    (rows tagged with a ticker) is NEVER relevance-pruned — only aged out. Everything
    older than 21 days is dropped to keep the table fresh."""
    cutoff = (datetime.utcnow() - timedelta(days=21)).isoformat()
    try:
        with database.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, headline, published_at, matched_tickers FROM news").fetchall()
            drop = []
            for r in rows:
                tagged = (r["matched_tickers"] or "").strip()
                too_old = (r["published_at"] or "") < cutoff
                # broad-feed rows (no ticker) must stay relevant; company rows only age out
                if too_old or (not tagged and not is_relevant(r["headline"])):
                    drop.append(r["id"])
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
