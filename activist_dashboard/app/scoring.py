"""
Predictive target-attractiveness scoring.
Structural, peer-relative within sector (2-digit SIC):
  Cheap valuation (price-to-book < 1.5x absolute, or bottom quartile) ... 2
  Operating margin in bottom quartile of sector ........................ 2
  1-yr / 3-yr total return in bottom quartile of sector ................ 1 each
  ROA bottom quartile / shrinking revenue / bloated SG&A /
  cash-heavy / under-levered ........................................... 1 each
Event accelerants (recent filings/news), text-confirmed where it matters:
  Confirmed CEO/exec departure ......................................... 2
  Confirmed earnings miss / guidance cut ............................... 2
  Impairment / write-down .............................................. 2
  Layoffs / restructuring .............................................. 1
  Leadership change (routine appointment/board) ........................ 1
  Negative activist headline ........................................... 1
  Recent results (no clear miss) ....................................... 0  (note only)
Each triggered signal produces an EVIDENCE record (value, the underlying SEC math,
fiscal period, peer context with sample size, source, and a link to the exact
filing), stored as JSON on the score so the detail view can prove why each tag fired.
"""
import json
import os
import re
from datetime import datetime
from . import config, database, pitch, activists
MIN_PEERS = 5
# 1-yr stock return must lag the S&P 500 by at least this much (in return terms) to flag.
TSR_LAG_1Y = -0.15
# 3-yr CUMULATIVE return must lag the S&P by at least this much (30 pts) to flag a
# structural, multi-year underperformer (a bigger bar than the 1-yr gate, by design).
TSR_LAG_3Y = -0.30
# We STORE any company that shows at least this many signal points (plus all active
# situations), then RANK them by the 0-92 vulnerability index and surface the top of the
# list on the dashboard. This is deliberately low so the leads table, spotlight, and
# "new & rising" always have real depth -- the absolute count isn't a quality bar, the
# ranking is. (Previously the much higher config.SCORE_THRESHOLD let only a handful
# through, which made the dashboard look empty even though the data was there.)
LEAD_FLOOR = 2
STRUCT_POINTS = {"cheap_abs": 2, "cheap_pb": 2, "cheap_ev_ebitda": 2, "low_margin": 2,
                 "weak_tsr_1y": 1, "weak_tsr_3y": 1, "low_roa": 1, "weak_growth": 1,
                 "high_sga": 1, "cash_hoard": 1, "underlevered": 1, "high_goodwill": 1,
                 "gov_classified": 1, "gov_poison": 1, "gov_dual": 0,
                 "insider_selling": 1, "insider_buying": 0,
                 "weak_vote_support": 1, "overpaid_ceo": 2, "exec_reaction_drop": 2,
                 "lags_own_peers": 2, "strategic_review": 3, "activist_holder": 4}
EVENT_POINTS = {"ceo_departure": 2, "earnings_miss": 2, "impairment": 2,
                "restatement": 2, "layoffs": 1, "leadership_change": 1,
                "results_update": 0, "news_negative": 1}
POINTS = {**STRUCT_POINTS, **EVENT_POINTS}
# The 0-100 number is an ABSOLUTE "activist-target profile" index, NOT a probability of
# a campaign. Each triggered signal contributes its point weight scaled by *how severe*
# it is (0..1) — how cheap, how deep in the peer tail, how far returns lag. VULN_SCALE is
# the severity-weighted point total that maps to the top of the range.
#
# CALIBRATION (June 2026): VULN_SCALE was 10.0, which combined with a hard 92 cap pegged
# every multi-signal lead at the same "92 / Severe" — the index stopped discriminating at
# the top. We raised the scale so realistic strong leads land in the 70s-80s and lighter
# leads spread down into the 30s-50s, and replaced the hard cap with a SMOOTH soft-cap
# (see _soft_cap) so even the hottest names keep an order instead of clipping to one
# number. This is the main knob: if the live board reads too low, lower VULN_SCALE; too
# high / clustered, raise it. (Starting point chosen so a genuinely strong multi-signal
# lead lands in low-Severe ~75-85, mid leads in High/Elevated, light leads Moderate —
# eyeball the live board after the first run and nudge this one number if needed.)
# Lowered 16 -> 14 alongside the cluster caps below: those caps reduce severity totals for
# value-heavy names, so a slightly smaller scale keeps genuine leads in High/low-Severe.
VULN_SCALE = 14.0
# Above this raw value the score compresses smoothly toward VULN_MAX instead of clipping.
VULN_KNEE = 80.0
VULN_MAX = 92
# Minimum rating for a name to appear on the proactive "Companies to pitch" board. A
# Moderate match (< 25) barely fits the activist-target profile, so showing it as a pitch
# target just dilutes the list. 25 = the Elevated band floor; raise toward 50 for a tighter,
# High-and-above board. (Active situations ignore this — they're tracked, not pitched.)
MIN_LEAD_VULN = int(os.getenv("MIN_LEAD_VULN", "25"))
# Founder / super-voting control (gov_dual) makes an activist campaign rarely winnable, so
# such a name is a poor *actionable* lead even when its governance is genuinely bad. We
# DISCOUNT the score (rather than adding points) when a dual-class / super-voting structure
# is present, demoting it down the board while keeping the flag visible as context. NOTE:
# this only fires where we've parsed the proxy AND detected the dual-class structure;
# sharpening that detection (and using actual voting %) is a follow-up.
DUAL_CLASS_DISCOUNT = 0.6
# Known founder / family / super-voting controlled names where an activist effectively
# cannot win a vote. We discount these even if the proxy parser hasn't flagged the
# dual-class structure yet (a stopgap until that detection is sharper). Tickers; extend
# via the FOUNDER_CONTROLLED env var (comma-separated).
FOUNDER_CONTROLLED = set(filter(None, os.getenv(
    "FOUNDER_CONTROLLED",
    "COIN,IAC,GOOGL,GOOG,META,FOXA,FOX,NWSA,NWS,PARA,PARAA,DELL,F,NKE,UAA,UA,EL"
).replace(" ", "").upper().split(",")))
# Deep cash-burn gate: a name losing money heavily on operations — a clinical/platform
# biotech burning R&D, say — isn't a fixable-margin value target; its deeply negative
# margin/ROA reflect the business model, not an inefficiency an activist can unlock, so we
# discount it. This is deliberately NOT triggered by a merely thin or slightly-negative
# margin: a bloated-SG&A, near-breakeven grower (e.g. Inspire, whose SG&A is ~74% of revenue)
# IS a legitimate margin-expansion target and must stay on the board.
DEEP_BURN_MARGIN = -0.25    # operating margin at/below this = structural cash burn
BURN_DISCOUNT = 0.6         # score multiplier for deep cash-burners
# "Fallen quality / strategic alternatives" — a viable business whose stock has de-rated
# sharply. The activist thesis here is a sale / take-private / brand reset (Lululemon,
# Wendy's, Bio-Techne), driven by the drawdown itself — NOT by cheap-vs-peers margins, which
# is the whole class the value/distress signals structurally miss. Fires on a deep 1-yr
# decline in a company that is NOT a cash-burner (a fixable, acquirable business, not a
# falling knife). Profitable/quality names that just fell out of favor are exactly the point.
STRATEGIC_DROP = -0.30      # 1-yr price return at/below this = a sharp de-rating
DERATE_EXTREME = float(os.getenv("DERATE_EXTREME", "-0.50"))  # a 50%+ 1-yr collapse (4e)
# De-correlation: signals driven by the SAME underlying fact — a depressed share price —
# shouldn't each add full weight. Cap the combined severity-weighted contribution of the
# valuation cluster and the return cluster so one beaten-down price counts once (with size),
# not three or four times. The signals still all appear as evidence; only the SCORE is capped.
VALUE_CLUSTER = ("cheap_abs", "cheap_pb", "cheap_ev_ebitda")
RETURN_CLUSTER = ("weak_tsr_1y", "weak_tsr_3y")
VALUE_CLUSTER_CAP = 2.0     # severity-weighted points
RETURN_CLUSTER_CAP = 1.0
# Rating bands shown as the headline (defensible, no probability implied).
def vuln_band(v):
    if v is None:
        return "Unscored"
    if v >= 75:
        return "Severe"
    if v >= 50:
        return "High"
    if v >= 25:
        return "Elevated"
    return "Moderate"
def _soft_cap(raw, knee=VULN_KNEE, ceiling=VULN_MAX):
    """Linear up to `knee`, then compress smoothly toward `ceiling` (asymptotic, never
    clipping). Keeps ordering intact among the very strongest leads instead of mapping
    them all to the same capped number."""
    if raw <= knee:
        return raw
    over = raw - knee
    head = ceiling - knee
    return knee + head * over / (over + head)
LABELS = {
    "cheap_abs": "cheap (low price-to-book < 1.5x)",
    "cheap_pb": "cheap vs peers (low price-to-book)",
    "cheap_ev_ebitda": "cheap on EV/EBITDA vs peers",
    "high_goodwill": "goodwill-heavy balance sheet (acquisition risk)",
    "low_margin": "low operating margin vs peers",
    "weak_tsr_1y": "1-yr stock return lagging the market",
    "weak_tsr_3y": "weak 3-yr stock return vs market",
    "low_roa": "low return on assets vs peers",
    "weak_growth": "weak / below-peer revenue growth",
    "high_sga": "bloated SG&A vs peers",
    "cash_hoard": "cash-heavy balance sheet",
    "underlevered": "under-levered balance sheet",
    "ceo_departure": "recent CEO/exec departure",
    "earnings_miss": "recent earnings miss / guidance cut",
    "impairment": "recent impairment / write-down",
    "restatement": "financial restatement / non-reliance",
    "layoffs": "recent layoffs / restructuring",
    "leadership_change": "recent leadership change",
    "results_update": "recent results",
    "news_negative": "negative press headline",
    "gov_classified": "classified / staggered board",
    "gov_poison": "poison pill / rights plan",
    "gov_dual": "dual-class / super-voting stock",
    "insider_selling": "cluster of insider selling",
    "insider_buying": "insiders buying (confidence signal)",
    "weak_vote_support": "weak say-on-pay support",
    "overpaid_ceo": "CEO pay rising while the stock lags",
    "exec_reaction_drop": "stock dropped on a leadership-change 8-K",
    "lags_own_peers": "trails its self-selected proxy peer group",
    "strategic_review": "sharp de-rating — strategic-review / sale candidate",
    "activist_holder": "known activist already a holder (13F)",
}
# Insider activity (Form 4). insider_selling is a leading vulnerability signal;
# insider_buying is shown as a 0-point defense/confidence note.
INSIDER_KEYS = ("insider_selling", "insider_buying")
# Shareholder-vote discontent (8-K Item 5.07). Flag when say-on-pay support falls below
# this fraction -- well under the ~90%+ norm, a recognized pre-activism warning.
VOTE_KEYS = ("weak_vote_support",)
SAY_ON_PAY_FLAG = 0.75
# Floor for a believable say-on-pay result. A parsed value below this is a mis-parse, not a real
# vote (failed say-on-pay votes cluster ~40-60%; nothing lands near 0%). Guards against any
# already-stored garbage 0% rows so they never fire the signal, even before votes.py re-parses.
SAY_ON_PAY_MIN = 0.20
# CEO pay-for-performance (from DEF 14A Summary Comp Table). Fires when total CEO comp
# rose while the stock lagged — its own evidence card with the pay trajectory.
COMP_KEYS = ("overpaid_ceo",)
# Fire when comp rose at least this much over the SCT window (cumulative).
COMP_RISE_FLOOR = 0.05
# A pay "rise" this large alongside a recent CEO-change 8-K is almost certainly a new-CEO ramp
# (partial stub year -> first full year), not a real raise. Used as a scoring-side guard that
# catches the Signet 333% case even when the proxy was parsed/cached BEFORE compensation.py's
# ceo_transition flag existed (parse-time fixes don't touch cached rows; scoring guards do).
COMP_RAMP_SUSPECT = 1.0
# A company with large operating-lease liabilities (a mall-based retailer like Signet) is NOT
# truly "under-levered" even at zero funded debt — its leases ARE its leverage, so "lever up to
# return capital" is the wrong thesis. Suppress under-levered when lease liabilities exceed this
# share of assets (SIG: ~$1.1B leases / $5.73B ≈ 19%).
LEASE_HEAVY = 0.10


