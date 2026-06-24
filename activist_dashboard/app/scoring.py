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
import re

from . import config, database, pitch

MIN_PEERS = 5
# 1-yr stock return must lag the S&P 500 by at least this much (in return terms) to flag.
TSR_LAG_1Y = -0.15

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
                 "gov_classified": 1, "gov_poison": 1, "gov_dual": 1,
                 "insider_selling": 1, "insider_buying": 0,
                 "weak_vote_support": 1}
EVENT_POINTS = {"ceo_departure": 2, "earnings_miss": 2, "impairment": 2,
                "layoffs": 1, "leadership_change": 1, "results_update": 0,
                "news_negative": 1}
POINTS = {**STRUCT_POINTS, **EVENT_POINTS}

# The 0-100 number is an ABSOLUTE "activist-target profile" index, NOT a probability of
# a campaign. Each triggered signal contributes its point weight scaled by *how severe*
# it is (0..1) — how cheap, how deep in the peer tail, how far returns lag. VULN_SCALE is
# the severity-weighted point total that maps to the top of the range. We deliberately
# cap the index at VULN_MAX (below 100) so it never reads as "100% certain to be targeted"
# — the highest a company can score is "matches the activist-target profile as strongly as
# we measure." A strong multi-signal lead lands in the 80s; lighter leads spread down into
# the 30s-50s. The headline a partner sees is a RATING BAND (see VULN_BANDS), with this
# index as supporting detail.
VULN_SCALE = 10.0
VULN_MAX = 92

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

LABELS = {
    "cheap_abs": "cheap (low price-to-book < 1.5x)",
    "cheap_pb": "cheap vs peers (low price-to-book)",
    "cheap_ev_ebitda": "cheap on EV/EBITDA vs peers",
    "high_goodwill": "goodwill-heavy balance sheet (acquisition risk)",
    "low_margin": "low operating margin vs peers",
    "weak_tsr_1y": "1-yr stock return lagging the market",
    "weak_tsr_3y": "weak 3-yr stock return vs market",
    "low_roa": "low return on assets vs peers",
    "weak_growth": "shrinking / weak revenue growth",
    "high_sga": "bloated SG&A vs peers",
    "cash_hoard": "cash-heavy balance sheet",
    "underlevered": "under-levered balance sheet",
    "ceo_departure": "recent CEO/exec departure",
    "earnings_miss": "recent earnings miss / guidance cut",
    "impairment": "recent impairment / write-down",
    "layoffs": "recent layoffs / restructuring",
    "leadership_change": "recent leadership change",
    "results_update": "recent results",
    "news_negative": "negative activist headline",
    "gov_classified": "classified / staggered board",
    "gov_poison": "poison pill / rights plan",
    "gov_dual": "dual-class / super-voting stock",
    "insider_selling": "cluster of insider selling",
    "insider_buying": "insiders buying (confidence signal)",
    "weak_vote_support": "weak say-on-pay support",
}

# Insider activity (Form 4). insider_selling is a leading vulnerability signal;
# insider_buying is shown as a 0-point defense/confidence note.
INSIDER_KEYS = ("insider_selling", "insider_buying")

# Shareholder-vote discontent (8-K Item 5.07). Flag when say-on-pay support falls below
# this fraction -- well under the ~90%+ norm, a recognized pre-activism warning.
VOTE_KEYS = ("weak_vote_support",)
SAY_ON_PAY_FLAG = 0.75

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
                "impairment": "SEC 8-K", "layoffs": "SEC 8-K",
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

# Known activist funds. Substring match on the lowercased headline.
KNOWN_FUNDS = [
    "elliott", "starboard value", "starboard", "trian", "jana partners", "jana",
    "third point", "carl icahn", "icahn", "nelson peltz", "valueact", "value act",
    "engine no", "ancora", "politan", "sachem head", "legion partners",
    "pershing square", "ackman", "corvex", "land & buildings", "land and buildings",
    "mantle ridge", "inclusive capital", "saba capital", "h partners",
    "cruiser capital", "irenic", "barington", "d.e. shaw", "blue harbour", "marcato",
    "glenview", "hestia", "bluebell", "soroban", "kimmeridge", "impactive", "scopia",
]
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
    "starboard value": "Starboard",
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


def _activist_news_hit(ticker, name):
    """Most recent news headline that names BOTH an activist and this company, or None."""
    _, keys = _company_keys(name)
    if not keys:
        return None
    for n in database.news_for_ticker_in_window(ticker, ACTIVIST_NEWS_WINDOW):
        who = _activist_cue(n.get("headline"))
        if who and _headline_about_company(n.get("headline"), keys):
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
    if key in ("weak_tsr_1y", "weak_tsr_3y"):
        ret = r.get("tsr_1y" if key == "weak_tsr_1y" else "tsr_3y")
        bench = r.get("_spy_1y")
        if ret is None or bench is None:
            return 0.5
        gap = bench - ret  # positive = underperformance vs the index
        return _clamp((gap - 0.15) / 0.50)  # 15pts behind -> 0, 65pts behind -> 1
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
        out.append({"key": key, "label": label, "value": v, "cutoff": cutoff,
                    "pct": pct, "verdict": verdict, "n": n})
    return out


