"""
Financial news ingestion via a free-tier news API (NewsAPI or GNews).

Relevance strategy (tuned for "distressed / activist-attracting" public-company news):
  1. Ask the API to match distress / activist / proxy / exec / price-move terms
     in the HEADLINE only.
  2. Restrict to financial outlets via a domain allowlist (NewsAPI).
  3. Re-check each headline locally against the keyword sets + MOVE_PATTERN.
  4. Drop noise: academic journals, sports, govt share sales, crypto promos,
     entertainment/box-office, and law-firm "deadline alert" solicitations.
  5. De-duplicate the same story from multiple outlets.

Headlines are bucketed into categories (activist / proxy / exec / price-mover /
market / distress) on the FRONT END from the stored headline, so no DB change is
needed here. This module just makes sure the right headlines get fetched + kept.

We only store/display headline, source, date, and a link out -- never article text.
"""
import hashlib
import os
import re

import requests

from . import config, database

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GNEWS_URL = "https://gnews.io/api/v4/search"

# Financial outlets only (NewsAPI 'domains' filter). Override via NEWS_DOMAINS env.
DEFAULT_DOMAINS = ("reuters.com,bloomberg.com,wsj.com,ft.com,cnbc.com,marketwatch.com,"
                   "barrons.com,fortune.com,businessinsider.com,forbes.com,thestreet.com,"
                   "seekingalpha.com,fool.com,benzinga.com,investing.com,finance.yahoo.com,"
                   "businesswire.com,globenewswire.com,prnewswire.com,nasdaq.com")
NEWS_DOMAINS = os.getenv("NEWS_DOMAINS", DEFAULT_DOMAINS)

# Sent to the API; matched against the headline only. Kept under NewsAPI's 500-char
# query limit. Covers price moves, distress, activist funds/terms, proxy advisors,
# and executive changes so each front-end category has something to show.
QUERY = ('"profit warning" OR "guidance cut" OR "earnings miss" OR "misses estimates" '
         'OR "activist investor" OR "proxy fight" OR "proxy contest" OR "short seller" '
         'OR "strategic review" OR restructuring OR impairment OR "write-down" OR downgraded '
         'OR plunges OR tumbles OR slides OR slips OR falls OR drops OR sinks OR slumps '
         'OR "steps down" OR resigns OR "interim CEO" OR "Glass Lewis" OR "13D" '
         'OR "board seat" OR Starboard OR Icahn OR "Elliott Management"')

# A headline must contain at least one of these (substring) to be kept.
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
    r"retreat|retreats|retreated"
    r")\b"
)

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
    # crypto promo / price noise
    "airdrop", "memecoin", "presale", "token sale", "bitcoin", "ethereum",
    # entertainment / box office
    "box office", "box-office", "weekend debut", "ticket sales",
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
    if MOVE_PATTERN.search(t):
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