def _leveraged_recap(r):
    """True for a negative-equity / debt-funded-buyback capital structure (e.g. Domino's:
    book equity < 0, debt ~2.6x assets). Such a company has ALREADY returned all its capital
    and then some via a leveraged recap, so its cash is operating float — not "idle capital an
    activist would push to return." We suppress the cash-hoard read for these (same spirit as
    the bank/insurer and lease carve-outs)."""
    be = (r.get("raw") or {}).get("book_equity")
    da = r.get("debt_to_assets")
    return (be is not None and be <= 0) or (da is not None and da > 1.0)
# Exec-change stock reaction (the 1-day abnormal move vs S&P on a leadership-change 8-K).
REACTION_KEYS = ("exec_reaction_drop",)
# Fire when the stock fell at least this much MORE than the market on the announcement day.
REACTION_DROP_FLOOR = -0.03
# Self-selected proxy peer group (from the DEF 14A). lags_own_peers fires when the
# company's 1-yr TSR trails the MEDIAN of the peers it named itself, by PEER_LAG.
PEER_KEYS = ("lags_own_peers",)
PEER_MIN = 5            # need at least this many matched peers (with TSR) to judge
PEER_LAG = 0.05         # company must trail the peer-median 1-yr return by >= 5 pts
# Governance red flags (from DEF 14A). Boolean signals -> their own evidence cards.
GOV_KEYS = ("gov_classified", "gov_poison", "gov_dual")
GOV_META = {
    "gov_classified": "directors elected in staggered classes — entrenches the board against change",
    "gov_poison": "shareholder rights plan in place — blocks an activist from accumulating a stake",
    "gov_dual": "super-voting share structure — insiders control the vote",
}
# Structural signal -> (metric, direction, source label).
PCT_METRICS = {"operating_margin", "tsr_1y", "tsr_3y", "roa", "revenue_growth",
               "sga_pct", "cash_to_assets", "debt_to_assets", "goodwill_to_assets"}
STRUCT_META = {
    "cheap_abs": ("pb_ratio", "abs", "Alpha Vantage"),
    "cheap_pb": ("pb_ratio", "low", "Alpha Vantage"),
    "cheap_ev_ebitda": ("ev_ebitda", "low", "SEC XBRL"),
    "high_goodwill": ("goodwill_to_assets", "high", "SEC XBRL"),
    "low_margin": ("operating_margin", "low", "SEC XBRL"),
    "weak_tsr_1y": ("tsr_1y", "low", "Alpha Vantage"),
    "weak_tsr_3y": ("tsr_3y", "low", "Alpha Vantage"),
    "low_roa": ("roa", "low", "SEC XBRL"),
    "weak_growth": ("revenue_growth", "low", "SEC XBRL"),
    "high_sga": ("sga_pct", "high", "SEC XBRL"),
    "cash_hoard": ("cash_to_assets", "high", "SEC XBRL"),
    "underlevered": ("debt_to_assets", "low", "SEC XBRL"),
}
# How to show the underlying math: signal -> (numerator raw key, denominator raw key, template)
INPUTS_META = {
    "low_margin": ("operating_income", "revenue", "operating income {a} ÷ revenue {b}"),
    "high_sga": ("sga", "revenue", "SG&A {a} ÷ revenue {b}"),
    "cash_hoard": ("cash", "total_assets", "cash & ST investments {a} ÷ total assets {b}"),
    "underlevered": ("debt", "total_assets", "total debt {a} ÷ total assets {b}"),
    "high_goodwill": ("goodwill", "total_assets", "goodwill {a} ÷ total assets {b}"),
}
EVENT_SOURCE = {"ceo_departure": "SEC 8-K", "earnings_miss": "SEC 8-K",
                "impairment": "SEC 8-K", "restatement": "SEC 8-K Item 4.02",
                "layoffs": "SEC 8-K",
                "leadership_change": "SEC 8-K", "results_update": "SEC 8-K",
                "news_negative": "News"}
# A news headline only counts as an "active situation" when it BOTH (a) names an activist
# -- a known fund, or an explicit proxy-fight / "activist" cue -- AND (b) names the company
# itself. Requiring the company NAME (not just a loose ticker/keyword match) kills the
# misattribution that produced most false positives: a short-seller story landing on the
# wrong company, a generic "activist concerns" headline matching three names at once, or a
# two-letter ticker ("ON") matching the word "on" in an unrelated headline.
#
# How many days of news to scan for a named-activist headline (longer than the general
# scoring window, since a campaign stays "active" for months).
ACTIVIST_NEWS_WINDOW = 220
# 18-month agitation gate: a 13D / contested-proxy FILING only counts as a live active
# situation when it was filed within this window. Older filings are stale campaigns and
# drop off the page (a partner can re-add by hand). Kept in sync with activist.WINDOW_DAYS.
AGITATION_MAX_DAYS = 548   # ~18 months


def _within_days(date_str, days):
    """True if an ISO date string (YYYY-MM-DD, or a longer ISO timestamp) is within `days`
    of today. Missing/unparseable dates return False (can't prove recency -> don't count)."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - d).days <= days
# Known activist funds + individuals — the single shared list (funds, the FGS "Recurring
# Activist Investor List", and known individuals) lives in activists.py so the filer gate,
# the news auto-routing here, and news.py capture all stay in sync. Substring match on the
# lowercased headline.
KNOWN_FUNDS = activists.NEWS_TERMS
# Explicit campaign cues (no fund named, but unambiguous activist language). We keep
# these tight: weak phrases like "director nominees" appear in routine *defensive* proxy
# news (e.g. "ISS supports ALL of management's director nominees"), so they're excluded.
PROXY_CUES = [
    "proxy fight", "proxy contest", "proxy battle", "contested proxy",
    "dissident", "nominate directors",
    "activist investor", "activist stake", "activist campaign", "activist",
]
# Pretty labels for funds (whatever matched first wins).
_FUND_DISPLAY = {
    "icahn": "Icahn", "carl icahn": "Carl Icahn", "valueact": "ValueAct",
    "value act": "ValueAct", "jana": "JANA Partners", "jana partners": "JANA Partners",
    "h partners": "H Partners", "d.e. shaw": "D.E. Shaw", "engine no": "Engine No. 1",
    "land & buildings": "Land & Buildings", "land and buildings": "Land & Buildings",
    "pershing square": "Pershing Square", "ackman": "Pershing Square (Ackman)",
    "nelson peltz": "Trian (Peltz)", "trian": "Trian", "starboard": "Starboard",
    "starboard value": "Starboard", "lynrock lake": "Lynrock Lake", "lynrock": "Lynrock Lake",
    "browning west": "Browning West", "ananym": "Ananym Capital", "caligan": "Caligan Partners",
}
# Corporate suffixes stripped from a company name to find its distinctive core.
_NAME_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company",
                "companies", "holdings", "holding", "group", "plc", "ltd", "limited",
                "lp", "llc", "sa", "ag", "nv", "class"}
# Tokens too generic to identify a company on their own. These are ordinary English
# words (directions, sectors, filler adjectives) that routinely appear in UNRELATED
# headlines -- so a single one of them must NEVER confirm a match (that's what let
# "northern" match a Northern Star story, or "independent" match an ISS press release).
# A distinctive proper noun like "Ashland" or "Tripadvisor" is not on this list, so it
# still confirms; and the full multi-word company core always confirms regardless.
_NAME_STOP = _NAME_SUFFIX | {
    "the", "and", "for", "of",
    # directional / geographic
    "north", "northern", "south", "southern", "east", "eastern", "west", "western",
    "central", "pacific", "atlantic", "gulf", "midwest", "american", "america",
    "national", "international", "intl", "global", "worldwide", "continental",
    "united", "states", "canadian", "european", "western", "midwestern",
    # generic descriptors
    "independent", "standard", "general", "premier", "premiere", "allied", "alliance",
    "consolidated", "integrated", "advanced", "dynamic", "unified", "prime", "select",
    "public", "private", "federal", "mutual", "community", "first", "new", "modern",
    "next", "true", "core", "peak", "apex", "popular", "citizens", "peoples",
    "sovereign", "liberty", "freedom", "heritage", "pioneer", "frontier", "summit",
    "keystone", "cornerstone", "gateway", "horizon", "interstate", "regional",
    # generic words that recur in activist/deal headlines -> a company key like "strategic" was
    # matching "Strategic Review" and cross-tagging (Strategic Education <- a Compass Diversified item).
    "strategic", "diversified", "consumer", "opportunity", "growth", "development", "value",
    # sector / industry words
    "financial", "finance", "capital", "bancorp", "bancshares", "bank", "banks",
    "banking", "energy", "power", "electric", "gas", "oil", "petroleum", "water",
    "utilities", "utility", "health", "healthcare", "medical", "pharma",
    "pharmaceutical", "pharmaceuticals", "therapeutics", "biosciences",
    "technology", "technologies", "tech", "digital", "data", "cloud", "cyber",
    "software", "systems", "networks", "network", "communications", "telecom",
    "media", "entertainment", "properties", "property", "realty", "estate", "homes",
    "residential", "hotels", "resorts", "retail", "stores", "foods", "food",
    "beverage", "brands", "products", "goods", "materials", "chemical", "chemicals",
    "steel", "metals", "mining", "minerals", "motors", "auto", "automotive",
    "airlines", "airways", "transport", "transportation", "logistics", "express",
    "freight", "shipping", "marine", "defense", "aerospace", "industrial",
    "industries", "manufacturing", "services", "service", "solutions", "holdings",
    "group", "partners", "resources", "enterprises", "ventures", "insurance",
    "securities", "investments", "investment", "management", "trust", "corp",
}
def _name_core_tokens(name):
    toks = re.findall(r"[a-z0-9&.-]+", (name or "").lower())
    while toks and toks[-1] in _NAME_SUFFIX:
        toks.pop()
    return toks
def _company_keys(name):
    """(core_phrase, {keys}) used to confirm a headline really names this company.
    Keys are the full multi-word core (high precision) plus distinctive single tokens."""
    toks = _name_core_tokens(name)
    if not toks:
        return "", set()
    core = " ".join(toks)
    keys = set()
    if len(toks) >= 2:
        keys.add(core)
    for t in toks:
        tt = t.strip(".-")
        if tt in _NAME_STOP:
            continue
        if len(tt) >= 6 or (len(toks) == 1 and len(tt) >= 4):
            keys.add(tt)
    return core, keys
def _headline_about_company(headline, keys):
    if not keys:
        return False
    h = " " + re.sub(r"[^a-z0-9&.-]+", " ", (headline or "").lower()) + " "
    for k in keys:
        if " " in k:                       # multi-word core: plain substring is safe
            if k in h:
                return True
        elif re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", h):
            return True
    return False
def _activist_cue(headline):
    """Return a display label if the headline carries an activist cue, else None."""
    t = " " + (headline or "").lower() + " "
    for f in KNOWN_FUNDS:
        if f in t:
            return _FUND_DISPLAY.get(f, f.title())
    for c in PROXY_CUES:
        if c in t:
            return "Proxy contest" if "proxy" in c or "nominee" in c or "dissident" in c \
                or "nominate" in c else "Activist campaign"
    return None
# Opinion / listicle / analyst headlines are commentary, not evidence of a real campaign — they
# recur on any name "activists might target". Reject them from the Reported activist tier.
_OPINION_RE = re.compile(
    r"buy,?\s+sell,?\s+or\s+hold|\bis\b[^.]*\ba\s+buy\b|should you (?:buy|sell)|"
    r"\ba look at\b|here'?s why|\b\d+\s+reasons\b|reasons to (?:buy|sell)|"
    r"undervaluation|draws activist attention|draws activist interest|"
    r"price target|analyst|upgrade|downgrade|\bwhy\b[^.]*\bis\b[^.]*\b(?:down|up|falling|rising|sinking|soaring)\b|"
    r"time to buy|worth buying|stock (?:forecast|prediction|to buy|pick)|dividend",
    re.I)


def _activist_news_hit(ticker, name):
    """Most recent news headline that names BOTH an activist and this company, or None. Opinion /
    listicle / analyst pieces are excluded — they're commentary, not evidence of a live campaign."""
    _, keys = _company_keys(name)
    if not keys:
        return None
    for n in database.news_for_ticker_in_window(ticker, ACTIVIST_NEWS_WINDOW):
        hl = n.get("headline")
        who = _activist_cue(hl)
        if who and _headline_about_company(hl, keys) and not _OPINION_RE.search(hl or ""):
            return {"title": n["headline"], "url": n["url"], "who": who,
                    "date": (n.get("published_at") or "")[:10]}
    return None
