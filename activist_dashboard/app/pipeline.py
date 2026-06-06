"""
Orchestration: the jobs the scheduler and startup routine call.

  refresh_data()             -- every REFRESH_MINUTES: EDGAR + news + rescore.
  refresh_market_data()      -- market cap / TSR / P-B via Yahoo (see note).
  daily_rescore_and_digest() -- the 4pm ET job: rescore + email.
  startup_full_refresh()     -- once after boot.

NOTE on market data: Yahoo Finance rate-limits/blocks requests from cloud hosts
like Render (HTTP 429), so the market-based signals are disabled in the hosted
demo and scoring runs on SEC filings + news (the reliable core). To re-enable on
a host Yahoo permits, add `refresh_market_data()` back into the two jobs below.
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
    """Fast refresh: EDGAR filings + news headlines, then rescore."""
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
    """Market cap / P:B / TSR via Yahoo. Currently unused on Render because
    Yahoo blocks cloud IPs; kept for hosts that allow it."""
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    done = 0
    for c in subset:
        try:
            market.refresh_company(c["cik"], c["ticker"], c["name"])
            done += 1
            time.sleep(0.2)
        except Exception:
            traceback.print_exc()
    print(f"[market] refreshed {done} companies")
    return done


def daily_rescore_and_digest():
    """The 4pm-ET job: rescore on fresh filings/news, then send the digest."""
    refresh_data()
    sent = emailer.send_digest()
    return sent


def startup_full_refresh():
    """Run once shortly after boot: pull filings + news and score. Does NOT
    send email and does NOT hit Yahoo (blocked on Render)."""
    refresh_data()


if __name__ == "__main__":
    database.init_db()
    refresh_data()
