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
     law-firm "deadline alert" solicitations, routine insider Form-4 reports,
     retail stock-tip clickbait, and political/non-corporate items.
  4. De-duplicate the same story from multiple outlets.

Per-company headlines are NOT relevance-filtered (any recent news about a tracked
company is useful context) and are NOT pruned by the broad-feed prune. They ARE
passed through the noise filter, so routine insider-transaction reports and promo
clickbait don't clutter the profile or leak into the global headline panels.

We only store/display headline, source, date, and a link out -- never article text.
"""
import hashlib
import os
import re
import time
from datetime import datetime, timedelta

import requests

from . import config, database, activists

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
COMPANY_NEWS_DAYS = int(os.getenv("COMPANY_NEWS_DAYS", "90"))
COMPANY_NEWS_PER = int(os.getenv("COMPANY_NEWS_PER", "6"))
# Activist-naming headlines are kept far longer than the 21-day default and are always
# captured (even past the per-company cap), so the profile's "reported activist" check
# (scoring._activist_news_hit, a 220-day window) still has data to match weeks later. That's
# what lets an already-engaged name route itself into Active Situations without a manual tag.
ACTIVIST_RETAIN_DAYS = int(os.getenv("ACTIVIST_RETAIN_DAYS", "220"))
# Activist capture/retention cues = generic campaign language + the shared known-activist
# list (funds + FGS names + individuals) from activists.py, so this stays in sync with the
# filer gate and the news auto-routing instead of drifting as its own copy.
_GENERIC_CUES = [
    "activist", "13d", "proxy fight", "proxy contest", "proxy battle", "dissident",
]
_ACTIVIST_CUES = _GENERIC_CUES + activists.NEWS_TERMS


def _names_activist(title):
    t = " " + _norm(title) + " "
    return any(c in t for c in _ACTIVIST_CUES)

# A broad-feed headline must trip a relevance keyword to be kept. The keywords are
# split by whether they can stand on their own:
#   STRONG -- inherently corporate/financial; kept on their own.
#   WEAK   -- also fire on non-corporate stories (a mayor "steps down", a charity
#             director "resigns", "stocks tumble" market commentary), so they only
#             count when a CORPORATE ANCHOR is also present. This is what drops
#             "Edmonton mayor's chief of staff steps down" while keeping "Acme Corp
#             CEO steps down" -- a structural fix, not a per-name blocklist.
STRONG_KEYWORDS = [
    "activist", "proxy fight", "proxy battle", "short seller", "short-seller",
    "profit warning", "guidance cut", "cuts guidance", "lowers guidance",
    "lowered guidance", "cuts outlook", "lowers outlook", "cuts forecast",
    "weak guidance", "earnings miss", "misses estimates", "misses expectations",
    "write-down", "writedown", "goodwill impairment", "impairment",
    "restructuring", "strategic review", "explores sale", "exploring sale",
    "considering sale", "strategic alternatives", "take private", "take-private",
    "activist stake", "activist investor", "13d", "poison pill", "delist",
    "going concern", "restatement", "non-reliance", "material weakness",
]
WEAK_KEYWORDS = [
    "steps down", "stepping down", "step down", "to resign", "resigns", "resigned",
    "ousted", "new leader", "new ceo", "leadership change", "shareholder",
    "layoff", "layoffs", "job cuts", "slashes", "slashed",
    "plunge", "plunges", "tumble", "tumbles", "slump", "slumps", "sinks",
    "plummets", "sell-off", "selloff", "downgrade", "downgraded", "warns",
    "turnaround", "scraps", "halts", "falls short", "disappointing", "disappoints",
]
# WEAK keyword only qualifies if one of these is also present. Word-boundaried so
# "inc" can't match "since" or "district".
_CORP_ANCHOR_RE = re.compile(
    r"\b("
    r"inc|corp|corporation|company|companies|plc|ltd|llc|holdings|group|"
    r"technologies|bancorp|bancshares|industries|systems|pharmaceuticals|"
    r"ceo|cfo|coo|cio|chief executive|chief financial|chairman|chairwoman|"
    r"board of directors|nasdaq|nyse|earnings|revenue|guidance|quarterly|"
    r"dividend|buyback|share price|shares|stock|profit|shareholders"
    r")\b"
)

# Drop if the headline contains any of these (noise / off-topic).
# NOTE: _is_noise() runs BEFORE the activist-capture check in refresh_company_news(),
# so every term here must be one that could NOT appear in a genuine activist/distress
# headline we'd want to keep. Keep additions specific (e.g. "options exercise",
# not the bare word "exercise") to avoid suppressing real news.
EXCLUDE_PATTERNS = [
    # law-firm solicitations
    "deadline alert", "investor alert", "class action", "law firm", "lead plaintiff",
    "rosen law", "pomerantz", "bragar", "kessler", "levi & korsinsky", "schall law",
    "robbins", "securities fraud", "reminds investors", "encourages investors",
    "investigation on behalf", "notifies investors", "shareholder rights",
    # law-firm client alerts / M&A-governance memos (not a company-specific campaign) —
    # e.g. a Skadden Delaware-court note that name-matched three unrelated companies.
    "skadden", "wachtell", "sullivan & cromwell", "cravath", "latham & watkins",
    "kirkland & ellis", "paul weiss", "meagher & flom", "cleary gottlieb",
    "advance notice bylaws", "client alert", "client memo", "legal update",
    # foreign / general-interest news roundups ("Today top 7 stories")
    "top 7 stories", "top stories", "today's top", "todays top",
    # academic / health journals
    "plos", "journal", "study", "accumulation", "glycation", "renal", "peer-review",
    "clinical study", "examination population", "doi.org",
    # sports
    "premier league", "west ham", "wycombe", "f.c.", " fc ", "footbal", "soccer",
    "nba", " nfl ", " mlb ", " afc ", " cfc ", "midfielder", "striker",
    " coach ", "head coach", "world cup", " wc exit", "olympic", "tournament",
    "quarterback", "touchdown", " league ",
    # sports staff / management (a "steps down"/"departs" that dodges the words above —
    # e.g. "Xavi Valero steps down from Liverpool goalkeeper coaching role"). Kept sports-
    # specific so a real corporate "CEO/CFO steps down" is never suppressed.
    "goalkeeper", "goalkeeping", "coaching role", "coaching staff", "assistant coach",
    "head coaching", "caretaker manager", "transfer window", "loan spell", "under-21",
    "reserve team", "sporting director", "relegation", "rugby", "cricket", "wimbledon",
    "grand prix", "formula 1", " pga ",
    # govt / non-US share sales
    "crore", " ofs ", "nlc india", " sebi ", "lakh", "disinvestment", " rs ",
    # crypto price/promo (not a company distress signal)
    "airdrop", "memecoin", "presale", "token sale", "bitcoin", " crypto", "ethereum",
    "stablecoin", "dogecoin", "solana",
    # routine insider Form-4 transaction reports (Benzinga format) — not activist/distress
    "options exercise", "exercises options", "realizes $",
    # analyst / retail-promo noise (chart setups, "is it a buy", target-price clickbait)
    "breakout", "momentum", "stocks to buy", "stock to buy", "best stocks", "top stocks",
    "stock a buy", "a buy before", "buy before", "should you buy", "should i buy",
    "is it time to buy", "time to buy", "price target", "chartmill", "zacks",
    "motley fool", "here's why", "this stock", "magnificent seven",
    "dividend aristocrat", "dividend king", "best dividend", "to watch",
    "undervalued", "deep value", "value investors", "turnaround stock",
    "long-term investors", "could reward", "better stock", "to buy now",
    # analyst buy/hold/sell-rating OPINION pieces (SeekingAlpha/Fool genre). These trip a
    # distress keyword like "turnaround" or "downgrade" but are an author's rating, not a
    # company event — e.g. "Turnaround Is Improving, But Still Too Early To Buy".
    "too early to buy", "too soon to buy", "early to buy", "still too early",
    "not a buy", "is a buy", "a buy?", "still a buy", "worth buying",
    "buy rating", "hold rating", "sell rating", "reiterates", "initiates coverage",
    # algorithmic stock-screener content (ChartMill "Caviar Cruise" / quality-screen genre) and
    # bullish CEO/analyst thought-leadership — relevant-sounding but NOT a company event. These
    # were showing on profiles: "Caviar Cruise Quality Screen Analysis", "A Quality Investment
    # with Strong Margins and Cheap Valuation", and the "AeroVironment CEO warns global warfare
    # faces a historic inflection point" macro comment that mis-fired the negative-headline signal.
    "quality screen", "caviar cruise", "screen analysis", "value screen", "growth screen",
    "stock screen", "quality investment", "quality stock", "inflection point",
    "more bullish", "more bearish", "bull case", "bear case", "revolution makes",
    # political / non-corporate "ousted/steps down/fight" false positives
    "worker party", "workers' party", "party chief", "cadres", "prime minister",
    "parliament", "general election", "lawmaker", "republican", "democrat",
    "communist", " gop ", "senator", "congress", "white house", "governor",
    # foreign government / military / politics (non-US-corporate; "defence" is the British
    # spelling, so it reliably flags UK/foreign items, e.g. "UK Defence Spending ... Army Chief Warns")
    "defence", "army chief", "royal navy", "warships", "labour party", "ministry of",
    "defense spending", "military spending",
    # entertainment / gaming studios (often private; not corporate distress, e.g. a "Games CEO steps down")
    "video game", "game studio", "games ceo", "game developer", "gaming studio",
    # local government / municipal / non-profit leadership (the exact items that leaked
    # into the pitch email: a charity director, a mayor's chief of staff, a PM's aide)
    "gospel mission", " mayor", "mayoral", "chief of staff", "city council",
    "city manager", "town council", "charity", "nonprofit", "non-profit", "starmer",
    "trump wants", "steps down as president", "school board",
    # broad-market commentary / index roundups (not a company event; leaked as "Distress")
    "mag 7", "chip stocks", "stocks step", "summer rally", "santa rally",
    "rally signals", "outperformance points", "big tech resurgence", "options opportunities",
    "options trade", " ndx ", "nasdaq 100", "dow jones", "premarket", "pre-market",
    "sector etf", "market rally", "stock market today",
    # additional opinion / listicle / earnings-preview genres seen leaking into the broad feed
    "buy, sell or hold", "buy, sell, or hold", "buy sell or hold", "sell or hold",
    "hold the stock", "favorite stocks", "wall street's favorite", "wall streets favorite",
    "most active stocks", "trending stock", "stocks that fall short", "falls short",
    "beat estimates", "beat earnings", "top-ranked", "top ranked", "reasons to buy",
    "reasons to sell", "3 reasons", "5 reasons", "curious about", "bargain",
    "is it a buy", "cheap after", "fairly valued", "attractive risk-reward",
    "here's what you need", "what you should know", "what you need to know",
    "buy the dip", "run for the hills", "belongs on your shortlist", "on your shortlist",
    "fairly priced", "looks fairly", "exit now", "back in growth mode", "still smack",
    "contrarian opportunity", "before q2 earnings", "ahead of q2", "swing ahead",
]

# Opinion / academic / legal-aggregator / hyper-local-news domains -- drop regardless of
# the headline text. jdsupra.com is a law-firm client-alert aggregator (not company news);
# news24.com is a South-African general-news site whose "top stories" roundups leaked in.
EXCLUDE_DOMAINS = [
    "theconversation.com", "castanetkamloops.net", "edmontonsun.com",
    "castanet.net", "jdsupra.com", "news24.com", "lexology.com", "mondaq.com",
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


def _domain_excluded(domain):
    d = (domain or "").lower()
    return any(bad in d for bad in EXCLUDE_DOMAINS)


def is_relevant(title):
    t = " " + _norm(title) + " "
    if not t.strip():
        return False
    if _is_noise(title):
        return False
    if any(kw in t for kw in STRONG_KEYWORDS):
        return True
    # A weak keyword (steps down / tumbles / resigns) only counts with a corporate anchor.
    if any(kw in t for kw in WEAK_KEYWORDS) and _CORP_ANCHOR_RE.search(t):
        return True
    return False


# --- Activist-relevance ranking for the daily-email "Top headlines" --------------------------
# The on-site panels rank headlines client-side (app.js); the emailed pitch kit needs the same
# server-side, so smart-money / activism items lead instead of generic market/distress noise.
_RANK_OWNERSHIP = ("13g", "13f", "builds stake", "raises stake", "takes stake", "boosts stake",
                   "new stake", "million stake", "takes a position", "new position",
                   "acquires a stake", "hedge fund", "activist", "13d", "proxy fight",
                   "proxy contest", "elliott", "starboard", "icahn", "ackman", "pershing square",
                   "ancora", "jana", "third point", "trian", "valueact", "engaged capital")
_RANK_STRATEGIC = ("strategic review", "explores sale", "exploring sale", "takeover", "buyout",
                   "to acquire", "merger", "spin-off", "spinoff", "break up", "break-up", "sale of")
_RANK_LEADERSHIP = ("steps down", "stepping down", "resigns", "ousted", "departs", "interim ceo",
                    "new ceo", "names ceo", "leadership change", "shake-up", "shakeup")


def relevance_rank(title):
    """Activist-relevance bucket (lower = surfaced first): ownership/activism, then strategic
    (M&A / sale), then leadership, then everything else."""
    t = " " + _norm(title) + " "
    if any(k in t for k in _RANK_OWNERSHIP):
        return 0
    if any(k in t for k in _RANK_STRATEGIC):
        return 1
    if any(k in t for k in _RANK_LEADERSHIP):
        return 2
    return 3


def rank_relevant(rows, limit=6):
    """Order stored headlines by activist-relevance, tie-broken by recency; trim to `limit`."""
    ordered = sorted(rows, key=lambda r: r.get("published_at") or "", reverse=True)  # newest first
    ordered.sort(key=lambda r: relevance_rank(r.get("headline") or ""))              # stable
    return ordered[:limit]


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
        if not url or _domain_excluded(a.get("domain")) or not is_relevant(title):
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
        src = (a.get("source") or {}).get("name") or ""
        # Check the URL too, not just the source NAME — the excluded-domain list is domains
        # (jdsupra.com), but the source name is often "JD Supra", which the domain list misses.
        if not url or _domain_excluded(src) or _domain_excluded(url) or not is_relevant(title):
            continue
        out.append({
            "id": _hash(url), "headline": title,
            "source": src,
            "published_at": a.get("publishedAt") or "", "url": url,
        })
    return out


# --------------------------------------------------------------------------- #
# Per-company news (Finnhub)                                                   #
# --------------------------------------------------------------------------- #
def purge_excluded_news():
    """Delete stored headlines that now match an exclude rule (a law-firm client alert like the
    Skadden/jdsupra Delaware-court memo, an excluded domain, or noise) but were ingested BEFORE
    that rule existed — otherwise they linger in the news table and keep routing names to Active
    Situations. Runs each company-news refresh. Returns the count removed."""
    try:
        rows = database.news_all_brief()
    except Exception:
        return 0
    bad = [n["id"] for n in rows
           if _is_noise(n.get("headline")) or _domain_excluded(n.get("url"))
           or _domain_excluded(n.get("source"))]
    n = database.delete_news(bad)
    if n:
        print(f"[news] purged {n} stale excluded headlines")
    return n


def refresh_company_news(tickers, key, days=COMPANY_NEWS_DAYS, per_symbol=COMPANY_NEWS_PER):
    """Recent headlines for each tracked ticker from Finnhub's company-news endpoint,
    stored in the shared `news` table tagged with that ticker (so the company profile
    can show them). Real-time and ticker-clean — no fuzzy name matching. Returns the
    number of headlines stored. No-op without a key or tickers."""
    purge_excluded_news()          # scrub stale items ingested before an exclude rule existed
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
            # skip analyst-promo / chart-setup / off-topic clutter AND law-firm client alerts /
            # excluded domains (a Skadden/jdsupra Delaware-court memo was routing 3 names to
            # Active Situations). Check the URL for the domain (source name misses "jdsupra.com").
            if _is_noise(head) or _domain_excluded(url):
                continue
            is_act = _names_activist(head)
            # Keep the most-recent `per_symbol` general headlines for display, but ALWAYS
            # capture an activist-naming headline (even past the cap) so it's available to
            # route the name to Active Situations.
            if n >= per_symbol and not is_act:
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
            if not is_act:
                n += 1
        time.sleep(0.15)           # under Finnhub's 60/min free limit
    return kept


# Loose ticker/first-word matching cross-tagged the wrong company on the broad feed (M7): a
# Gilead story -> PFE, chip stories -> INTC, "general" -> General Mills, or the 3-letter ticker
# "ALL" matching the word "all". We now require a DISTINCTIVE token match with word boundaries
# (the full multi-word name core, or a distinctive >=6-char name word that isn't a generic /
# sector stopword), and only accept a raw ticker in an unambiguous context ($PFE, "(NYSE: PFE)",
# or a >=4-char standalone ticker). Precision over recall — the broad feed is reference material.
_TICK_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "companies",
                "holdings", "holding", "group", "plc", "ltd", "limited", "lp", "llc", "sa",
                "ag", "nv", "class", "the"}
_TICK_STOP = _TICK_SUFFIX | {
    "and", "for", "of", "north", "northern", "south", "southern", "east", "west", "western",
    "eastern", "central", "pacific", "atlantic", "american", "america", "national",
    "international", "global", "united", "states", "general", "standard", "premier", "allied",
    "consolidated", "advanced", "dynamic", "prime", "select", "public", "federal", "mutual",
    "community", "first", "new", "next", "true", "core", "liberty", "freedom", "heritage",
    "pioneer", "frontier", "summit", "gateway", "horizon", "regional", "strategic",
    "diversified", "consumer", "opportunity", "growth", "development", "value",
    "financial", "finance", "capital", "bancorp", "bancshares", "bank", "banking", "energy",
    "power", "electric", "gas", "oil", "petroleum", "water", "utilities", "utility", "health",
    "healthcare", "medical", "pharma", "pharmaceutical", "pharmaceuticals", "therapeutics",
    "technology", "technologies", "digital", "data", "cloud", "cyber", "software", "systems",
    "networks", "network", "communications", "telecom", "media", "entertainment", "properties",
    "property", "realty", "estate", "homes", "residential", "hotels", "resorts", "retail",
    "stores", "foods", "food", "beverage", "brands", "products", "goods", "materials",
    "chemical", "chemicals", "steel", "metals", "mining", "minerals", "motors", "auto",
    "automotive", "airlines", "airways", "transport", "transportation", "logistics", "express",
    "freight", "shipping", "marine", "defense", "aerospace", "industrial", "industries",
    "manufacturing", "services", "service", "solutions", "partners", "resources", "enterprises",
    "ventures", "insurance", "securities", "investments", "investment", "management", "trust",
    # generic corporate-name words that must NOT anchor a distinctive match on their own —
    # e.g. "Fidelity National *Information* Services" (FIS) was tagging "Inside information:
    # Valmet…". The company's distinctive token ("fidelity") still matches; the generic one won't.
    "information", "informatics", "interactive", "worldwide", "consulting"}
_KEY_CACHE = {}

# Common English words that are ALSO real tickers. A bare standalone word like "five" or "open"
# in a headline is far more likely the ordinary word than a reference to Five Below (FIVE) or
# Opendoor (OPEN) -- so these are NOT accepted via the bare-ticker path in _ticker_hit(). They can
# still match with an UNAMBIGUOUS ticker context ($FIVE, "(NASDAQ: FIVE)") or via the company's
# NAME appearing in the headline ("Five Below"), which is the correct anchor. This is what fixes
# 'UK retailers issue FIVE profit warnings' being tagged Five Below, and the same class of error
# for any word-like ticker. Extend freely -- membership only ever REMOVES a weak bare-word match,
# never a name/cashtag/exchange match, so adding a word here cannot cause a wrong tag.
_COMMON_WORD_TICKERS = {
    "five", "four", "nine", "open", "love", "well", "play", "fast", "good", "best", "real",
    "true", "free", "safe", "edge", "peak", "path", "gold", "iron", "cost", "rent", "loan",
    "cash", "ride", "work", "live", "life", "care", "home", "hood", "plug", "boom", "wise",
    "rise", "move", "grow", "news", "time", "pool", "wave", "leap", "step", "gain", "lock",
    "seat", "roof", "book", "read", "post", "sign", "wall", "road", "lane", "park", "farm",
    "seed", "corn", "fuel", "coal", "mine", "ship", "boat", "lift", "beam", "bolt", "bond",
    "hunt", "camp", "dock", "port", "fort", "peak", "wing", "star", "moon", "atom", "cell",
    "gene", "flow", "loop", "node", "byte", "chip", "code", "link", "mode", "unit", "beta",
    "buzz", "dash", "deck", "dice", "duo", "epic", "flex", "grid", "halo", "hero", "ionq", "jazz",
    "aura", "onto", "very", "sofi", "upst",
    # 5+ letter common words that are tickers
    "block", "smart", "prime", "swift", "sharp", "solid", "clear", "trade", "stock", "share",
    "value", "yield", "money", "trust", "power", "spark", "pulse", "vital", "quest", "focus",
    "bloom", "cargo", "crown", "eagle", "field", "flash", "flint", "frost", "globe", "grace",
    "amble", "match", "media", "arena", "asset", "audio", "bench", "boost", "brave", "bumble",
}
# Only length >= 4 tickers ever reach the bare-word path, so 3-letter words don't need listing.

# Bump this string whenever the matcher logic changes. On the first ingest after deploy, every
# stored headline is re-checked with the current matcher (see _retag_stored_if_needed), so stale
# tags from an older matcher -- e.g. an ADBE tag on a ServiceNow headline -- clear at once instead
# of lingering up to ~21 days until the row ages out.
MATCHER_VERSION = "2026-07-28-mistag-guard-r2"

# The broad feed and per-company (Finnhub) headlines live in the SAME `news` table with no column
# recording which feed a row came from. A per-company row is deliberately tagged with its ticker
# even when the headline never repeats the company name, so a blanket "drop every tag the matcher
# no longer reproduces" would silently un-link legitimate profile headlines. Hence the re-tag is
# CONSERVATIVE by default: it only drops a stale tag when that ticker is a common English word
# (the exact class the new guard fixes, e.g. FIVE) -- which can never be a real standalone
# per-company tag surviving on its own. Flip this env flag to also clear NON-common-word ghosts
# (the ADBE-on-a-ServiceNow-headline case); that clears more but can briefly un-link the odd
# legitimate headline until the next company-news refresh re-adds it. Start conservative, read the
# retag count in the logs, then decide.
RETAG_CLEAR_ALL_UNMATCHED = os.getenv("RETAG_CLEAR_ALL_UNMATCHED", "0") == "1"


def _company_keys(name):
    """(core_phrase, {distinctive keys}) for a company name — the full multi-word core plus any
    distinctive (>=6-char, non-stopword) single token. Empty when nothing distinctive remains."""
    toks = re.findall(r"[a-z0-9&]+", (name or "").lower())
    while toks and toks[-1] in _TICK_SUFFIX:
        toks.pop()
    if not toks:
        return "", set()
    keys = set()
    core = " ".join(toks)
    if len(toks) >= 2:
        keys.add(core)
    for t in toks:
        if t in _TICK_STOP:
            continue
        if len(t) >= 6 or (len(toks) == 1 and len(t) >= 4):
            keys.add(t)
    return core, keys


def _keys_for(c):
    tk = c.get("ticker") or ""
    cached = _KEY_CACHE.get(tk)
    if cached is None:
        cached = _company_keys(c.get("name") or "")
        _KEY_CACHE[tk] = cached
    return cached


def _ticker_hit(norm_head, raw_head, tk):
    """True only when the ticker appears unambiguously: a cashtag ($PFE), exchange-prefixed
    ('(NYSE: PFE)', 'NASDAQ: PFE'), or a >=4-char ticker as a standalone word. Short 3-letter
    tickers as bare words (ALL, GIS, HOG) are NOT accepted — too many English collisions."""
    t = tk.lower()
    if not t:
        return False
    low = raw_head.lower()
    if re.search(r"\$" + re.escape(t) + r"\b", low):
        return True
    if re.search(r"(?:nasdaq|nyse|nyse american|amex|otc|cboe|bats)\s*:?\s*" + re.escape(t) + r"\b", low):
        return True
    if re.search(r"\(\s*(?:[a-z\. ]{0,10}:\s*)?" + re.escape(t) + r"\s*\)", low):
        return True
    # A >=4-char ticker as a bare standalone word -- but NOT when that "ticker" is really a common
    # English word (five, open, cost, ...), which collides far more often than it refers to the
    # company. Those must arrive via a cashtag/exchange/paren (handled above) or the company name.
    # Match the ticker CASE-SENSITIVELY as UPPERCASE in the raw headline: real ticker references are
    # written upper-case ("DASH shares fell"), so a title-cased common word or surname ("…Kumar
    # Dash…", "Bill Gross") no longer collides with a same-spelled ticker (DASH, GROSS).
    if (len(t) >= 4 and t not in _COMMON_WORD_TICKERS
            and re.search(r"(?<![A-Za-z0-9])" + re.escape(tk.upper()) + r"(?![A-Za-z0-9])", raw_head)):
        return True
    return False


def _match_tickers(headline, companies):
    raw = headline or ""
    norm = " " + re.sub(r"[^a-z0-9&]+", " ", raw.lower()) + " "
    matched = []
    for c in companies:
        tk = c.get("ticker")
        if not tk:
            continue
        _core, keys = _keys_for(c)
        hit = False
        for k in keys:
            if " " in k:                       # multi-word core: plain substring is safe
                if k in norm:
                    hit = True
                    break
            elif re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", norm):
                hit = True
                break
        if not hit and _ticker_hit(norm, raw, tk):
            hit = True
        if hit:
            matched.append(tk)
    return matched


def _prune_stored():
    """Re-check stored headlines against the current filter and delete any that no longer
    qualify (cleans out old noise when the filter tightens). Per-company news (rows tagged
    with a ticker) is re-checked against the NOISE filter too — so newly-added
    EXCLUDE_PATTERNS retroactively clear old promo/insider clutter — but is NOT
    relevance-pruned, only aged out. Everything older than 21 days is dropped to keep the
    table fresh (activist-naming headlines get a longer retention window)."""
    cutoff = (datetime.utcnow() - timedelta(days=21)).isoformat()
    act_cutoff = (datetime.utcnow() - timedelta(days=ACTIVIST_RETAIN_DAYS)).isoformat()
    try:
        with database.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, headline, published_at, matched_tickers FROM news").fetchall()
            drop = []
            for r in rows:
                tagged = (r["matched_tickers"] or "").strip()
                pub = r["published_at"] or ""
                head = r["headline"]
                act = _names_activist(head)
                # Activist-naming headlines get the long retention window so they can keep a
                # name routed to Active Situations; everything else ages out at 21 days.
                too_old = pub < (act_cutoff if act else cutoff)
                # Noise is dropped from BOTH feeds (clears old clutter when the filter
                # tightens), but never an activist-naming headline. Broad-feed rows (no
                # ticker) must additionally stay relevant.
                is_noise = _is_noise(head) and not act
                if too_old or is_noise or (not tagged and not is_relevant(head)):
                    drop.append(r["id"])
            conn.executemany("DELETE FROM news WHERE id=?", [(i,) for i in drop])
        return len(drop)
    except Exception:
        return 0


def _retag_stored_if_needed(companies):
    """One-time re-tag of stored headlines after a matcher change, gated by MATCHER_VERSION and
    mirroring edgar's version-bump re-parse: it runs on the first ingest after a deploy that
    changed the version string, then no-ops on every later run. For each row it re-runs the
    current matcher and DROPS a stale tag the matcher no longer produces -- but, per the note on
    RETAG_CLEAR_ALL_UNMATCHED, only for common-word tickers by default so legitimate per-company
    tags are preserved. It never adds tags and never deletes rows (pruning stays in
    _prune_stored). Best-effort: any DB error is swallowed so a cleanup hiccup can't stall the
    feed."""
    if not companies:
        return 0
    try:
        with database.get_conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS news_meta (key TEXT PRIMARY KEY, val TEXT)")
            cur = conn.execute(
                "SELECT val FROM news_meta WHERE key='matcher_version'").fetchone()
            if cur is not None and cur["val"] == MATCHER_VERSION:
                return 0
            rows = conn.execute("SELECT id, headline, matched_tickers FROM news").fetchall()
            changed = 0
            for r in rows:
                old = [t for t in (r["matched_tickers"] or "").split(",") if t]
                if not old:
                    continue                       # untagged broad rows: nothing to clean
                newset = set(_match_tickers(r["headline"], companies))
                keep = []
                for t in old:
                    if t in newset:
                        keep.append(t)             # still a valid match — keep
                    elif t.lower() in _COMMON_WORD_TICKERS or RETAG_CLEAR_ALL_UNMATCHED:
                        continue                   # stale common-word (or, if flagged, any) — drop
                    else:
                        keep.append(t)             # preserve: may be a legit per-company tag
                newval = ",".join(keep)
                if newval != (r["matched_tickers"] or ""):
                    conn.execute("UPDATE news SET matched_tickers=? WHERE id=?",
                                 (newval, r["id"]))
                    changed += 1
            conn.execute(
                "INSERT OR REPLACE INTO news_meta(key, val) VALUES('matcher_version', ?)",
                (MATCHER_VERSION,))
        print(f"[news] retag {MATCHER_VERSION}: cleaned {changed} stale-tag row(s) "
              f"(clear_all_unmatched={RETAG_CLEAR_ALL_UNMATCHED})", flush=True)
        return changed
    except Exception as e:  # noqa: BLE001 - never let a cleanup error stall ingestion
        print(f"[news] retag skipped (non-fatal): {e}", flush=True)
        return 0


def ingest(companies, limit=40):
    provider = (config.NEWS_PROVIDER or "newsapi").lower()
    _retag_stored_if_needed(companies)   # one-time stale-tag cleanup after a matcher-version bump
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