def _pad_cik(cik):
    return str(cik).lstrip("0").zfill(10) if cik else cik
def _quantiles(values):
    vals = sorted(v for v in values if v is not None)
    if len(vals) < MIN_PEERS:
        return None, None, len(vals)
    def pct(p):
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[idx]
    return pct(0.25), pct(0.75), len(vals)
def _pct_lo_hi(values):
    """5th / 95th percentile of a metric across a sector (tail anchors for severity)."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < MIN_PEERS:
        return None, None
    def pct(p):
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[idx]
    return pct(0.05), pct(0.95)
def _clamp(x):
    return max(0.0, min(1.0, x))
def _depth_low(metric, r, t, e):
    """How deep below the bottom-quartile cutoff toward the sector floor (0..1)."""
    v = r.get(metric)
    q1, _, _ = t.get(metric, (None, None, 0))
    lo, _ = e.get(metric, (None, None))
    if v is None or q1 is None:
        return 0.5
    if lo is None or q1 <= lo:
        return 0.6
    return _clamp((q1 - v) / (q1 - lo))
def _depth_high(metric, r, t, e):
    """How far above the top-quartile cutoff toward the sector ceiling (0..1)."""
    v = r.get(metric)
    _, q3, _ = t.get(metric, (None, None, 0))
    _, hi = e.get(metric, (None, None))
    if v is None or q3 is None:
        return 0.5
    if hi is None or hi <= q3:
        return 0.6
    return _clamp((v - q3) / (hi - q3))
def _severity(key, r, t, e):
    """Magnitude (0..1) of a triggered signal. Events/governance are binary (1.0);
    peer-relative and valuation/return signals scale by how extreme they are."""
    if key == "cheap_abs":
        pb = r.get("pb_ratio")
        return _clamp((1.5 - pb) / 1.5) if pb is not None else 0.5
    if key == "cheap_pb":
        return _depth_low("pb_ratio", r, t, e)
    if key == "cheap_ev_ebitda":
        return _depth_low("ev_ebitda", r, t, e)
    if key == "high_goodwill":
        return _depth_high("goodwill_to_assets", r, t, e)
    if key == "weak_tsr_1y":
        ret, bench = r.get("tsr_1y"), r.get("_spy_1y")
        if ret is None or bench is None:
            return 0.5
        gap = bench - ret  # positive = underperformance vs the index
        return _clamp((gap - 0.15) / 0.50)  # 15pts behind -> 0, 65pts behind -> 1
    if key == "weak_tsr_3y":
        ret, bench = r.get("tsr_3y"), r.get("_spy_3y")
        if ret is None or bench is None:
            return 0.5
        gap = bench - ret
        return _clamp((gap - 0.30) / 1.20)  # 30pts behind -> 0, 150pts behind -> 1
    if key == "low_margin":
        return _depth_low("operating_margin", r, t, e)
    if key == "low_roa":
        return _depth_low("roa", r, t, e)
    if key == "weak_growth":
        return _depth_low("revenue_growth", r, t, e)
    if key == "high_sga":
        return _depth_high("sga_pct", r, t, e)
    if key == "cash_hoard":
        return _depth_high("cash_to_assets", r, t, e)
    if key == "underlevered":
        return _depth_low("debt_to_assets", r, t, e)
    if key == "insider_selling":
        ins = r.get("_insider") or {}
        ns = ins.get("n_sellers") or 0
        net = (ins.get("sell_value") or 0) - (ins.get("buy_value") or 0)
        mcap = r.get("market_cap")
        sev = 0.5 + 0.15 * max(0, ns - 2)            # more distinct sellers = worse
        if mcap and net > 0:
            sev += min(0.4, (net / mcap) * 40.0)     # ~1% of market cap sold -> +0.4
        return _clamp(sev)
    if key == "weak_vote_support":
        sop = (r.get("_votes") or {}).get("say_on_pay")
        if sop is None:
            return 0.5
        return _clamp((0.75 - sop) / 0.25)           # 75% -> 0, 50% or worse -> 1
    if key == "overpaid_ceo":
        c = r.get("_comp") or {}
        pct = c.get("pct_change") or 0.0
        tsr = r.get("tsr_1y") or 0.0
        # bigger raise + worse stock = sharper disconnect
        return _clamp(0.4 + min(0.4, pct) + min(0.2, max(0.0, -tsr)))
    if key == "exec_reaction_drop":
        abn = (r.get("_reaction") or {}).get("abnormal")
        if abn is None:
            return 0.5
        return _clamp((-abn - 0.03) / 0.12)          # -3% -> 0, -15% -> 1
    if key == "strategic_review":
        ret = r.get("tsr_1y")
        if ret is None:
            return 0.5
        return _clamp((-ret - 0.30) / 0.40 + 0.3)    # -30% -> ~0.3, -55% -> ~0.9, deeper -> 1
    if key == "activist_holder":
        # Scale by the strongest holder's conviction/ownership: a big concentrated slug or a
        # ~toehold-approaching-5% stake is a sharper tell than a 2%/1% threshold position.
        h = (r.get("_holders") or [{}])[0]
        w = h.get("weight_in_fund") or 0.0
        o = h.get("ownership_pct") or 0.0
        return _clamp(0.55 + min(0.3, w * 6.0) + min(0.15, o * 3.0))
    if key == "lags_own_peers":
        pa = r.get("_peers") or {}
        med = (pa.get("median") or {}).get("tsr_1y")
        st = (pa.get("self") or {}).get("tsr_1y")
        if med is None or st is None:
            return 0.5
        return _clamp((med - st - 0.05) / 0.40)       # 5pts behind -> 0, 45pts behind -> 1
    return 1.0  # governance flags + insider_buying + event accelerants
# Financials-tab peer context: each metric vs its sector, with a verdict that says what
# the number MEANS for a pitch -- a vulnerability to attack ("bad"), idle capital / a cheap
# entry an activist could exploit ("opp"), or in line ("mid"). Drives the meaning bars.
_FIN_METRICS = [
    ("pb_ratio", "Price / book", "opp_low"),
    ("ev_ebitda", "EV / EBITDA", "opp_low"),
    ("goodwill_to_assets", "Goodwill / assets", "bad_high"),
    ("operating_margin", "Operating margin", "bad_low"),
    ("roa", "Return on assets", "bad_low"),
    ("revenue_growth", "Revenue growth", "bad_low"),
    ("sga_pct", "SG&A % of revenue", "bad_high"),
    ("cash_to_assets", "Cash / assets", "opp_high"),
    ("debt_to_assets", "Debt / assets", "opp_low"),
]
def _fin_context(r, t, e):
    out = []
    # Mirror the trigger guards so the Financials tab matches the evidence cards (don't paint a
    # red "vulnerability" / gold "opportunity" the scorer deliberately suppressed): financials
    # (SIC 60-64), strong multi-year outperformers, and the profitability rule (margin/ROA/SG&A
    # levers only apply to a profitable company).
    _fin = (r.get("sector") or "") in ("60", "61", "62", "63", "64")
    _g3 = ((r.get("tsr_3y") - r.get("_spy_3y")) if (r.get("tsr_3y") is not None and r.get("_spy_3y") is not None) else None)
    _g5 = ((r.get("tsr_5y") - r.get("_spy_5y")) if (r.get("tsr_5y") is not None and r.get("_spy_5y") is not None) else None)
    _outperf = (_g3 is not None and _g3 >= 0.25) or (_g5 is not None and _g5 >= 0.40 and _g3 is not None and _g3 >= 0)
    _om_pos = (r.get("operating_margin") or 0) > 0
    _bs = r.get("raw") or {}
    _ol, _ta = _bs.get("operating_lease"), _bs.get("total_assets")
    _lease_heavy = _ol is not None and _ta and _ta > 0 and (_ol / _ta) >= LEASE_HEAVY
    _suppress = {
        "operating_margin": _fin or _outperf or not _om_pos,
        "roa": (r.get("roa") or 0) <= 0,
        "sga_pct": _fin or _outperf or not _om_pos,
        "cash_to_assets": _fin or _outperf or _leveraged_recap(r),
        "debt_to_assets": _fin or _outperf or _lease_heavy,
        "ev_ebitda": _fin,
    }
    for key, label, rule in _FIN_METRICS:
        v = r.get(key)
        if v is None:
            continue
        q1, q3, n = t.get(key, (None, None, 0))
        lo, hi = e.get(key, (None, None))
        pct = None
        if lo is not None and hi is not None and hi > lo:
            pct = _clamp((v - lo) / (hi - lo))
        verdict, cutoff = "mid", None
        if rule == "bad_low":
            cutoff = q1
            if q1 is not None and v <= q1:
                verdict = "bad"
        elif rule == "bad_high":
            cutoff = q3
            if q3 is not None and v >= q3:
                verdict = "bad"
        elif rule == "opp_low":
            cutoff = q1
            if q1 is not None and v <= q1:
                verdict = "opp"
        elif rule == "opp_high":
            cutoff = q3
            if q3 is not None and v >= q3:
                verdict = "opp"
        # If the matching scored signal was suppressed (financial / outperformer / not
        # profitable), don't show this metric as a red/gold activist lever — keep it "in line".
        if verdict in ("bad", "opp") and _suppress.get(key):
            verdict = "mid"
        out.append({"key": key, "label": label, "value": v, "cutoff": cutoff,
                    "pct": pct, "verdict": verdict, "n": n})
    return out
def _is_cash_burner(r):
    """Deeply unprofitable on operations -> a structural cash burner (clinical/platform
    biotech, etc.) where 'low margin / low ROA' reflect the business model, not a fixable
    inefficiency. Discounted. A merely thin or slightly-negative margin is NOT caught — that
    is a genuine margin-expansion target and stays on the board. A company that is GAAP-
    profitable over the full year is never a burner (its negative period figure is a charge)."""
    if r.get("_gaap_profitable"):
        return False
    om = r.get("operating_margin")
    roa = r.get("roa")
    if om is not None and om <= DEEP_BURN_MARGIN:
        return True
    # Fallback: some clinical-stage names report no clean operating income, so margin is
    # missing — a deeply negative ROA still identifies the cash burn.
    if om is None and roa is not None and roa <= DEEP_BURN_MARGIN:
        return True
    return False


def _vuln_score(trig, r, t, e):
    """0-VULN_MAX activist-target-profile index from the severity-weighted signal total.
    Correlated value/return signals are capped per-cluster (one depressed price counts once);
    linear up to a knee, then a smooth soft-cap so the strongest leads keep an order instead
    of clipping to the ceiling. Founder/super-voting control and growth-stage (unprofitable +
    growing) names are discounted, since neither is a clean actionable value target."""
    contrib = {k: POINTS.get(k, 0) * _severity(k, r, t, e) for k in trig}
    clustered = set(VALUE_CLUSTER) | set(RETURN_CLUSTER)
    sev_total = sum(v for k, v in contrib.items() if k not in clustered)
    sev_total += min(VALUE_CLUSTER_CAP, sum(contrib.get(k, 0) for k in VALUE_CLUSTER))
    sev_total += min(RETURN_CLUSTER_CAP, sum(contrib.get(k, 0) for k in RETURN_CLUSTER))
    raw = 100 * sev_total / VULN_SCALE
    v = _soft_cap(raw)
    tk = (r.get("ticker") or "").upper()
    if "gov_dual" in trig or tk in FOUNDER_CONTROLLED:
        v *= DUAL_CLASS_DISCOUNT
    if _is_cash_burner(r):
        v *= BURN_DISCOUNT
    return int(round(v))
def _fmt_metric(metric, v):
    if v is None:
        return "n/a"
    if metric == "pb_ratio":
        return f"{v:.2f}x"
    if metric == "ev_ebitda":
        return f"{v:.1f}x"
    if metric in PCT_METRICS:
        return f"{v * 100:.1f}%"
    return f"{v:.2f}"
def _money(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"
def _source_url(cik, accn):
    try:
        ci = int(cik)
    except (ValueError, TypeError):
        ci = cik
    if accn:
        nod = str(accn).replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{ci}/{nod}/{accn}-index.htm"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ci}&type=10-K"
def _period_label(raw):
    lbl = raw.get("period_label")
    if lbl:
        return lbl
    fy = raw.get("period_fy")
    end = raw.get("period_end")
    if fy:
        return f"FY{fy}"
    if end:
        return f"FY{str(end)[:4]}"
    return ""
def _tsr_evidence(key, r):
    if key == "weak_tsr_1y":
        metric, bench, span = "tsr_1y", r.get("_spy_1y"), "past year"
        src = "Finnhub (price return, excl. dividends)"
        period = "trailing 1 yr"
    else:
        metric, bench, span = "tsr_3y", r.get("_spy_3y"), "past 3 years"
        src = "Twelve Data (price return, monthly closes, excl. dividends)"
        period = "trailing 3 yr"
    ret = r.get(metric)
    if bench is not None and ret is not None:
        excess = (ret - bench) * 100
        ctx = (f"underperformed the S&P 500 by {abs(excess):.0f} pts over the {span} "
               f"(stock {ret * 100:+.0f}% vs index {bench * 100:+.0f}%)")
        # add the 5-yr figure as extra color when this is the 3-yr card
        if key == "weak_tsr_3y" and r.get("tsr_5y") is not None:
            ctx += f"; {r['tsr_5y'] * 100:+.0f}% over 5 years"
    else:
        ctx = f"lagged the broader market over the {span}"
    return {"key": key, "label": LABELS.get(key, key), "value": _fmt_metric(metric, ret),
            "context": ctx, "inputs": "", "period": period, "source": src, "url": None}
def _gov_evidence(key, r):
    return {"key": key, "label": LABELS.get(key, key), "value": "",
            "context": GOV_META.get(key, ""), "inputs": "",
            "period": (f"proxy {r.get('_gov_date')}" if r.get("_gov_date") else "DEF 14A"),
            "source": "SEC DEF 14A", "url": r.get("_gov_url")}
def _insider_evidence(key, r):
    ins = r.get("_insider") or {}
    buy, sell = ins.get("buy_value") or 0, ins.get("sell_value") or 0
    nb, ns = ins.get("n_buyers") or 0, ins.get("n_sellers") or 0
    win = ins.get("window_days") or 120
    if key == "insider_selling":
        net = sell - buy
        ctx = (f"{ns} insider{'s' if ns != 1 else ''} sold a net {_money(net)} of stock on "
               f"the open market over the past {win} days — a crack in insider confidence")
        val = "-" + _money(net)
    else:
        net = buy - sell
        ctx = (f"{nb} insider{'s' if nb != 1 else ''} bought a net {_money(net)} of stock on "
               f"the open market over the past {win} days — insiders are aligned (a defense signal)")
        val = "+" + _money(net)
    return {"key": key, "label": LABELS.get(key, key), "value": val,
            "context": ctx, "inputs": "", "period": f"trailing {win} days",
            "source": "SEC Form 4", "url": ins.get("top_url")}
def _vote_evidence(key, r):
    v = r.get("_votes") or {}
    sop = v.get("say_on_pay")
    mtg = v.get("meeting_date")
    pct = f"{sop * 100:.0f}% for" if sop is not None else ""
    ctx = (f"only {sop * 100:.0f}% of votes backed executive pay at the last annual "
           f"meeting{(' (' + mtg + ')') if mtg else ''} — shareholder discontent that "
           f"often precedes an activist campaign") if sop is not None else ""
    return {"key": key, "label": LABELS.get(key, key), "value": pct, "context": ctx,
            "inputs": "", "period": (f"meeting {mtg}" if mtg else "annual meeting"),
            "source": "SEC 8-K Item 5.07", "url": v.get("url")}
def _comp_evidence(key, r):
    c = r.get("_comp") or {}
    pct = c.get("pct_change")
    ey, ly = c.get("earliest_year"), c.get("latest_year")
    et, lt = c.get("earliest_total"), c.get("latest_total")
    tsr = r.get("tsr_1y")
    val = (f"+{pct * 100:.0f}%" if pct is not None else "")
    move = ""
    if tsr is not None:
        move = (f", while the stock returned {tsr * 100:.0f}% over the past year"
                if tsr < 0 else f", while the stock trailed the market")
    ctx = (f"CEO total pay rose {pct * 100:.0f}% — from {_money(et)} in {ey} to "
           f"{_money(lt)} in {ly}{move} — a pay-for-performance disconnect activists target"
           if pct is not None else "CEO pay rose while shareholders lagged")
    inputs = (f"Summary Compensation Table 'Total' column: {ey} {_money(et)} → {ly} {_money(lt)}")
    return {"key": key, "label": LABELS.get(key, key), "value": val, "context": ctx,
            "inputs": inputs, "period": (f"proxy {r.get('_gov_date')}" if r.get("_gov_date") else "DEF 14A"),
            "source": "SEC DEF 14A", "url": r.get("_gov_url")}
def _reaction_evidence(key, r):
    rc = r.get("_reaction") or {}
    move, bench, abn = rc.get("move"), rc.get("bench_move"), rc.get("abnormal")
    ed = rc.get("event_date") or rc.get("filed_at")
    val = (f"{move * 100:+.0f}%" if move is not None else "")
    benchtxt = (f" while the S&P moved {bench * 100:+.1f}%" if bench is not None else "")
    ctx = (f"the stock moved {move * 100:+.1f}% the day the leadership-change 8-K was "
           f"filed{benchtxt} — about {abs(abn) * 100:.0f} points of company-specific "
           f"downside, signalling the market lost confidence in the transition"
           if move is not None and abn is not None else
           "the stock sold off on a leadership-change filing")
    return {"key": key, "label": LABELS.get(key, key), "value": val, "context": ctx,
            "inputs": (f"close on {ed} vs prior trading day, less the S&P's same-day move"),
            "period": (f"8-K filed {rc.get('filed_at')}" if rc.get("filed_at") else "recent 8-K"),
            "source": "SEC 8-K + Twelve Data", "url": rc.get("url")}
def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0
# Minimum number of names (company + peers) that must have 1-yr TSR before we report a peer
# rank — below this a "rank" is just the company alone ("1 of 1") and reads as false precision.
PEER_RANK_MIN = 3


def _peer_analysis(r, peer_ciks, rec_by_cik):
    """Build the company-vs-self-selected-peers comparison (for the Peer Analysis tab and
    the lags_own_peers signal). Only peers we also cover (have a rec for) are included."""
    peers = []
    for pc in peer_ciks:
        pr = rec_by_cik.get(pc)
        if not pr or pr.get("cik") == r.get("cik"):
            continue
        peers.append({"ticker": pr.get("ticker"), "name": pr.get("name"),
                      "tsr_1y": pr.get("tsr_1y"), "operating_margin": pr.get("operating_margin"),
                      "pb_ratio": pr.get("pb_ratio"), "ev_ebitda": pr.get("ev_ebitda")})
    if not peers:
        return None
    med = {m: _median([p[m] for p in peers])
           for m in ("tsr_1y", "operating_margin", "pb_ratio", "ev_ebitda")}
    self_obj = {"ticker": r.get("ticker"), "name": r.get("name"), "tsr_1y": r.get("tsr_1y"),
                "operating_margin": r.get("operating_margin"), "pb_ratio": r.get("pb_ratio"),
                "ev_ebitda": r.get("ev_ebitda")}
    # rank the company by 1-yr TSR within {self + peers} (1 = best return)
    rated = [(self_obj["ticker"], self_obj["tsr_1y"])] + [(p["ticker"], p["tsr_1y"]) for p in peers]
    rated = [(tk, v) for tk, v in rated if v is not None]
    rated.sort(key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (tk, _v) in enumerate(rated) if tk == self_obj["ticker"]), None)
    n_tsr = len([p for p in peers if p["tsr_1y"] is not None])
    # A rank is only meaningful when enough of the named peers actually have return data. Proxy
    # peers are often outside our enriched set, so with just the company itself the rank reads a
    # misleading "1 of 1". Below PEER_RANK_MIN ranked names, drop the rank so the UI shows the
    # plain "compensation peer group … versus the metrics we track" copy instead of a false rank.
    rank_of = len(rated)
    if rank_of < PEER_RANK_MIN:
        rank, rank_of = None, None
    return {"self": self_obj, "peers": peers, "median": med, "n": len(peers),
            "n_tsr": n_tsr, "rank": rank, "rank_of": rank_of}
def _peer_evidence(key, r):
    pa = r.get("_peers") or {}
    med = (pa.get("median") or {}).get("tsr_1y")
    st = (pa.get("self") or {}).get("tsr_1y")
    rank, rof, n = pa.get("rank"), pa.get("rank_of"), pa.get("n")
    parts = []
    if st is not None and med is not None:
        parts.append(f"1-yr return of {st * 100:+.0f}% vs the {med * 100:+.0f}% median of the "
                     f"{n} peers it named in its own proxy")
    if rank and rof:
        parts.append(f"ranks {rank} of {rof} in that group")
    ctx = " — ".join(parts) if parts else f"trails the {n}-company peer group it selected itself"
    return {"key": key, "label": LABELS.get(key, key),
            "value": (f"#{rank}/{rof}" if rank and rof else ""),
            "context": ctx,
            "inputs": "self-selected compensation peer group (DEF 14A) vs 1-yr total return",
            "period": "trailing 1 yr", "source": "SEC DEF 14A + Finnhub",
            "url": r.get("_gov_url")}
# 4e — separate a genuine falling knife from a fallen-but-viable de-rating.
def _structural_decline(r):
    """A de-rated name whose TOP LINE is actually shrinking — the business is contracting,
    not merely the multiple. Revenue is the cleanest 'falling knife?' tell: it's the hardest
    number to distort with a one-time charge, so an impairment-driven negative margin (e.g.
    SMPL, still growing revenue) is NOT mislabeled a structural decline. When true, the
    strategic-review framing flips from 'clean sale candidate' to 'possible structural
    decline — diligence required,' and the name loses its quality-floor exemption (a
    shrinking-and-cheap falling knife shouldn't be force-surfaced as a proactive lead)."""
    rg = r.get("revenue_growth")
    return rg is not None and rg < 0