def _vuln_score(trig, r, t, e):
    """0-VULN_MAX activist-target-profile index from the severity-weighted signal total.
    Capped below 100 so it never implies a guaranteed campaign."""
    sev_total = sum(POINTS.get(k, 0) * _severity(k, r, t, e) for k in trig)
    return min(VULN_MAX, int(round(100 * sev_total / VULN_SCALE)))


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
    metric = "tsr_1y" if key == "weak_tsr_1y" else "tsr_3y"
    ret = r.get(metric)
    bench = r.get("_spy_1y")
    if bench is not None and ret is not None:
        excess = (ret - bench) * 100
        ctx = (f"underperformed the S&P 500 by {abs(excess):.0f} pts over the past year "
               f"(stock {ret * 100:+.0f}% vs index {bench * 100:+.0f}%)")
    else:
        ctx = "lagged the broader market over the past year"
    return {"key": key, "label": LABELS.get(key, key), "value": _fmt_metric("tsr_1y", ret),
            "context": ctx, "inputs": "", "period": "trailing 1 yr",
            "source": "Finnhub (price return)", "url": None}


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
            nlbl = "net income (annualized)" if (raw.get("period_days") or 999) < 350 else "net income"
            inputs = f"{nlbl} {_money(ni)} ÷ total assets {_money(ta)}"
    elif key == "weak_growth":
        a, b = raw.get("revenue"), raw.get("revenue_prior")
        if a is not None and b is not None:
            inputs = f"revenue {_money(a)} vs prior-year {_money(b)}"
    elif key in ("cheap_abs", "cheap_pb"):
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
            "context": ctx, "inputs": inputs, "period": _period_label(raw),
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
        if nws:
            triggered.add("news_negative")
            ev.setdefault("news_negative", {"title": nws[0]["headline"], "url": nws[0]["url"]})
            if top is None:
                top = {"title": nws[0]["headline"], "url": nws[0]["url"]}
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
            "tsr_3y": comp.get("tsr_3y"), "market_cap": comp.get("market_cap"),
            "ev_ebitda": ev_ebitda, "goodwill_to_assets": goodwill_to_assets,
            "name": comp.get("name") or f.get("ticker"), "raw": raw,
        })

    # Drop delisted / deregistered / long-dormant names so they can't be pitched,
    # and keep them out of the peer benchmarks too.
    recs = [r for r in recs if not r.get("raw", {}).get("inactive")]

    try:
        spy_1y = float(database.get_meta("spy_1y"))
    except (TypeError, ValueError):
        spy_1y = None
    gov = database.get_all_governance()
    ins_all = database.get_all_insider()
    votes_all = database.get_all_votes()
    aflags = database.get_all_activist_flags()
    manual = database.get_manual_situations()   # partner overrides (always win)

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
        if r.get("ev_ebitda") is not None and r["ev_ebitda"] > 0 and low("ev_ebitda"):
            trig.append("cheap_ev_ebitda")
        if r.get("goodwill_to_assets") is not None and high("goodwill_to_assets"):
            trig.append("high_goodwill")
        if low("operating_margin"):
            trig.append("low_margin")
        if (r.get("tsr_1y") is not None and spy_1y is not None
                and (r["tsr_1y"] - spy_1y) <= TSR_LAG_1Y):
            trig.append("weak_tsr_1y")
        if low("roa"):
            trig.append("low_roa")
        if r.get("revenue_growth") is not None and (r["revenue_growth"] < 0 or low("revenue_growth")):
            trig.append("weak_growth")
        if high("sga_pct"):
            trig.append("high_sga")
        if high("cash_to_assets"):
            trig.append("cash_hoard")
        if low("debt_to_assets"):
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
        if sop is not None and sop < SAY_ON_PAY_FLAG:
            trig.append("weak_vote_support")

        struct = sum(STRUCT_POINTS[s] for s in trig)
        events, top, ev, activist = _event_signals(r["cik"], r["ticker"], r["name"])
        total = struct + sum(EVENT_POINTS[s] for s in events)
        trig += list(events)

        aflag = aflags.get(r["cik"])
        man = manual.get(r["cik"]) or {}
        man_status = man.get("status")

        # Decide whether this name is an ACTIVE SITUATION and at what confidence tier:
        #   confirmed -> authoritative SEC activist filing (13D / contested proxy)
        #   reported  -> a news headline naming a known activist AND this company
        #   manual    -> a partner tagged it by hand
        # A manual override ALWAYS wins: "active" forces it on (even with no auto signal),
        # "exclude" suppresses a false-positive auto-detection.
        if man_status == "exclude":
            if total < LEAD_FLOOR:
                continue                         # not a proactive lead either -> drop it
            is_active, tier = False, ""
        elif man_status == "active":
            is_active = True
            tier = "confirmed" if aflag else ("reported" if activist else "manual")
        else:
            if total < LEAD_FLOOR and not aflag and not activist:
                continue
            if aflag:
                is_active, tier = True, "confirmed"
            elif activist:
                is_active, tier = True, "reported"
            else:
                is_active, tier = False, ""

        # 0-100 absolute vulnerability, weighted by how severe each signal is.
        vuln = _vuln_score(trig, r, t, e)

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
            "first_flagged": database.now_iso()[:10],
        })

    # Rank by the 0-100 vulnerability (tie-break on raw signal count, then size).
    rows.sort(key=lambda r: (r["vuln"], r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    shortlist = [r for r in rows if not r.get("active_situation")]
    return shortlist[: config.SHORTLIST_SIZE]
