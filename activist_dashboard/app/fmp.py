"""
Financial Modeling Prep (FMP) — company contacts, descriptions, and price history.

Free tier ~250 requests/day, non-commercial. We use it for three things, all gated by
the FMP_API_KEY env var (absent key -> these features quietly no-op):

  * /profile      -> CEO, HQ address, phone, website (IR), sector/industry, description.
                     Static, so fetched once per company and cached (refreshed monthly).
  * /historical-price-full -> ~1yr of daily closes for the profile price chart.

We deliberately keep SEC XBRL as the authoritative fundamentals source; FMP only fills
the contact / description / price-chart gaps that SEC doesn't serve.
"""
import os
import time

import requests

V3 = "https://financialmodelingprep.com/api/v3"
PROFILE_MAX_AGE_DAYS = 30          # re-pull a cached profile at most monthly
_session = requests.Session()
_session.headers.update({"User-Agent": "ActivistDashboard/1.0"})


def key():
    return os.getenv("FMP_API_KEY", "")


def _get(url, params):
    for i in range(3):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(1.0 * (i + 1))
    return None


def fetch_profile(symbol, k):
    """Return a normalized company-profile dict, or None."""
    d = _get(f"{V3}/profile/{symbol}", {"apikey": k})
    if not d or not isinstance(d, list) or not d:
        return None
    p = d[0]
    def num(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None
    return {
        "ceo": p.get("ceo") or None,
        "phone": p.get("phone") or None,
        "address": p.get("address") or None,
        "city": p.get("city") or None,
        "state": p.get("state") or None,
        "zip": p.get("zip") or None,
        "country": p.get("country") or None,
        "employees": num(p.get("fullTimeEmployees")),
        "ipo": p.get("ipoDate") or None,
        "website": p.get("website") or None,
        "sector": p.get("sector") or None,
        "industry": p.get("industry") or None,
        "description": p.get("description") or None,
    }


def fetch_prices(symbol, k, points=260):
    """Return (closes[], last_close) of daily closes oldest-first, or ([], None)."""
    d = _get(f"{V3}/historical-price-full/{symbol}",
             {"serietype": "line", "timeseries": points, "apikey": k})
    hist = (d or {}).get("historical") or []
    if not hist:
        return [], None
    # FMP returns newest-first; reverse to oldest-first for charting
    closes = []
    for row in reversed(hist):
        c = row.get("close")
        try:
            closes.append(round(float(c), 4))
        except (ValueError, TypeError):
            continue
    last = closes[-1] if closes else None
    return closes, last


def refresh_fmp(pairs, db, only_missing_profile=True):
    """pairs: {cik: ticker}. Pulls profile (cached) + price history. Returns counts.
    `db` is the database module (passed in to avoid a circular import)."""
    k = key()
    if not k:
        print("[fmp] no FMP_API_KEY set; skipping contacts + price charts")
        return 0
    from datetime import datetime, timedelta
    stale_before = (datetime.utcnow() - timedelta(days=PROFILE_MAX_AGE_DAYS)).isoformat()
    prof_done = px_done = 0
    for cik, tk in pairs.items():
        if not tk:
            continue
        existing = db.get_company_profile(cik)
        need_profile = (not existing) or (existing.get("updated_at") or "") < stale_before
        if need_profile or not only_missing_profile:
            p = fetch_profile(tk, k); time.sleep(0.2)
            if p:
                db.upsert_company_profile(cik, p); prof_done += 1
        closes, last = fetch_prices(tk, k); time.sleep(0.2)
        if closes:
            db.upsert_prices(cik, closes, last); px_done += 1
    print(f"[fmp] profiles {prof_done} · price series {px_done} (of {len(pairs)} names)")
    return px_done