def _forward_risk_derate(r):
    """A VERY sharp de-rating (>=50% in a year) on a still-growing, still-viable business:
    the multiple has collapsed while the fundamentals hold. That divergence almost always
    means the market is pricing a FORWARD risk the trailing financials don't yet show (a
    secular / AI-narrative overhang, a moat question, a guide-down) — the Intuit pattern
    (down ~63% but +16% revenue, healthy margins). We keep the name as a real sale candidate
    but attach a caveat so the pitch pressure-tests the thesis instead of treating the
    de-rating as a pure valuation gift."""
    t1 = r.get("tsr_1y")
    return (t1 is not None and t1 <= DERATE_EXTREME) and not _structural_decline(r)


def _impaired_fundamentals(r):
    """A de-rated name that posted a FULL-YEAR GAAP LOSS — usually a large goodwill impairment /
    write-down (Concentrix: $1.5B Webhelp goodwill impairment, ~$1.3B FY net loss). Unlike a single
    quarter dinged by a charge on top of a PROFITABLE year (which stays 'fallen-but-viable', e.g.
    SMPL), a loss for the WHOLE year means the fundamentals are impaired — not merely the multiple —
    so it is NOT a clean sale candidate. `annual_net_income` is the most-recent full-year net income."""
    ani = (r.get("raw") or {}).get("annual_net_income")
    return ani is not None and ani < 0


