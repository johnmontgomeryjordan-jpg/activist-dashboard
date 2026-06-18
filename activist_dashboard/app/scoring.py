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

from . import config, database

MIN_PEERS = 5
# 1-yr stock return must lag the S&P 500 by at least this much (in return terms) to flag.
TSR_LAG_1Y = -0.15

STRUCT_POINTS = {"cheap_abs": 2, "cheap_pb": 2, "low_margin": 2, "weak_tsr_1y": 1,
                 "weak_tsr_3y": 1, "low_roa": 1, "weak_growth": 1, "high_sga": 1,
                 "cash_hoard": 1, "underlevered": 1,
                 "gov_classified": 1, "gov_poison": 1, "gov_dual": 1}
EVENT_POINTS = {"ceo_departure": 2, "earnings_miss": 2, "impairment": 2,
                "layoffs": 1, "leadership_change": 1, "results_update": 0,
                "news_negative": 1}

LABELS = {
    "cheap_abs": "cheap (low price-to-book < 1.5x)",
    "cheap_pb": "cheap vs peers (low price-to-book)",
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
}

# Governance red flags (from DEF 14A). Boolean signals -> their own evidence cards.
GOV_KEYS = ("gov_classified", "gov_poison", "gov_dual")
GOV_META = {
    "gov_classified": "directors elected in staggered classes — entrenches the board against change",
    "gov_poison": "shareholder rights plan in place — blocks an activist from accumulating a stake",
    "gov_dual": "super-voting share structure — insiders control the vote",
}

# Structural signal -> (metric, direction, source label).
PCT_METRICS = {"operating_margin", "tsr_1y", "tsr_3y", "roa", "revenue_growth",
               "sga_pct", "cash_to_assets", "debt_to_assets"}
STRUCT_META = {
    "cheap_abs": ("pb_ratio", "abs", "Alpha Vantage"),
    "cheap_pb": ("pb_ratio", "low", "Alpha Vantage"),
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
}
EVENT_SOURCE = {"ceo_departure": "SEC 8-K", "earnings_miss": "SEC 8-K",
                "impairment": "SEC 8-K", "layoffs": "SEC 8-K",
                "leadership_change": "SEC 8-K", "results_update": "SEC 8-K",
                "news_negative": "News"}

# A headline matched to a company that names an activist / campaign means an activist
# has ALREADY shown up -- too late to pitch proactively, so we pull these names off the
# main shortlist into a separate "active situations" bucket instead of ranking them.
ACTIVIST_NEWS = [
    "activist", "proxy fight", "proxy battle", "proxy contest", "13d",
    "board seat", "board seats", "nominat", "director nominee",
    "builds stake", "raises stake", "takes stake", "boosts stake",
    "elliott management", "starboard", "trian", "jana partners", "third point",
    "carl icahn", "icahn", "nelson peltz", "valueact", "value act", "engine no",
    "ancora", "politan", "sachem head", "legion partners",
    "short seller", "short-seller",
]


def _activist_headline(headline):
    t = " " + (headline or "").lower() + " "
    return any(k in t for k in ACTIVIST_NEWS)


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


def _fmt_metric(metric, v):
    if v is None:
        return "n/a"
    if metric == "pb_ratio":
        return f"{v:.2f}x"
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


def _event_signals(cik, ticker):
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
            for n in nws:
                if _activist_headline(n.get("headline")):
                    activist = {"title": n["headline"], "url": n["url"]}
                    break
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
        recs.append({
            "cik": cik, "ticker": f.get("ticker"),
            "sector": f.get("sector") or "??",
            "operating_margin": f.get("operating_margin"), "roa": f.get("roa"),
            "revenue_growth": f.get("revenue_growth"), "sga_pct": f.get("sga_pct"),
            "cash_to_assets": f.get("cash_to_assets"), "debt_to_assets": f.get("debt_to_assets"),
            "pb_ratio": comp.get("pb_ratio"), "tsr_1y": comp.get("tsr_1y"),
            "tsr_3y": comp.get("tsr_3y"), "market_cap": comp.get("market_cap"),
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

    metrics = ["pb_ratio", "operating_margin", "tsr_1y", "tsr_3y", "roa",
               "revenue_growth", "sga_pct", "cash_to_assets", "debt_to_assets"]
    by_sector = {}
    for r in recs:
        by_sector.setdefault(r["sector"], []).append(r)
    th = {sec: {m: _quantiles([x.get(m) for x in rows]) for m in metrics}
          for sec, rows in by_sector.items()}

    rows = []
    for r in recs:
        t = th.get(r["sector"], {})
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

        struct = sum(STRUCT_POINTS[s] for s in trig)
        events, top, ev, activist = _event_signals(r["cik"], r["ticker"])
        total = struct + sum(EVENT_POINTS[s] for s in events)
        trig += list(events)

        if total < config.SCORE_THRESHOLD:
            continue

        evidence = []
        for key in trig:
            if key in STRUCT_META or key in GOV_KEYS:
                evidence.append(_struct_evidence(key, r, t))
            elif key in EVENT_POINTS:
                evidence.append(_event_evidence(key, ev))

        # Activist already on the scene -> move to the "active situations" bucket
        # (too late to pitch) and surface the activist headline as its latest item.
        item = activist or top
        rows.append({
            "cik": r["cik"], "ticker": r["ticker"], "company": r["name"],
            "market_cap": r.get("market_cap"), "score": total,
            "signals": " + ".join(LABELS[s] for s in trig if s in LABELS),
            "top_item_title": item["title"] if item else "",
            "top_item_url": item["url"] if item else "",
            "active_situation": 1 if activist else 0,
            "evidence": evidence,
            "first_flagged": database.now_iso()[:10],
        })

    rows.sort(key=lambda r: (r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    shortlist = [r for r in rows if not r.get("active_situation")]
    return shortlist[: config.SHORTLIST_SIZE]
