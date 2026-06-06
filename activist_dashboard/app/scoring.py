"""
Vulnerability scoring model.

Point system (per the brief):

    CEO / C-suite departure (8-K)................. 2
      + stock drops 5%+ on the announcement....... +1 bonus
    Earnings miss or guidance cut (8-K/10-Q)...... 2
    Goodwill impairment / write-down (8-K/10-K)... 2
    Layoff announcement (8-K)..................... 1
    Negative activist/restructuring headline...... 1
    1-year TSR negative........................... 1
    3-year TSR in bottom quartile of universe..... 1
    Low P/B (< 1.5x).............................. 1

Scores use a rolling SCORE_WINDOW_DAYS (default 90) window and are recomputed
daily. Companies at/above SCORE_THRESHOLD (default 3) are flagged. The top
SHORTLIST_SIZE are surfaced as 'Companies to Pitch'.
"""
from . import config, database, market

POINTS = {
    "ceo_departure": 2,
    "ceo_drop_bonus": 1,
    "earnings_miss": 2,
    "impairment": 2,
    "layoffs": 1,
    "news_negative": 1,
    "tsr_1y_negative": 1,
    "tsr_3y_bottom_quartile": 1,
    "low_pb": 1,
}

SIGNAL_LABELS = {
    "ceo_departure": "CEO/exec departure",
    "ceo_drop_bonus": "stock dropped 5%+ on the news",
    "earnings_miss": "earnings miss / guidance cut",
    "impairment": "impairment / write-down",
    "layoffs": "layoffs / restructuring",
    "news_negative": "negative activist headline",
    "tsr_1y_negative": "negative 1-yr TSR",
    "tsr_3y_bottom_quartile": "bottom-quartile 3-yr TSR",
    "low_pb": "low price-to-book",
}


def _bottom_quartile_threshold(companies):
    vals = sorted(c["tsr_3y"] for c in companies if c.get("tsr_3y") is not None)
    if len(vals) < 4:
        return None
    idx = max(0, int(0.25 * len(vals)) - 1)
    return vals[idx]


def score_company(company, window_days, q3_threshold):
    """Return (score:int, triggered_keys:list, top_item:dict|None)."""
    cik = company["cik"]
    ticker = company.get("ticker")
    triggered = []
    top_item = None

    filings = database.filings_in_window(cik, window_days)
    # Each distinct signal type counts once (avoid double-counting repeat filings).
    seen_signals = set()
    for f in filings:
        for sig in (f.get("signals") or "").split(","):
            sig = sig.strip()
            if sig and sig in POINTS and sig not in seen_signals:
                seen_signals.add(sig)
                triggered.append(sig)
                if top_item is None:
                    top_item = {"title": f"{f['company']} — {f['title']}",
                                "url": f["url"]}
        # CEO drop bonus: if this filing is a CEO departure, check price reaction.
        if "ceo_departure" in (f.get("signals") or "") and \
                "ceo_drop_bonus" not in seen_signals and ticker:
            change = market.price_change_on_date(ticker, f["filed_at"])
            if change is not None and change <= -0.05:
                seen_signals.add("ceo_drop_bonus")
                triggered.append("ceo_drop_bonus")

    # News-based signal
    news = database.news_for_ticker_in_window(ticker, window_days) if ticker else []
    if news and "news_negative" not in seen_signals:
        seen_signals.add("news_negative")
        triggered.append("news_negative")
        if top_item is None:
            n = news[0]
            top_item = {"title": n["headline"], "url": n["url"]}

    # Market-based signals
    if company.get("tsr_1y") is not None and company["tsr_1y"] < 0:
        triggered.append("tsr_1y_negative")
    if (q3_threshold is not None and company.get("tsr_3y") is not None
            and company["tsr_3y"] <= q3_threshold):
        triggered.append("tsr_3y_bottom_quartile")
    if company.get("pb_ratio") is not None and 0 < company["pb_ratio"] < 1.5:
        triggered.append("low_pb")

    score = sum(POINTS[s] for s in triggered)
    return score, triggered, top_item


def recompute_all():
    """
    Score every in-universe company, write the shortlist to the scores table.
    Returns the list of flagged company dicts.
    """
    companies = database.get_companies()
    in_universe = [c for c in companies
                   if market.passes_universe_filter(c.get("market_cap"))]
    # If market caps haven't been fetched yet (e.g. first run / offline),
    # fall back to scoring everyone so the demo still produces output.
    pool = in_universe if in_universe else companies

    q3 = _bottom_quartile_threshold(pool)
    rows = []
    for c in pool:
        score, triggered, top = score_company(c, config.SCORE_WINDOW_DAYS, q3)
        if score >= config.SCORE_THRESHOLD:
            rows.append({
                "cik": c["cik"],
                "ticker": c.get("ticker"),
                "company": c.get("name"),
                "market_cap": c.get("market_cap"),
                "score": score,
                "signals": " + ".join(SIGNAL_LABELS[s] for s in triggered),
                "top_item_title": top["title"] if top else "",
                "top_item_url": top["url"] if top else "",
                "first_flagged": database.now_iso()[:10],
            })
    rows.sort(key=lambda r: (r["score"], r["market_cap"] or 0), reverse=True)
    database.replace_scores(rows)
    return rows[: config.SHORTLIST_SIZE]
