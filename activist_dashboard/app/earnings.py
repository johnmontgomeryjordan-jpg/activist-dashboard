"""
Earnings calendar (timing layer) via Finnhub's free /calendar/earnings endpoint.

This is NOT a vulnerability signal -- it's a timing tool. A target is most receptive to
an outreach right around a soft quarter, so surfacing the next (and last) earnings date
helps a partner choose *when* to reach out. Free, non-commercial tier.
"""
import os
import time
from datetime import datetime, timedelta

import requests

CAL_URL = "https://finnhub.io/api/v1/calendar/earnings"


def _get(symbol, frm, to, key):
    try:
        r = requests.get(CAL_URL, params={"from": frm, "to": to, "symbol": symbol,
                                           "token": key}, timeout=20)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def next_and_last(symbol, key):
    """Return (next_date, last_date) ISO strings for a ticker, or (None, None)."""
    today = datetime.utcnow().date()
    fwd = _get(symbol, today.isoformat(), (today + timedelta(days=180)).isoformat(), key)
    back = _get(symbol, (today - timedelta(days=180)).isoformat(),
                today.isoformat(), key)
    def dates(j):
        rows = (j or {}).get("earningsCalendar", []) or []
        return sorted(d.get("date") for d in rows if d.get("date"))
    fwd_d = dates(fwd)
    back_d = dates(back)
    next_date = fwd_d[0] if fwd_d else None
    last_date = back_d[-1] if back_d else None
    return next_date, last_date


def refresh_earnings(pairs):
    """pairs: {cik: ticker}. Stores next/last earnings dates. Needs FINNHUB_API_KEY."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        print("[earnings] no FINNHUB_API_KEY set; skipping")
        return 0
    done = 0
    for cik, ticker in pairs.items():
        if not ticker:
            continue
        nxt, last = next_and_last(ticker, key)
        time.sleep(0.25)
        if nxt or last:
            from . import database
            database.upsert_earnings(cik, ticker, nxt, last)
            done += 1
    print(f"[earnings] updated {done}/{len(pairs)} names")
    return done
