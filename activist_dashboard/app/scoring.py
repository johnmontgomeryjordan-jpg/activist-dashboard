"""
Predictive target-attractiveness scoring.

Scores every company on the STRUCTURAL traits activists screen for when picking
targets -- measured RELATIVE TO INDUSTRY PEERS (same 2-digit SIC group) using
free SEC XBRL fundamentals -- plus event "accelerants" that say pitch them now.

  Structural (peer-relative)
    Operating margin in bottom quartile of sector ........ 2
    ROA in bottom quartile of sector ..................... 1
    Revenue shrinking or bottom-quartile growth .......... 1
    SG&A % of revenue in top quartile (bloated costs) .... 1
    Cash/assets in top quartile (lazy, cash-hoarding) .... 1
    Debt/assets in bottom quartile (under-levered) ....... 1
  Event accelerants (recent filings / news)
    CEO/exec departure, earnings miss, impairment,
    layoffs, negative activist headline .................. 1 each

Flagged at/above SCORE_THRESHOLD; top SHORTLIST_SIZE surfaced. Peer quartiles
need >= MIN_PEERS companies with the metric or the signal is skipped.
Valuation & total-return signals arrive in Phase 2 (free Stooq prices).
"""
from . import config, database

MIN_PEERS = 5

STRUCT_POINTS = {"low_margin": 2, "low_roa": 1, "weak_growth": 1,
                 "high_sga": 1, "cash_hoard": 1, "underlevered": 1}
EVENT_POINTS = {"ceo_departure": 1, "earnings_miss": 1, "impairment": 1,
                "layoffs": 1, "news_negative": 1}

LABELS = {
    "low_margin": "low margin vs peers",
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


def _sector_thresholds(funds):
    by_sector = {}
    for f in funds:
        by_sector.setdefault(f.get("sector") or "??", []).append(f)
    metrics = ["operating_margin", "roa", "revenue_growth", "sga_pct",
               "cash_to_assets", "debt_to_assets"]
    th = {}
    for sec, rows in by_sector.items():
        th[sec] = {m: _quantiles([r.get(m) for r in rows]) for m in metrics}
    return th


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
    th = _sector_thresholds(funds)

    rows = []
    for f in funds:
        t = th.get(f.get("sector") or "??", {})
        triggered = []

        def q(metric):
            return t.get(metric, (None, None))

        om_q1, _ = q("operating_margin")
        if om_q1 is not None and f.get("operating_margin") is not None and f["operating_margin"] <= om_q1:
            triggered.append("low_margin")
        roa_q1, _ = q("roa")
        if roa_q1 is not None and f.get("roa") is not None and f["roa"] <= roa_q1:
            triggered.append("low_roa")
        g_q1, _ = q("revenue_growth")
        if f.get("revenue_growth") is not None and (
                f["revenue_growth"] < 0 or (g_q1 is not None and f["revenue_growth"] <= g_q1)):
            triggered.append("weak_growth")
        _, sga_q3 = q("sga_pct")
        if sga_q3 is not None and f.get("sga_pct") is not None and f["sga_pct"] >= sga_q3:
            triggered.append("high_sga")
        _, cash_q3 = q("cash_to_assets")
        if cash_q3 is not None and f.get("cash_to_assets") is not None and f["cash_to_assets"] >= cash_q3:
            triggered.append("cash_hoard")
        d_q1, _ = q("debt_to_assets")
        if d_q1 is not None and f.get("debt_to_assets") is not None and f["debt_to_assets"] <= d_q1:
            triggered.append("underlevered")

        struct_score = sum(STRUCT_POINTS[s] for s in triggered)

        cik, ticker = f["cik"], f.get("ticker")
        events, top = _event_signals(cik, ticker)
        total = struct_score + sum(EVENT_POINTS[s] for s in events)
        triggered += list(events)

        if total < config.SCORE_THRESHOLD:
            continue
        comp = companies.get(_pad_cik(cik), {})
        rows.append({
            "cik": cik, "ticker": ticker,
            "company": comp.get("name") or ticker,
            "market_cap": comp.get("market_cap"),
            "score": total,
            "signals": " + ".join(LABELS[s] for s in triggered if s in LABELS),
            "top_item_title": top["title"] if top else "",
            "top_item_url": top["url"] if top else "",
            "first_flagged": database.now_iso()[:10],
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    database.replace_scores(rows)
    return rows[: config.SHORTLIST_SIZE]
