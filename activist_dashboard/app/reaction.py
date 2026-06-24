"""
Exec-change stock reaction: the 1-day move vs the S&P on the day a CEO/leadership-change
8-K hit. "The stock fell 9% the day they announced the CEO was out" is a sharp, dated,
defensible signal of a market that has lost confidence — exactly the moment an activist
circles.

We pull a small DATED price window around the filing from Twelve Data (these events are
rare, so it's a handful of calls), find the event day (first trading day on/after the
filing) and the prior trading day, and compute:

    move        = close[event] / close[event-1] - 1          (the stock's 1-day return)
    bench_move  = SPY[event]   / SPY[event-1]   - 1          (the market that day)
    abnormal    = move - bench_move                          (move attributable to the news)

abnormal isolates the company-specific reaction from a broad up/down market day.
"""
from datetime import datetime, timedelta

from . import twelvedata

BENCH = "SPY"


def _d(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _one_day_move(pairs, event_dt):
    """pairs: [(date_str, close)] oldest-first. Returns (event_date, ret) for the first
    trading day on/after event_dt vs the close before it, or (None, None)."""
    if not pairs or event_dt is None:
        return None, None
    dated = [(d, c) for (d, c) in ((p[0], p[1]) for p in pairs) if _d(d)]
    dated.sort(key=lambda x: x[0])
    for i in range(1, len(dated)):
        if _d(dated[i][0]) >= event_dt:
            prev_c, cur_c = dated[i - 1][1], dated[i][1]
            if prev_c:
                return dated[i][0], (cur_c / prev_c - 1.0)
            return dated[i][0], None
    return None, None


def fetch_reaction(ticker, filed_at, key=None):
    """Compute the abnormal 1-day reaction for an exec-change 8-K. Returns a dict or None."""
    key = key or twelvedata.key()
    if not key or not ticker:
        return None
    ev = _d(filed_at)
    if ev is None:
        return None
    start = (ev - timedelta(days=8)).strftime("%Y-%m-%d")
    end = (ev + timedelta(days=6)).strftime("%Y-%m-%d")
    data = twelvedata.fetch_dated([ticker, BENCH], key, start, end)
    co = data.get(ticker) or []
    bm = data.get(BENCH) or []
    edate, move = _one_day_move(co, ev)
    _, bench_move = _one_day_move(bm, ev)
    if move is None:
        return None
    abnormal = move - (bench_move or 0.0)
    return {"event_date": edate, "move": move,
            "bench_move": bench_move, "abnormal": abnormal}
