"""
Market data module — DISABLED on this host.

Yahoo Finance rate-limits/blocks requests from cloud hosts like Render, and the
failing calls were spamming logs, slowing startup, and crashing the container.
So every function here returns "no data" instantly without making any network
call. Scoring therefore runs on SEC filings + news (the reliable core).

The function names/signatures are unchanged, so nothing else needs editing. To
re-enable real market data on a host that allows Yahoo (or with a paid data
provider), restore the yfinance-backed implementations of these functions.
"""
from . import config


def get_fundamentals(ticker):
    """Market cap + price-to-book. Disabled -> returns no data."""
    return {"market_cap": None, "pb_ratio": None}


def get_tsr(ticker, years):
    """Total shareholder return over `years`. Disabled -> None."""
    return None


def price_change_on_date(ticker, date_str):
    """1-day price change on a date. Disabled -> None."""
    return None


def refresh_company(cik, ticker, name):
    """No-op: records the company with no market data (no network call)."""
    from . import database
    database.upsert_company(cik=cik, ticker=ticker, name=name)
    return None


def passes_universe_filter(market_cap):
    return market_cap is not None and market_cap >= config.MIN_MARKET_CAP
