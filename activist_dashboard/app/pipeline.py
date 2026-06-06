"""
Orchestration jobs. Market data (Yahoo) is intentionally NOT called here because
Yahoo rate-limits cloud hosts like Render and the failing sweep was crashing the
container before scoring could save. Scoring runs on SEC filings + news.
"""
import time
import traceback

from . import config, database, universe, edgar, news, market, scoring, emailer

_UNIVERSE = None


def get_universe():
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = universe.load_universe()
        for c in _UNIVERSE:
            if c["cik"]:
                database.upsert_company(c["cik"], c["ticker"], c["name"])
    return _UNIVERSE


def refresh_data(max_companies=None):
    """EDGAR filings + news headlines, then rescore. NO Yahoo calls."""
    uni = get_universe()
    try:
        n_news = news.ingest(uni, limit=40)
    except Exception:
        traceback.print_exc()
        n_news = 0
    try:
        n_filings = edgar.ingest(uni, days=config.SCORE_WINDOW_DAYS,
                                 max_companies=max_companies)
    except Exception:
        traceback.print_exc()
        n_filings = 0
    try:
        flagged = scoring.recompute_all()
    except Exception:
        traceback.print_exc()
        flagged = []
    print(f"[refresh] news={n_news} filings={n_filings} flagged={len(flagged)}")
    return {"news": n_news, "filings": n_filings, "flagged": len(flagged)}


def refresh_market_data(max_companies=None):
    """Deliberately disabled on Render (Yahoo blocks cloud IPs). No-op."""
    print("[market] skipped (Yahoo disabled on this host)")
    return 0


def daily_rescore_and_digest():
    """The 4pm-ET job: rescore on fresh filings/news, then send the digest."""
    refresh_data()
    sent = emailer.send_digest()
    return sent


def startup_full_refresh():
    """Runs once after boot. Filings + news only — no Yahoo, no crash."""
    print("[boot] VERSION=no-yahoo  starting filings+news refresh")
    refresh_data()


if __name__ == "__main__":
    database.init_db()
    refresh_data()
