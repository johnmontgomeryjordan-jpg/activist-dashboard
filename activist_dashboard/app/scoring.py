"""
Predictive target-attractiveness scoring.

Structural, peer-relative within sector (2-digit SIC), from free SEC XBRL:
  Operating margin in bottom quartile of sector ................... 2
  ROA in bottom quartile of sector ............................... 1
  Revenue shrinking or bottom-quartile growth .................... 1
  SG&A % of revenue in top quartile (bloated costs) .............. 1
  Cash/assets in top quartile (lazy, cash-hoarding) ............. 1
  Debt/assets in bottom quartile (under-levered) ................ 1
Valuation (from Alpha Vantage shortlist enrichment):
  Cheap: price-to-book < 1.5x (absolute) ......................... 2
  (cheap_pb / TSR signals stay dormant unless full price coverage exists)
Event accelerants (recent filings/news), 1 each:
  CEO/exec departure, earnings miss, impairment, layoffs, neg. headline

Flagged at/above SCORE_THRESHOLD; top SHORTLIST_SIZE surfaced. Peer quartiles
need >= MIN_PEERS companies with the metric.
"""
from . import config, database

MIN_PEERS = 5

STRUCT_POINTS = {"cheap_abs": 2, "cheap_pb": 2, "low_margin": 2, "weak_tsr_1y": 1, "weak_tsr_3y": 1,
                 "low_roa": 1, "weak_growth": 1, "high_sga": 1, "cash_hoard": 1,
                 "underlevered": 1}
EVENT_POINTS = {"ceo_departure": 1, "earnings_miss": 1, "impairment": 1,
                "layoffs": 1, "news_negative": 1}

LABELS = {
    "cheap_abs": "cheap (low price-to-book < 1.5x)",
    "cheap_pb": "cheap vs peers (low price-to-book)",
    "low_margin": "low margin vs peers",
    "weak_tsr_1y": "weak 1-yr stock return vs peers",
    "weak_tsr_3y": "weak 3-yr stock return vs peers",
    "low_roa": "low return on assets vs peers",
    "weak_growth": "shrinking / weak revenue growth",
    "high_sga": "bloated SG&A vs peers",
    "cash_hoard": "cash-heavy balance sheet",
    "underlevered": "under-levered balance sheet",
    "ceo_departure": "recent CEO/exec departure",
    "earnings_miss": "recent earnings miss / guidance cut",
    "impairment": "recent impairment / write-down",
    "layoffs": "recent layoffs / restructuring",
    "news_negative": "negative activist headline",
}


def _pad_cik(cik):
    return str(cik).lstrip("0").zfill(10) if cik else cik


def _quantiles(values):
    vals = sorted(v for v in values if v is not None)
    if len(vals) < MIN_PEERS:
        return None, None
    def pct(p):
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[idx]
    return pct(0.25), pct(0.75)


def _event_signals(cik, ticker):
    triggered = set()
    top = None
    for f in database.filings_in_window(_pad_cik(cik), config.SCORE_WINDOW_DAYS):
        for sig in (f.get("signals") or "").split(","):
            sig = sig.strip()
            if sig in EVENT_POINTS:
                triggered.add(sig)
                if top is None:
                    top = {"title": f"{f['company']} — {f['title']}", "url": f["url"]}
    if ticker:
        nws = database.news_for_ticker_in_window(ticker, config.SCORE_WINDOW_DAYS)
        if nws:
            triggered.add("news_negative")
            if top is None:
                top = {"title": nws[0]["headline"], "url": nws[0]["url"]}
    return triggered, top


def recompute_all():
    funds = database.get_all_fundamentals()
    companies = {_pad_cik(c["cik"]): c for c in database.get_companies()}

    recs = []
    for f in funds:
        cik = _pad_cik(f["cik"])
        comp = companies.get(cik, {})
        recs.append({
            "cik": cik, "ticker": f.get("ticker"),
            "sector": f.get("sector") or "??",
            "operating_margin": f.get("operating_margin"), "roa": f.get("roa"),
            "revenue_growth": f.get("revenue_growth"), "sga_pct": f.get("sga_pct"),
            "cash_to_assets": f.get("cash_to_assets"), "debt_to_assets": f.get("debt_to_assets"),
            "pb_ratio": comp.get("pb_ratio"), "tsr_1y": comp.get("tsr_1y"),
            "tsr_3y": comp.get("tsr_3y"), "market_cap": comp.get("market_cap"),
            "name": comp.get("name") or f.get("ticker"),
        })

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
        trig = []

        def low(metric):
            q1, _ = t.get(metric, (None, None))
            v = r.get(metric)
            return q1 is not None and v is not None and v <= q1

        def high(metric):
            _, q3 = t.get(metric, (None, None))
            v = r.get(metric)
            return q3 is not None and v is not None and v >= q3

        if r.get("pb_ratio") is not None and 0 < r["pb_ratio"] < 1.5:
            trig.append("cheap_abs")
        elif r.get("pb_ratio") is not None and r["pb_ratio"] > 0 and low("pb_ratio"):
            trig.append("cheap_pb")
        if low("operating_margin"):
            trig.append("low_margin")
        if low("tsr_1y"):
            trig.append("weak_tsr_1y")
        if low("tsr_3y"):
            trig.append("weak_tsr_3y")
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

        struct = sum(STRUCT_POINTS[s] for s in trig)
        events, top = _event_signals(r["cik"], r["ticker"])
        total = struct + sum(EVENT_POINTS[s] for s in events)
        trig += list(events)

        if total < config.SCORE_THRESHOLD:
            continue
        rows.append({
            "cik": r["cik"], "ticker": r["ticker"], "company": r["name"],
            "market_cap": r.get("market_cap"), "score": total,
            "signals": " + ".join(LABELS[s] for s in trig if s in LABELS),
            "top_item_title": top["title"] if top else "",
            "top_item_url": top["url"] if top else "",
            "first_flagged": database.now_iso()[:10],
        })

    rows.sort(key=lambda r: (r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    return rows[: config.SHORTLIST_SIZE]