def _strategic_evidence(key, r):
    ret = r.get("tsr_1y")
    val = _fmt_metric("tsr_1y", ret) if ret is not None else ""
    drop = (f"down {abs(ret) * 100:.0f}% over the past year"
            if ret is not None else "sharply de-rated")
    lbl = LABELS.get(key, key)
    if _structural_decline(r):
        lbl = "sharp de-rating — possible structural decline (diligence)"
        ctx = (f"the stock is {drop}, but the top line is shrinking too — the business is "
               f"contracting, not just the multiple. Treat as a possible structural decline / "
               f"falling knife, not a clean turnaround: diligence the revenue trajectory before "
               f"pitching a sale or strategic-review angle.")
    elif _impaired_fundamentals(r):
        lbl = "sharp de-rating — impaired fundamentals (full-year GAAP loss)"
        ctx = (f"the stock is {drop}, but the company also posted a full-year GAAP loss (often a "
               f"large impairment or write-down) — the fundamentals are impaired, not merely the "
               f"multiple. This is NOT a clean fallen-but-viable sale candidate; diligence what "
               f"drove the loss (the write-down and the acquisition or segment behind it) before "
               f"pitching a valuation or strategic-review angle.")
    elif _forward_risk_derate(r):
        lbl = "sharp de-rating — sale candidate (thesis at risk)"
        ctx = (f"the stock is {drop} on a still-growing, still-viable business — the multiple "
               f"has collapsed while the fundamentals hold. A divergence this wide usually means "
               f"the market is pricing a forward risk the trailing numbers don't yet show (a "
               f"secular / narrative overhang or moat question). A fallen-but-viable sale "
               f"candidate — but pressure-test the thesis before pitching; don't assume the "
               f"de-rating is a pure valuation gift.")
    else:
        ctx = (f"the stock has de-rated sharply — {drop} — on a still-viable business; the "
               f"classic setup for a sale, take-private, or strategic review (a fixable, "
               f"acquirable brand, not a falling knife)"
               if ret is not None else
               "a sharp de-rating on a still-viable business — a strategic-alternatives setup")
    return {"key": key, "label": lbl, "value": val, "context": ctx,
            "inputs": "", "period": "trailing 1 yr",
            "source": "Finnhub (price return, excl. dividends)", "url": None}
# 13F activist-holder signal -------------------------------------------------
_OWNERSHIP_PCT_MIN = getattr(config, "OWNERSHIP_PCT_MIN", 0.01)   # >=1% of the company (strong)
_OWNERSHIP_SOFT = getattr(config, "OWNERSHIP_SOFT", 0.005)        # >=0.5% of the company (soft)
_FUND_WEIGHT_CONV = getattr(config, "FUND_WEIGHT_CONV", 0.05)     # + >=5% of the fund's book
_FUND_WEIGHT_FLOOR = getattr(config, "FUND_WEIGHT_FLOOR", 0.01)   # >=1% of the fund's book
_OWNERSHIP_MAX = getattr(config, "OWNERSHIP_MAX", 1.0)            # >100% = data error


def _holder_is_material(h):
    """A concentrated activist stake with real influence over the COMPANY: enough of the company
    (ownership) AND enough of the fund's own book (conviction — separates a real activist from a
    quant/multi-strat fund holding 1% of hundreds of names). >=1% of company AND >=1% of book, or
    >=0.5% of company when it's a concentrated >=5% of book. Ownership present, not a >100% error."""
    o = h.get("ownership_pct")
    w = h.get("weight_in_fund") or 0
    if o is None or o > _OWNERSHIP_MAX:
        return False
    if o >= _OWNERSHIP_PCT_MIN and w >= _FUND_WEIGHT_FLOOR:
        return True
    return o >= _OWNERSHIP_SOFT and w >= _FUND_WEIGHT_CONV


def _material_holders(holders):
    """Material holders, strongest conviction first (list already weight-sorted upstream)."""
    return [h for h in (holders or []) if _holder_is_material(h)]


def _fmt_holder(h):
    """'Elliott — 4.2% of its 13F book · ~1.8% of shares (Q4 2025)'."""
    fund = (h.get("fund") or "an activist").title()
    bits = []
    if h.get("weight_in_fund") is not None:
        bits.append(f"{h['weight_in_fund'] * 100:.1f}% of its 13F book")
    if h.get("ownership_pct") is not None:
        bits.append(f"~{h['ownership_pct'] * 100:.1f}% of shares outstanding")
    tail = " · ".join(bits) if bits else "a disclosed stake"
    q = f" ({h['quarter']})" if h.get("quarter") else ""
    return f"{fund} — {tail}{q}"


