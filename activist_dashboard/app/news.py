"""
Financial news ingestion.

Two complementary sources:
  * THEMATIC feed (NewsAPI) -- broad activist / distress / governance headlines for
    the dashboard, restricted to financial outlets and matched to the universe.
  * COMPANY news (Finnhub, free tier) -- per-ticker news for the names that matter
    (shortlist / active situations / watchlist), so each company's detail view and
    its event score are driven by real, company-specific news.

Relevance is enforced locally (keyword sets + cue-gated price verbs), noise is
dropped (law-firm/forensic solicitations, academic, sports, crypto, entertainment),
and repeats are de-duplicated on read. We only store headline, source, date, link.
"""
import hashlib
import os
import re

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"
FINNHUB_URL = "https://finnhub.io/api/v1/company-news"

# Financial outlets only (NewsAPI 'domains' filter). Override via NEWS_DOMAINS env.
DEFAULT_DOMAINS = ("reuters.com,bloomberg.com,wsj.com,ft.com,cnbc.com,marketwatch.com,"
                   "barrons.com,fortune.com,businessinsider.com,forbes.com,thestreet.com,"
                   "seekingalpha.com,fool.com,benzinga.com,investing.com,finance.yahoo.com,"
                   "businesswire.com,globenewswire.com,prnewswire.com,nasdaq.com")
NEWS_DOMAINS = os.getenv("NEWS_DOMAINS", DEFAULT_DOMAINS)

# Sent to the API; matched against the headline only. Kept under NewsAPI's 500-char
# query limit. Covers price moves, distress, activist funds/terms, proxy advisors,
# executive changes, and strategic-review/campaign language.
QUERY = ('"profit warning" OR "guidance cut" OR "earnings miss" OR "misses estimates" '
         'OR activist OR "proxy fight" OR "proxy contest" OR "short seller" '
         'OR "strategic review" OR "strategic alternatives" OR restructuring OR impairment OR downgraded '
         'OR plunges OR tumbles OR slides OR slips OR falls OR drops OR sinks OR slumps '
         'OR "steps down" OR resigns OR "interim CEO" OR "Glass Lewis" OR "13D" '
         'OR "board seat" OR Starboard OR Icahn OR Ancora OR "Jana Partners" OR "Elliott Management"')

# A headline must contain at least one of these (substring) to be kept.
DISTRESS_KEYWORDS = [
    "activist", "proxy fight", "proxy battle", "short seller", "short-seller",
    "shareholder", "profit warning", "guidance cut", "cuts guidance",
    "lowers guidance", "lowered guidance", "cuts outlook", "lowers outlook",
    "cuts forecast", "slashes", "earnings miss", "misses estimates",
    "misses expectations", "falls short", "disappointing", "disappoints",
    "write-down", "writedown", "impairment", "restructuring", "layoff",
    "job cuts", "strategic review", "strategic alternatives", "explores sale",
    "exploring sale", "considering sale", "explore alternatives", "exploring alternatives",
    "steps down", "stepping down", "ousted", "to resign",
    "downgrade", "downgraded", "warns",
    "weak guidance", "turnaround", "scraps", "halts", "slashed",
]
# NOTE: generic price verbs (sink/plunge/tumble/slump/plummet/selloff) are NOT in
# the list above on purpose -- they run through the cue-gated MOVE_PATTERN below so
# headlines like "Sinks Navy Infrastructure" don't leak in without a finance cue.

# Extra accept-terms for the activist / proxy / executive-change buckets. Kept as
# precise phrases (no bare "iss"/"stake") so we don't reintroduce noise.
EXTRA_KEYWORDS = [
    # activist funds + campaign mechanics
    "proxy contest", "13d", "schedule 13d", "board seat", "board seats",
    "director nominee", "nominates", "builds stake", "raises stake", "takes stake",
    "boosts stake", "elliott management", "starboard", "trian", "jana partners",
    "third point", "carl icahn", "icahn", "nelson peltz", "valueact", "value act",
    "engine no", "ancora", "politan", "sachem head", "legion partners",
    # proxy advisors
    "glass lewis", "proxy advisor", "proxy adviser",
    "institutional shareholder services", "iss recommends", "iss advises",
    "iss backs", "recommends against", "withhold vote", "withhold votes",
    # executive changes
    "resigns", "resigned", "steps aside", "departs", "departure",
    "interim ceo", "interim cfo", "names ceo", "new ceo", "appoints ceo",
    "names new chief", "leadership change", "management shake", "shake-up",
    "shakeup", "reshuffle", "ousts", "exits as ceo", "exits as cfo",
]

