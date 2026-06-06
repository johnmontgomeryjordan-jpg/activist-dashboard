"""
Orchestration: the jobs the scheduler and startup routine call.

  refresh_data()             -- every REFRESH_MINUTES: EDGAR + news + rescore.
  refresh_market_data()      -- market cap / TSR / P-B via Yahoo (slower).
  daily_rescore_and_digest() -- the 4pm ET job: market data, rescore, email.
  startup_full_refresh()     -- once after boot: everything except email.
"""
import time
import traceback

from . import config, database, universe, edgar, news, market, scoring, emailer

# Loaded once; refreshed when the process restarts.
_UNIVERSE = None


def get_universe():
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = universe.load_universe()
        # Seed the companies table so scoring has rows even before market data.
        for c in _UNIVERSE:
            if c["cik"]:
                database.upsert_company(c["cik"], c["ticker"], c["name"])
    return _UNIVERSE


def refresh_data(max_companies=None):
    """Fast refresh: EDGAR filings + news headlines. Called every 30 min."""
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
    # Re-run scoring with whatever data we have.
    try:
        flagged = scoring.recompute_all()
    except Exception:
        traceback.print_exc()
        flagged = []
    print(f"[refresh] news={n_news} filings={n_filings} flagged={len(flagged)}")
    return {"news": n_news, "filings": n_filings, "flagged": len(flagged)}


def refresh_market_data(max_companies=None):
    """Slow refresh: market cap / P:B / TSR via Yahoo. Called once daily."""
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    done = 0
    for c in subset:
        try:
            market.refresh_company(c["cik"], c["ticker"], c["name"])
            done += 1
            time.sleep(0.2)  # be gentle with Yahoo
        except Exception:
            traceback.print_exc()
    print(f"[market] refreshed {done} companies")
    return done


def daily_rescore_and_digest():
    """The 4pm-ET job: refresh market data, rescore, send digest."""
    refresh_market_data()
    refresh_data()
    sent = emailer.send_digest()
    return sent


def startup_full_refresh():
    """
    Run once shortly after the server starts: pull filings + news, then market
    data, then rescore -- so the dashboard shows full, market-informed scores
    within minutes of a deploy instead of waiting for the daily 4pm ET job.
    Does NOT send any email.
    """
    refresh_data()            # filings + news + first-pass scores (fast)
    refresh_market_data()     # market cap / TSR / P-B (slower)
    try:
        flagged = scoring.recompute_all()   # final scores with market signals
        print(f"[startup] full refresh complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    database.init_db()
    refresh_data()