def _holder_evidence(key, r):
    hs = r.get("_holders") or []
    top = hs[0] if hs else {}
    names = ", ".join(sorted({(h.get("fund") or "").title() for h in hs if h.get("fund")}))
    ctx = (f"a known activist already holds a material stake here but has NOT yet filed a 13D or "
           f"launched a proxy fight — the early-warning window. {_fmt_holder(top)}."
           + (f" Other activist holders: {names}." if len(hs) > 1 else ""))
    url = None
    if top.get("fund_cik") and top.get("filed"):
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={top['fund_cik']}&type=13F-HR"
    return {"key": key, "label": LABELS.get(key, key), "value": "", "context": ctx,
            "inputs": "", "period": (top.get("quarter") or "") + " · 13F-HR",
            "source": "SEC EDGAR (Form 13F information table)", "url": url}


def _struct_evidence(key, r, t):
    if key in ("weak_tsr_1y", "weak_tsr_3y"):
        return _tsr_evidence(key, r)
    if key in GOV_KEYS:
        return _gov_evidence(key, r)
    metric, direction, source = STRUCT_META[key]
    v = r.get(metric)
    q1, q3, n = t.get(metric, (None, None, 0))
    raw = r.get("raw") or {}
    sector_label = raw.get("sector_desc") or "sector"
    period_override = None
    # peer context
    if key == "cheap_abs":
        ctx = "trades below 1.5x book value"
    elif key == "weak_growth" and v is not None and v < 0:
        ctx = "revenue is shrinking year over year"
    elif direction == "low":
        ctx = (f"bottom 25% of {n} {sector_label} peers · peer cutoff {_fmt_metric(metric, q1)}"
               if q1 is not None else f"bottom quartile of {sector_label} peers")
    elif direction == "high":
        ctx = (f"top 25% of {n} {sector_label} peers · peer cutoff {_fmt_metric(metric, q3)}"
               if q3 is not None else f"top quartile of {sector_label} peers")
    else:
        ctx = ""
    # underlying math
    inputs = ""
    if key in INPUTS_META:
        a_key, b_key, tmpl = INPUTS_META[key]
        a, b = raw.get(a_key), raw.get(b_key)
        if a is not None and b is not None:
            inputs = tmpl.format(a=_money(a), b=_money(b))
    elif key == "low_roa":
        ni = raw.get("net_income_ann")
        if ni is None:
            ni = raw.get("net_income")
        ta = raw.get("total_assets")
        if ni is not None and ta is not None:
            if raw.get("margin_basis") == "ttm":
                nlbl = "net income (trailing 12 mo)"
            elif (raw.get("period_days") or 999) < 350:
                nlbl = "net income (annualized)"
            else:
                nlbl = "net income"
            inputs = f"{nlbl} {_money(ni)} ÷ total assets {_money(ta)}"
    elif key == "weak_growth":
        # Show the SAME (current, prior) pair the growth % was computed from. The rating uses
        # a full-year (10-K) YoY, so the old quarterly revenue/prior pair contradicted it
        # (e.g. AAP: -5.4% headline shown next to $2.61B vs $2.58B = +1.2%). Fall back to the
        # legacy quarterly pair only if the annual pair isn't stored.
        a = raw.get("revenue_growth_cur"); b = raw.get("revenue_growth_prior")
        if a is None or b is None:
            a, b = raw.get("revenue"), raw.get("revenue_prior")
        else:
            period_override = raw.get("revenue_growth_period")   # match the % basis (e.g. "FY2025")
        if a is not None and b is not None:
            inputs = f"revenue {_money(a)} vs prior-year {_money(b)}"
    if key in ("low_margin", "high_sga", "low_roa"):
        period_override = raw.get("margin_label")   # "trailing 12 mo to May 2026" (TTM basis)
    if key in ("cheap_abs", "cheap_pb"):
        be = raw.get("book_equity")
        inputs = ("price ÷ book value " + _money(be) +
                  " — price from Alpha Vantage, book value from SEC 10-K"
                  if be is not None
                  else "price ÷ book value — price from Alpha Vantage, book value from SEC")
    elif key == "cheap_ev_ebitda":
        mc = r.get("market_cap")
        debt = raw.get("debt")
        cash = raw.get("cash")
        ebitda = raw.get("ebitda")
        if mc is not None and ebitda:
            ev = mc + (debt or 0) - (cash or 0)
            inputs = (f"EV {_money(ev)} (mkt cap {_money(mc)} + debt {_money(debt or 0)} "
                      f"− cash {_money(cash or 0)}) ÷ EBITDA {_money(ebitda)} "
                      f"— EBITDA = operating income + D&A (SEC XBRL)")
    url = _source_url(r.get("cik"), raw.get("source_accn")) if source == "SEC XBRL" else None
    return {"key": key, "label": LABELS.get(key, key), "value": _fmt_metric(metric, v),
            "context": ctx, "inputs": inputs, "period": period_override or _period_label(raw),
            "source": source, "url": url}
def _event_evidence(key, ev):
    item = ev.get(key)
    return {"key": key, "label": LABELS.get(key, key), "value": "",
            "context": (item["title"] if item else ""), "inputs": "", "period": "",
            "source": EVENT_SOURCE.get(key, "EDGAR"),
            "url": (item["url"] if item else None)}
def _event_signals(cik, ticker, name):
    triggered = set()
    top = None
    ev = {}
    activist = None
    for f in database.filings_in_window(_pad_cik(cik), config.SCORE_WINDOW_DAYS):
        for sig in (f.get("signals") or "").split(","):
            sig = sig.strip()
            if sig in EVENT_POINTS:
                triggered.add(sig)
                ev.setdefault(sig, {"title": f"{f['company']} — {f['title']}", "url": f["url"]})
                if top is None and EVENT_POINTS.get(sig, 0) > 0:
                    top = {"title": f"{f['company']} — {f['title']}", "url": f["url"]}
    if ticker:
        nws = database.news_for_ticker_in_window(ticker, config.SCORE_WINDOW_DAYS)
        # Fire only on a genuinely NEGATIVE / distress headline — not just any recent company
        # news. Per-company news is stored UNFILTERED (conference transcripts, "Top movers"
        # columns, bullish analyst notes), so taking nws[0] mislabeled benign items as a
        # "negative headline" on nearly every profile. Reuse the broad-feed distress filter and
        # surface the first headline that actually qualifies.
        from . import news as _news   # local import avoids a circular import at module load
        # Must be (a) genuinely negative/distress AND (b) actually ABOUT this company — Finnhub's
        # per-ticker feed returns tangential roundups ("Toll Brothers upgraded, Lennar downgraded"
        # tagged to Ingredion). Reuse the same name-match the activist path uses.
        _, _keys = _company_keys(name)
        # Also exclude analyst opinion / rating-change chatter (a downgrade, price target, "buy/sell/
        # hold" piece) — those are commentary, not a company distress event, so they shouldn't read
        # as a "negative press" catalyst (e.g. "Evercore downgrades INSP").
        neg = next((n for n in nws if _news.is_relevant(n.get("headline"))
                    and _headline_about_company(n.get("headline"), _keys)
                    and not _OPINION_RE.search(n.get("headline") or "")), None)
        if neg:
            triggered.add("news_negative")
            ev.setdefault("news_negative", {"title": neg["headline"], "url": neg["url"]})
            if top is None:
                top = {"title": neg["headline"], "url": neg["url"]}
        # Strict: a headline that names a known activist AND this specific company.
        activist = _activist_news_hit(ticker, name)
    return triggered, top, ev, activist
