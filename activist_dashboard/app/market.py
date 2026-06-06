"""
Market data via the free `yfinance` library (Yahoo Finance).

Provides, per ticker:
  * market cap (for universe filtering)
  * price-to-book ratio
  * 1-year and 3-year total shareholder return (approximated by price return;
    Yahoo's adjusted close already folds in dividends, so this is true TSR)

Yahoo's endpoints are unofficial and occasionally rate-limit or return empty
data, so every call is defensive and returns None on failure rather than raising.
"""
from datetime import datetime, timedelta

from . import config

try:
    import yfinance as yf
except ImportError:  # allows the rest of the app to import even if yfinance absent
    yf = None


def get_fundamentals(ticker):
    """Return dict with market_cap and pb_ratio (values may be None)."""
    out = {"market_cap": None, "pb_ratio": None}
    if yf is None:
        return out
    try:
        info = yf.Ticker(ticker).info or {}
        out["market_cap"] = info.get("marketCap")
        out["pb_ratio"] = info.get("priceToBook")
    except Exception:
        pass
    return out


def get_tsr(ticker, years):
    """Total shareholder return over `years`, as a fraction (0.10 == +10%)."""
    if yf is None:
        return None
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=int(365.25 * years) + 5)
        hist = yf.Ticker(ticker).history(start=start.date(), end=end.date(),
                                         auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first
    except Exception:
        return None


def price_change_on_date(ticker, date_str):
    """
    Single-day percentage price change on `date_str` (YYYY-MM-DD).
    Used for the 'stock drops 5%+ on CEO change' bonus point.
    Returns a fraction or None.
    """
    if yf is None:
        return None
    try:
        d = datetime.fromisoformat(date_str).date()
        start = d - timedelta(days=4)
        end = d + timedelta(days=2)
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        # find the row on/after the announcement date
        closes = hist["Close"].tolist()
        # prior close vs the close on/after the date
        prev = float(closes[-2])
        cur = float(closes[-1])
        if prev <= 0:
            return None
        return (cur - prev) / prev
    except Exception:
        return None


def refresh_company(cik, ticker, name):
    """Fetch fundamentals + TSR for one company and persist to DB."""
    from . import database
    f = get_fundamentals(ticker)
    tsr_1y = get_tsr(ticker, 1)
    tsr_3y = get_tsr(ticker, 3)
    database.upsert_company(
        cik=cik, ticker=ticker, name=name,
        market_cap=f["market_cap"], pb_ratio=f["pb_ratio"],
        tsr_1y=tsr_1y, tsr_3y=tsr_3y,
    )
    return f["market_cap"]


def passes_universe_filter(market_cap):
    return market_cap is not None and market_cap >= config.MIN_MARKET_CAP
