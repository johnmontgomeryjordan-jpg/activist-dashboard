"""
Multi-year total shareholder return (3-yr / 5-yr) vs the S&P 500.

A one-year lag can be noise; a stock that has trailed the index for 3-5 years is a
structural underperformer — the core of the "this board has destroyed value for years"
activist argument. We pull a small MONTHLY close series (1 credit/symbol on Twelve Data)
and compute total return from the close closest to N years ago to the latest close, for
the company and for SPY, so the signal is benchmark-relative.

Pure compute (compute_returns) is separated from the network fetch so it can be unit-tested.
"""
from datetime import datetime

from . import twelvedata

BENCH = "SPY"
HORIZONS = (3, 5)
_TOL_DAYS = 75              # accept a monthly point within ~2.5 months of the target date


def _d(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def compute_returns(dated):
    """dated: [(date_str, close)] oldest-first. Returns {'3y': r, '5y': r} (None if the
    history doesn't reach that far). Total return = latest_close / past_close - 1."""
    pts = [(_d(s), c) for (s, c) in dated if _d(s) is not None and c]
    pts.sort(key=lambda x: x[0])
    out = {f"{y}y": None for y in HORIZONS}
    if len(pts) < 2:
        return out
    latest_dt, latest_c = pts[-1]
    for y in HORIZONS:
        target = latest_dt.replace(year=latest_dt.year - y) if _safe_year(latest_dt, y) else None
        if target is None:
            continue
        best, best_gap = None, None
        for dt, c in pts[:-1]:
            gap = abs((dt - target).days)
            if best_gap is None or gap < best_gap:
                best, best_gap = (dt, c), gap
        if best and best_gap is not None and best_gap <= _TOL_DAYS and best[1]:
            out[f"{y}y"] = latest_c / best[1] - 1.0
    return out


def _safe_year(dt, y):
    try:
        dt.replace(year=dt.year - y)
        return True
    except ValueError:                     # Feb 29 edge
        return True


def fetch(symbols, key=None):
    """symbols: list of tickers (include SPY). Returns {symbol: {'3y':r,'5y':r}}."""
    key = key or twelvedata.key()
    if not key or not symbols:
        return {}
    data = twelvedata.fetch_monthly(symbols, key)
    return {sym: compute_returns(series) for sym, series in data.items() if series}