def recompute_all():
    funds = database.get_all_fundamentals()
    companies = {_pad_cik(c["cik"]): c for c in database.get_companies()}
    recs = []
    for f in funds:
        cik = _pad_cik(f["cik"])
        comp = companies.get(cik, {})
        try:
            raw = json.loads(f.get("raw") or "{}")
        except (ValueError, TypeError):
            raw = {}
        # EV/EBITDA (valuation) and goodwill/assets (M&A overpayment) from XBRL + price.
        mcap = comp.get("market_cap")
        ebitda = raw.get("ebitda")
        ev_ebitda = None
        if mcap is not None and ebitda is not None and ebitda > 0:
            ev = mcap + (raw.get("debt") or 0) - (raw.get("cash") or 0)
            if ev > 0:
                ev_ebitda = ev / ebitda
                # A multiple this high means EBITDA is negligible relative to EV — the ratio
                # isn't a meaningful valuation read (it just produces "219x"). Null it.
                if ev_ebitda > 75:
                    ev_ebitda = None
        goodwill_to_assets = None
        gw, ta = raw.get("goodwill"), raw.get("total_assets")
        if gw is not None and ta:
            goodwill_to_assets = gw / ta
        recs.append({
            "cik": cik, "ticker": f.get("ticker"),
            "sector": f.get("sector") or "??",
            "operating_margin": f.get("operating_margin"), "roa": f.get("roa"),
            "revenue_growth": f.get("revenue_growth"), "sga_pct": f.get("sga_pct"),
            "cash_to_assets": f.get("cash_to_assets"), "debt_to_assets": f.get("debt_to_assets"),
            "pb_ratio": comp.get("pb_ratio"), "tsr_1y": comp.get("tsr_1y"),
            "tsr_3y": comp.get("tsr_3y"), "tsr_5y": comp.get("tsr_5y"),
            "market_cap": comp.get("market_cap"),
            "ev_ebitda": ev_ebitda, "goodwill_to_assets": goodwill_to_assets,
            "name": comp.get("name") or f.get("ticker"), "raw": raw,
        })
    # Drop delisted / deregistered / long-dormant names so they can't be pitched,
    # and keep them out of the peer benchmarks too.
    recs = [r for r in recs if not r.get("raw", {}).get("inactive")]
    # Index by (padded) CIK so a company can be compared against its self-selected peers.
    rec_by_cik = {r["cik"]: r for r in recs}
    try:
        spy_1y = float(database.get_meta("spy_1y"))
    except (TypeError, ValueError):
        spy_1y = None
    try:
        spy_3y = float(database.get_meta("spy_3y"))
    except (TypeError, ValueError):
        spy_3y = None
    try:
        spy_5y = float(database.get_meta("spy_5y"))
    except (TypeError, ValueError):
        spy_5y = None
    gov = database.get_all_governance()
    ins_all = database.get_all_insider()
    votes_all = database.get_all_votes()
    reactions = database.get_all_exec_reactions()
    # Normalize keys to the padded 10-digit CIK that the `recs` rows use. Activist flags are
    # written with the universe's UNPADDED cik (e.g. "1702744") while rows are padded
    # ("0001702744"), so a raw lookup silently missed every flag -> no Confirmed situations.
    # _pad_cik is idempotent, so this also handles any older padded-key rows.
    aflags = {_pad_cik(k): v for k, v in database.get_all_activist_flags().items()}
    manual = {_pad_cik(k): v for k, v in database.get_manual_situations().items()}   # partner overrides (always win)
    # 13F activist holders, keyed by ticker (early-warning: an activist is in the stock but
    # hasn't agitated). Materiality gating happens per-row below.
    holders_by_ticker = database.holders_for_all()
    metrics = ["pb_ratio", "ev_ebitda", "goodwill_to_assets", "operating_margin",
               "tsr_1y", "tsr_3y", "roa", "revenue_growth", "sga_pct",
               "cash_to_assets", "debt_to_assets"]
    by_sector = {}
    for r in recs:
        by_sector.setdefault(r["sector"], []).append(r)
    th = {sec: {m: _quantiles([x.get(m) for x in rows]) for m in metrics}
          for sec, rows in by_sector.items()}
    # Sector tail anchors (5th/95th pct) used to scale each signal's severity.
    ext = {sec: {m: _pct_lo_hi([x.get(m) for x in rows]) for m in metrics}
           for sec, rows in by_sector.items()}
    rows = []
    for r in recs:
        t = th.get(r["sector"], {})
        e = ext.get(r["sector"], {})
        r["_spy_1y"] = spy_1y
        r["_spy_3y"] = spy_3y
        r["_spy_5y"] = spy_5y
        # Profitability sanity: when a company is GAAP-profitable over the full year, a
        # negative latest-period margin/ROA is almost always a one-time charge (impairment,
        # discontinued-ops divestiture) distorting the quarter — not real distress. We don't
        # let those distressed signals fire in that case (this is the Gibraltar fix: a company
        # that earned $3.25/share shouldn't carry a "negative margin / loses money" thesis).
        _raw = r.get("raw") or {}
        _ani = _raw.get("annual_net_income")
        r["_gaap_profitable"] = (_ani is not None and _ani > 0)
        # A one-time BELOW-THE-LINE charge (goodwill impairment, divestiture loss) presents as
        # an operating business that is roughly breakeven-or-better while the bottom line is
        # deeply negative. Annualizing such a quarter into ROA (Gibraltar's -$277M phantom
        # loss) — or reading it as "loses money" — is a distortion, not real distress. This
        # catches the case the annual-NI test misses: when the charge dragged the most recent
        # full year negative too. A genuine cash-burner has a deeply NEGATIVE *operating*
        # margin as well, so it won't qualify here and its ROA/margin signals still fire.
        _rev = _raw.get("revenue"); _opi = _raw.get("operating_income"); _ni = _raw.get("net_income")
        _charge_distorted = bool(
            _rev and _rev > 0 and _opi is not None and _ni is not None
            and (_opi / _rev) > -0.05 and (_ni / _rev) < -0.10
        )
        _distorted = r["_gaap_profitable"] or _charge_distorted
        # Banks / insurers / brokers (SIC 60-64) hold cash & investments as regulatory
        # reserves / float, run structurally high leverage, and don't report an industrial
        # "operating margin" / "SG&A" / EBITDA. Those balance-sheet & income-statement signals
        # aren't activist levers there (TRUP's $384M "idle cash" is claims reserves), so we
        # suppress them for financials. P/B discount, price underperformance, ROA-vs-peers,
        # goodwill, governance and events stay valid and still fire.
        _is_financial = (r.get("sector") or "") in ("60", "61", "62", "63", "64")
        # A STRONG MULTI-YEAR OUTPERFORMER (beat the S&P by >=25 pts over 3yr or >=40 pts over
        # 5yr) fails the screen's core premise — "sustained underperformer." A 1-yr pullback on
        # a multi-year market-beater (Axon: +138%/3yr, +163%/5yr, P/E ~200) is profit-taking,
        # not an activist campaign setup, and its low operating margin / high SG&A / cash are
        # growth-investment characteristics the market rewards — not levers. So for these names
        # we suppress the soft value/operating/balance-sheet signals and strategic_review, while
        # still letting governance, say-on-pay, insider and events through (real even on a winner
        # that stumbled). Fallen angels (INSP -150pts/3yr) and laggards (TRUP, Kohl's) are NOT
        # outperformers, so they keep all their signals.
        # Anchor on the 3-YEAR gap (the current multi-year trend). A 5-yr-alone test would wrongly
        # rescue a fallen angel that still carries an OLD 5-yr gain but has since collapsed (TMDX:
        # 5yr +36pts yet 3yr -83pts and 1yr -67pts — a current target, not a winner). Axon beats
        # it on 3yr (+74); TMDX fails on 3yr regardless of its stale 5yr number. The 5-yr gap is
        # only a secondary confirm (a name must ALSO be at least flat-to-market over 3yr).
        _g3 = ((r.get("tsr_3y") - spy_3y) if (r.get("tsr_3y") is not None and spy_3y is not None) else None)
        _g5 = ((r.get("tsr_5y") - spy_5y) if (r.get("tsr_5y") is not None and spy_5y is not None) else None)
        _strong_outperformer = (_g3 is not None and _g3 >= 0.25) or (
            _g5 is not None and _g5 >= 0.40 and _g3 is not None and _g3 >= 0)
        trig = []
        def low(metric):
            q1, _, _ = t.get(metric, (None, None, 0))
            v = r.get(metric)
            return q1 is not None and v is not None and v <= q1
        def high(metric):
            _, q3, _ = t.get(metric, (None, None, 0))
            v = r.get(metric)
            return q3 is not None and v is not None and v >= q3
        if r.get("pb_ratio") is not None and 0 < r["pb_ratio"] < 1.5:
            trig.append("cheap_abs")
        elif r.get("pb_ratio") is not None and r["pb_ratio"] > 0 and low("pb_ratio"):
            trig.append("cheap_pb")
        if (r.get("ev_ebitda") is not None and r["ev_ebitda"] > 0 and low("ev_ebitda")
                and not _is_financial):
            trig.append("cheap_ev_ebitda")
        if r.get("goodwill_to_assets") is not None and high("goodwill_to_assets"):
            trig.append("high_goodwill")
        # Operational-improvement levers (margin, SG&A, ROA) only make sense for a company that
        # is actually PROFITABLE — you expand a thin-but-positive margin by cutting costs; you
        # can't cost-cut your way out of a loss. A NEGATIVE margin/ROA is growth reinvestment
        # (SBC/R&D), acquisition amortization (AVAV/BlueHalo), or genuine distress — not a clean
        # "margin turnaround" activist thesis, and is already captured by other signals
        # (cash-burner discount, strategic_review, the de-rate). Requiring the metric to be
        # POSITIVE-but-below-peer kills the false "margin turnaround" theses on money-losing
        # growth/software/medtech names (CERT, AVAV, SolarEdge, Tandem) while keeping the real
        # value targets (Kohl's, Advance Auto — profitable, thin margins). Subsumes the earlier
        # charge-distortion guard for these signals.
        if (low("operating_margin") and not _is_financial and not _strong_outperformer
                and (r.get("operating_margin") or 0) > 0):
            trig.append("low_margin")
        if (r.get("tsr_1y") is not None and spy_1y is not None
                and (r["tsr_1y"] - spy_1y) <= TSR_LAG_1Y):
            trig.append("weak_tsr_1y")
        if (r.get("tsr_3y") is not None and spy_3y is not None
                and (r["tsr_3y"] - spy_3y) <= TSR_LAG_3Y):
            trig.append("weak_tsr_3y")
        # Fallen-quality / strategic-alternatives: a viable business that de-rated sharply
        # (down >=30% over the year) — a sale / take-private / brand-reset candidate the
        # value/distress signals miss. Excludes cash-burners (falling knives, already discounted)
        # AND still-hyper-growth names (rev growth > 20%): a sharp pullback on a fast grower is
        # multiple compression on a business the market still backs (Axon, AeroVironment,
        # Inspire), not a sale setup. Mature de-raters (TRUP, Lululemon-type) still fire;
        # unknown growth still fires (don't drop a possible target on missing data).
        _hyper_growth = (r.get("revenue_growth") is not None and r["revenue_growth"] > 0.20)
        if (r.get("tsr_1y") is not None and r["tsr_1y"] <= STRATEGIC_DROP
                and not _is_cash_burner(r) and not _hyper_growth and not _strong_outperformer):
            trig.append("strategic_review")
        if low("roa") and (r.get("roa") or 0) > 0:    # profitable-but-low return (see low_margin note)
            trig.append("low_roa")
        if r.get("revenue_growth") is not None and (r["revenue_growth"] < 0 or low("revenue_growth")):
            trig.append("weak_growth")
        if (high("sga_pct") and not _is_financial and not _strong_outperformer
                and (r.get("operating_margin") or 0) > 0):
            trig.append("high_sga")
        # Leveraged-recap / negative-equity names (Domino's) have already returned all their
        # capital via debt-funded buybacks — "cash = idle capital to return" is exactly wrong.
        if (high("cash_to_assets") and not _is_financial and not _strong_outperformer
                and not _leveraged_recap(r)):
            trig.append("cash_hoard")
        # Lease guard: a lease-heavy retailer at zero funded debt isn't really under-levered.
        _bs = r.get("raw") or {}
        _ol, _ta = _bs.get("operating_lease"), _bs.get("total_assets")
        _lease_heavy = _ol is not None and _ta and _ta > 0 and (_ol / _ta) >= LEASE_HEAVY
        if low("debt_to_assets") and not _is_financial and not _strong_outperformer and not _lease_heavy:
            trig.append("underlevered")
        # Governance red flags (from DEF 14A; only present for parsed names).
        g = gov.get(r["cik"]) or {}
        r["_gov_url"] = g.get("proxy_url")
        r["_gov_date"] = g.get("proxy_date")
        if g.get("classified_board"):
            trig.append("gov_classified")
        if g.get("poison_pill"):
            trig.append("gov_poison")
        if g.get("dual_class"):
            trig.append("gov_dual")
        # CEO pay-for-performance: pay rose while the stock lagged the market.
        comp = None
        if g.get("comp_json"):
            try:
                comp = json.loads(g["comp_json"])
            except (ValueError, TypeError):
                comp = None
        r["_comp"] = comp
        if comp:
            pc = comp.get("pct_change")
            tsr = r.get("tsr_1y")
            lags = (tsr is not None and (tsr < 0 or (spy_1y is not None and (tsr - spy_1y) <= TSR_LAG_1Y)))
            # Suppress across a CEO change: the % then compares a new CEO's partial first year to a
            # full year (a ramp, not a raise) — e.g. Signet's false "pay rose 333%". Two guards:
            #  (a) the proxy itself flagged a transition (compensation.py) — precise, but only present
            #      once the proxy is RE-parsed with that code; cached comp rows won't have it.
            #  (b) scoring-side fallback for those cached rows: a big jump (>= COMP_RAMP_SUSPECT)
            #      coinciding with a recent CEO-change 8-K in the window.
            _ceo_change = any(
                s.strip() in ("ceo_departure", "leadership_change")
                for f in database.filings_in_window(_pad_cik(r["cik"]), config.SCORE_WINDOW_DAYS)
                for s in (f.get("signals") or "").split(","))
            _transition = comp.get("ceo_transition") or (_ceo_change and (pc or 0) >= COMP_RAMP_SUSPECT)
            if pc is not None and pc >= COMP_RISE_FLOOR and lags and not _transition:
                trig.append("overpaid_ceo")
        # Self-selected proxy peer group: does the company trail the peers it chose itself?
        peers_list = []
        if g.get("peers_json"):
            try:
                peers_list = json.loads(g["peers_json"]) or []
            except (ValueError, TypeError):
                peers_list = []
        pa = _peer_analysis(r, peers_list, rec_by_cik) if peers_list else None
        r["_peers"] = pa or {}
        if pa and pa.get("n_tsr", 0) >= PEER_MIN:
            pmed = (pa.get("median") or {}).get("tsr_1y")
            if pmed is not None and r.get("tsr_1y") is not None and r["tsr_1y"] <= pmed - PEER_LAG:
                trig.append("lags_own_peers")
        # Exec-change stock reaction: the stock dropped (vs the S&P) on a leadership 8-K.
        rxn = reactions.get(r["cik"]) or {}
        r["_reaction"] = rxn
        abn = rxn.get("abnormal")
        if abn is not None and abn <= REACTION_DROP_FLOOR:
            trig.append("exec_reaction_drop")
        # Insider activity (Form 4; only present for parsed names).
        ins = ins_all.get(r["cik"]) or {}
        r["_insider"] = ins
        buy_v, sell_v = ins.get("buy_value") or 0, ins.get("sell_value") or 0
        ns, nb = ins.get("n_sellers") or 0, ins.get("n_buyers") or 0
        if ns >= 2 and sell_v > buy_v:
            trig.append("insider_selling")
        elif nb >= 1 and buy_v > sell_v and buy_v > 0:
            trig.append("insider_buying")
        # Shareholder-vote discontent (8-K 5.07; only present for parsed names).
        vrow = votes_all.get(r["cik"]) or {}
        r["_votes"] = vrow
        sop = vrow.get("say_on_pay")
        if sop is not None and SAY_ON_PAY_MIN <= sop < SAY_ON_PAY_FLAG:
            trig.append("weak_vote_support")
        struct = sum(STRUCT_POINTS[s] for s in trig)
        events, top, ev, activist = _event_signals(r["cik"], r["ticker"], r["name"])
        total = struct + sum(EVENT_POINTS[s] for s in events)
        trig += list(events)
        aflag = aflags.get(r["cik"])
        # 18-month agitation gate is enforced upstream by activist.WINDOW_DAYS (=548): the
        # sweep only writes flags for filings <=18 months old, so every flag here is already
        # in-window. (A scoring-side date re-check was removed — it could wrongly drop a valid
        # Confirmed situation whenever the stored filing date was missing/odd.)
        man = manual.get(r["cik"]) or {}
        man_status = man.get("status")
        # An EXEMPT SOLICITATION (Rule 14a-6(g) notice / PX14A6G) is filed by a shareholder who
        # is NOT running a full proxy — sometimes a serious activist (HoldCo at Comerica), but
        # often an ESG/gadfly filer. It's less authoritative than a 13D or a contested proxy, so
        # we keep the name visible but at the lower "reported" tier (confirm before relying)
        # rather than "confirmed". 13D / contested-proxy flags stay "confirmed".
        _exempt = aflag is not None and (
            "exempt solicitation" in (aflag.get("label") or "").lower()
            or (aflag.get("form") or "").upper().replace(" ", "") == "PX14A6G")
        # A corporate acquirer's takeover/control contest is its own tier ("ma"), NOT a
        # shareholder-activist campaign — keep it off the "authoritative activist" header.
        # A SETTLED campaign (cooperation agreement) stands down into a muted "settled" tier.
        _is_ma = aflag is not None and (aflag.get("kind") == "ma")
        _is_settled = aflag is not None and (aflag.get("kind") in ("settled", "concluded"))
        _aflag_tier = ("settled" if _is_settled else "ma" if _is_ma
                       else ("reported" if _exempt else "confirmed"))
        # Decide whether this name is an ACTIVE SITUATION and at what confidence tier:
        #   confirmed -> authoritative SEC activist filing (13D / contested proxy)
        #   reported  -> a news headline naming a known activist, OR an exempt solicitation
        #   manual    -> a partner tagged it by hand
        # A manual override ALWAYS wins: "active" forces it on (even with no auto signal),
        # "exclude" suppresses a false-positive auto-detection.
        if man_status == "exclude":
            if total < LEAD_FLOOR:
                continue                         # not a proactive lead either -> drop it
            is_active, tier = False, ""
        elif man_status == "active":
            is_active = True
            tier = _aflag_tier if aflag else ("reported" if activist else "manual")
        else:
            if total < LEAD_FLOOR and not aflag and not activist:
                continue
            if aflag:
                is_active, tier = True, _aflag_tier
            elif activist:
                is_active, tier = True, "reported"
            else:
                is_active, tier = False, ""
        # 13F activist-holder signal (early warning). A known activist already holds a MATERIAL
        # stake but hasn't agitated (no 13D/proxy) -> the highest-conviction proactive setup:
        # the smart money is in the stock, we bring the thesis. Suppressed once a name IS an
        # active situation (aflag/news/manual) -- there the holding is redundant with the
        # campaign. Adds points + an evidence card + a warm-intro pitch input.
        if not is_active:
            _mh = _material_holders(holders_by_ticker.get((r.get("ticker") or "").upper()))
            if _mh:
                r["_holders"] = _mh
                trig.append("activist_holder")
                total += STRUCT_POINTS["activist_holder"]
        # Data-quality guard: a proactive lead with no market cap is data-incomplete
        # (its valuation signals can't be trusted and the card looks broken, e.g. IAC's
        # blank market cap). Drop it from the leads — but never drop an active situation.
        if not is_active and r.get("market_cap") is None:
            continue
        # 0-100 absolute vulnerability, weighted by how severe each signal is.
        vuln = _vuln_score(trig, r, t, e)
        # Quality floor for the PROACTIVE board: a Moderate match (< MIN_LEAD_VULN) barely
        # fits the activist-target profile and shouldn't be presented as a "company to pitch"
        # — it just dilutes the list. We drop those from the leads (active situations are
        # always kept regardless, since they're tracked for awareness, not pitched).
        # EXEMPTION: a "strategic_review" (fallen-quality / sale-candidate) name is a distinct,
        # high-value category that trips few signals by nature — it would score below the
        # generic floor, but it's exactly the class we built this signal to surface, so it's
        # kept even at a modest score (it's labeled distinctly, not generic weak-signal noise).
        # 4e: the exemption applies ONLY to a fallen-but-viable de-rater. A structural-decline
        # name (top line shrinking) is a possible falling knife — it forfeits the force-keep and
        # must clear the normal floor on its own merits to reach the proactive board.
        _sr_exempt = ("strategic_review" in trig and not _structural_decline(r))
        if not is_active and vuln < MIN_LEAD_VULN and not _sr_exempt:
            continue
        evidence = []
        if aflag:
            evidence.append({
                "key": "activist_filing", "label": "Activist already engaged",
                "value": "", "context": aflag.get("label") or "Activist filing on record",
                "inputs": "", "period": (aflag.get("form") or "") +
                (f" · filed {aflag['filed']}" if aflag.get("filed") else ""),
                "source": "SEC EDGAR (full-text search)", "url": aflag.get("url")})
        for key in trig:
            if key in INSIDER_KEYS:
                evidence.append(_insider_evidence(key, r))
            elif key in VOTE_KEYS:
                evidence.append(_vote_evidence(key, r))
            elif key in COMP_KEYS:
                evidence.append(_comp_evidence(key, r))
            elif key in REACTION_KEYS:
                evidence.append(_reaction_evidence(key, r))
            elif key in PEER_KEYS:
                evidence.append(_peer_evidence(key, r))
            elif key == "strategic_review":
                evidence.append(_strategic_evidence(key, r))
            elif key == "activist_holder":
                evidence.append(_holder_evidence(key, r))
            elif key in STRUCT_META or key in GOV_KEYS:
                evidence.append(_struct_evidence(key, r, t))
            elif key in EVENT_POINTS:
                evidence.append(_event_evidence(key, ev))
        # Headline item + situation metadata for the Active Situations card.
        if aflag:
            item = {"title": f"{r['name']} — {aflag.get('label')}", "url": aflag.get("url")}
            smeta = {"who": "", "kind": aflag.get("kind") or "13d", "form": aflag.get("form"),
                     "label": aflag.get("label") or "Activist filing on record",
                     "date": aflag.get("filed"), "source": "SEC EDGAR", "url": aflag.get("url")}
        elif activist:
            item = {"title": activist["title"], "url": activist["url"]}
            smeta = {"who": activist.get("who"), "kind": "news", "form": "",
                     "label": (activist.get("who") or "Activist") + " — reported in the press",
                     "date": activist.get("date"), "source": "News", "url": activist.get("url")}
        elif tier == "manual":
            item = top
            smeta = {"who": man.get("actor") or "", "kind": "manual", "form": "",
                     "label": "Manually tagged as an active situation",
                     "date": (man.get("updated_at") or "")[:10], "source": "Manual",
                     "url": (top or {}).get("url")}
        else:
            item = top
            smeta = {}
        if man_status == "active":
            smeta["manual"] = True
            smeta["manual_note"] = man.get("note") or ""
        rows.append({
            "cik": r["cik"], "ticker": r["ticker"], "company": r["name"],
            "market_cap": r.get("market_cap"), "score": total, "vuln": vuln,
            "signals": " + ".join(LABELS[s] for s in trig if s in LABELS),
            "top_item_title": item["title"] if item else "",
            "top_item_url": item["url"] if item else "",
            "active_situation": 1 if is_active else 0,
            "situation_tier": tier,
            "situation_meta": smeta,
            "evidence": evidence,
            "pitch": pitch.build_pitch(r, trig),
            "fin_context": _fin_context(r, t, e),
            "peer_analysis": r.get("_peers") or {},
            "first_flagged": database.now_iso()[:10],
        })
    # Rank by the 0-100 vulnerability (tie-break on raw signal count, then size).
    rows.sort(key=lambda r: (r["vuln"], r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    shortlist = [r for r in rows if not r.get("active_situation")]
    return shortlist[: config.SHORTLIST_SIZE]
