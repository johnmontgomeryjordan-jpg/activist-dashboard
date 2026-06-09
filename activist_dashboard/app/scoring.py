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

Each triggered signal also produces an EVIDENCE record (value + peer context +
source + optional link), stored as JSON on the score so the detail view can show
exactly why each tag fired.
"""
from . import config, database

MIN_PEERS = 5

STRUCT_POINTS = {"cheap_abs": 2, "cheap_pb": 2, "low_margin": 2, "weak_tsr_1y": 1,
                 "weak_tsr_3y": 1, "low_roa": 1, "weak_growth": 1, "high_sga": 1,
                 "cash_hoard": 1, "underlevered": 1}
EVENT_POINTS = {"ceo_departure": 2, "earnings_miss": 2, "impairment": 2,
                "layoffs": 1, "leadership_change": 1, "results_update": 0,
                "news_negative": 1}

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
    "leadership_change": "recent leadership change",
    "results_update": "recent results",
    "news_negative": "negative activist headline",
}

# Structural signal -> (metric, direction, source label) for evidence.
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
EVENT_SOURCE = {"ceo_departure": "SEC 8-K", "earnings_miss": "SEC 8-K",
                "impairment": "SEC 8-K", "layoffs": "SEC 8-K",
                "leadership_change": "SEC 8-K", "results_update": "SEC 8-K",
                "news_negative": "News"}


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


def _fmt_metric(metric, v):
    if v is None:
        return "n/a"
    if metric == "pb_ratio":
        return f"{v:.2f}x"
    if metric in PCT_METRICS:
        return f"{v * 100:.1f}%"
    return f"{v:.2f}"


def _struct_evidence(key, r, t):
    metric, direction, source = STRUCT_META[key]
    v = r.get(metric)
    q1, q3 = t.get(metric, (None, None))
    if key == "cheap_abs":
        ctx = "trades below 1.5x book value"
    elif key == "weak_growth" and v is not None and v < 0:
        ctx = "revenue is shrinking year over year"
    elif direction == "low":
        ctx = (f"bottom 25% of sector peers (peer cutoff {_fmt_metric(metric, q1)})"
               if q1 is not None else "bottom quartile of sector peers")
    elif direction == "high":
        ctx = (f"top 25% of sector peers (peer cutoff {_fmt_metric(metric, q3)})"
               if q3 is not None else "top quartile of sector peers")
    else:
        ctx = ""
    return {"key": key, "label": LABELS.get(key, key), "value": _fmt_metric(metric, v),
            "context": ctx, "source": source, "url": None}


def _event_evidence(key, ev):
    item = ev.get(key)
    return {"key": key, "label": LABELS.get(key, key), "value": "",
            "context": (item["title"] if item else ""),
            "source": EVENT_SOURCE.get(key, "EDGAR"),
            "url": (item["url"] if item else None)}


def _event_signals(cik, ticker):
    triggered = set()
    top = None
    ev = {}
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
    return triggered, top, ev


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
        events, top, ev = _event_signals(r["cik"], r["ticker"])
        total = struct + sum(EVENT_POINTS[s] for s in events)
        trig += list(events)

        if total < config.SCORE_THRESHOLD:
            continue

        evidence = []
        for key in trig:
            if key in STRUCT_META:
                evidence.append(_struct_evidence(key, r, t))
            elif key in EVENT_POINTS:
                evidence.append(_event_evidence(key, ev))

        rows.append({
            "cik": r["cik"], "ticker": r["ticker"], "company": r["name"],
            "market_cap": r.get("market_cap"), "score": total,
            "signals": " + ".join(LABELS[s] for s in trig if s in LABELS),
            "top_item_title": top["title"] if top else "",
            "top_item_url": top["url"] if top else "",
            "evidence": evidence,
            "first_flagged": database.now_iso()[:10],
        })

    rows.sort(key=lambda r: (r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    return rows[: config.SHORTLIST_SIZE]