# Negative price-move verbs, matched as whole words (so "slideshow", "shortfall",
# "backdrop", "landslide" do NOT match). Captures the gentler headline style:
# "Apple shares slide", "Nasdaq slips", "Tesla stock drops".
MOVE_PATTERN = re.compile(
    r"\b("
    r"slid|slide|slides|slip|slips|slipped|"
    r"fall|falls|fell|drop|drops|dropped|"
    r"dip|dips|dipped|sink|sinks|sank|"
    r"slump|slumps|slumped|decline|declines|declined|"
    r"retreat|retreats|retreated|"
    r"plunge|plunges|tumble|tumbles|plummet|plummets|sell-?off"
    r")\b"
)

# A bare price-move verb only counts as relevant if the headline ALSO contains one
# of these market cues -- otherwise "set drops", "sinks navy", "Talent falls 12%"
# (non-financial) leak in. Distress / activist / exec keywords don't need a cue.
FINANCE_CUES = [
    "shares", "stock", "nasdaq", "s&p", " dow ", "dow jones", "wall street",
    " market", "earnings", "guidance", "revenue", "profit", "quarter", "forecast",
    "outlook", "valuation", "premarket", "pre-market", "after-hours", "investor",
    "dividend", "buyback", "analyst", "price target", "bond yield", "shareholder",
    " etf", " ipo", " shr ", "market cap", "valuation",
]

# Drop if the headline contains any of these (noise / off-topic).
EXCLUDE_PATTERNS = [
    # law-firm solicitations / forensic-short reports
    "deadline alert", "investor alert", "class action", "law firm", "lead plaintiff",
    "rosen law", "pomerantz", "bragar", "kessler", "levi & korsinsky", "schall law",
    "robbins", "securities fraud", "reminds investors", "encourages investors",
    "investigation on behalf", "notifies investors", "shareholder rights",
    "hagens berman", "forensic report", "forensic analysis", "investor rights",
    "class period", "investigates", "investigating whether", "lawsuit",
    # academic / health journals
    "plos", "journal", "study", "accumulation", "glycation", "renal", "peer-review",
    "clinical study", "examination population", "doi.org",
    # sports
    "premier league", "west ham", "wycombe", "f.c.", " fc ", "footbal", "soccer",
    "nba", " nfl ", " mlb ", " afc ", " cfc ", "midfielder", "striker",
    # govt / non-US share sales
    "crore", " ofs ", "nlc india", " sebi ", "lakh", "disinvestment", " rs ",
    # crypto promo / price noise
    "airdrop", "memecoin", "presale", "token sale", "bitcoin", "ethereum",
    # entertainment / box office / music charts
    "box office", "box-office", "weekend debut", "ticket sales",
    "album", "albums", "billboard", "climate activist",
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
    if any(kw in t for kw in DISTRESS_KEYWORDS):
        return True
    if any(kw in t for kw in EXTRA_KEYWORDS):
        return True
    if MOVE_PATTERN.search(t) and any(cue in t for cue in FINANCE_CUES):
        return True
    return False


def fetch_headlines(limit=100):
    if not config.NEWS_API_KEY:
        return []
    if config.NEWS_PROVIDER == "gnews":
        return _fetch_gnews(limit)
    return _fetch_newsapi(limit)


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


def fetch_company_news(ticker, key, days=21):
    """Per-company news from Finnhub (free tier), filtered to our relevance rules."""
    from datetime import datetime, timedelta
    to = datetime.utcnow().date()
    frm = to - timedelta(days=days)
    try:
        r = requests.get(FINNHUB_URL, params={
            "symbol": ticker, "from": frm.isoformat(), "to": to.isoformat(), "token": key
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for a in data:
        head = a.get("headline") or ""
        url = a.get("url") or ""
        if not head or not url or not is_relevant(head):
            continue
        ts = a.get("datetime")
        try:
            published = datetime.utcfromtimestamp(ts).isoformat() if ts else ""
        except (ValueError, OSError, TypeError):
            published = ""
        out.append({"id": _hash(url), "headline": head,
                    "source": a.get("source") or "Finnhub",
                    "published_at": published, "url": url})
    return out


def refresh_company_news(tickers, key, days=21):
    """Fetch + store recent relevant news for each ticker (shortlist / watchlist).
    Finnhub free tier allows 60 calls/min, so a ~30-name set is well within budget."""
    if not key:
        return 0
    import time
    seen, kept = set(), 0
    for tk in tickers:
        tk = (tk or "").strip().upper()
        if not tk or tk in seen:
            continue
        seen.add(tk)
        for a in fetch_company_news(tk, key, days):
            a["matched_tickers"] = tk
            database.upsert_news(a)
            kept += 1
        time.sleep(0.25)
    _prune_stored()
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


def ingest(companies, limit=100):
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
    _prune_stored()
    return kept
