"""
Financial Modeling Prep (FMP) — company contacts, descriptions, and price history.

Free tier ~250 requests/day, non-commercial. Gated by the FMP_API_KEY env var (absent
key -> these features quietly no-op). Used for:

  * profile  -> CEO, HQ address, phone, website (IR), sector/industry, description.
                Static, so fetched once per company and cached (refreshed monthly).
  * EOD price history -> ~1yr of daily closes for the profile price chart.

FMP moved to a "stable" API; keys created recently may not have access to the legacy
`/api/v3/` endpoints, so we call `/stable/` first and fall back to `/api/v3/`. SEC XBRL
stays the authoritative fundamentals source; FMP only fills the contact / description /
price-chart gaps SEC doesn't serve.
"""
import os
import time

import requests

STABLE = "https://financialmodelingprep.com/stable"
V3 = "https://financialmodelingprep.com/api/v3"
PROFILE_MAX_AGE_DAYS = 30
LAST_ERROR = None          # last FMP error message seen (for log diagnostics)
_session = requests.Session()
_session.headers.update({"User-Agent": "ActivistDashboard/1.0"})


def key():
    return os.getenv("FMP_API_KEY", "")


def _get(url, params):
    """Return parsed JSON, or None. Records FMP error messages in LAST_ERROR."""
    global LAST_ERROR
    for i in range(3):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and ("Error Message" in d or "error" in d):
                    LAST_ERROR = str(d.get("Error Message") or d.get("error"))[:200]
                    return None
                return d
            if r.status_code == 429:
                LAST_ERROR = "HTTP 429 rate-limited"
                time.sleep(1.5 * (i + 1)); continue
            LAST_ERROR = f"HTTP {r.status_code}: {(r.text or '')[:160]}"
            return None
        except (requests.RequestException, ValueError) as e:
            LAST_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(1.0 * (i + 1))
    return None


def _num(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def fetch_profile(symbol, k):
    """Normalized company-profile dict, or None. Tries /stable then /api/v3."""
    d = _get(f"{STABLE}/profile", {"symbol": symbol, "apikey": k})
    if not (isinstance(d, list) and d):
        d = _get(f"{V3}/profile/{symbol}", {"apikey": k})
    if not (isinstance(d, list) and d):
        return None
    p = d[0]
    return {
        "ceo": p.get("ceo") or None,
        "phone": p.get("phone") or None,
        "address": p.get("address") or None,
        "city": p.get("city") or None,
        "state": p.get("state") or None,
        "zip": p.get("zip") or None,
        "country": p.get("country") or None,
        "employees": _num(p.get("fullTimeEmployees")),
        "ipo": p.get("ipoDate") or None,
        "website": p.get("website") or None,
        "sector": p.get("sector") or None,
        "industry": p.get("industry") or None,
        "description": p.get("description") or None,
    }


def _rows_to_closes(rows):
    """rows newest-first with 'price' or 'close' -> closes oldest-first + last."""
    closes = []
    for row in reversed(rows or []):
        c = row.get("price")
        if c is None:
            c = row.get("close")
        try:
            closes.append(round(float(c), 4))
        except (ValueError, TypeError):
            continue
    return closes, (closes[-1] if closes else None)


def fetch_prices(symbol, k, points=260):
    """(closes oldest-first, last_close). Tries /stable EOD-light then /api/v3 full."""
    d = _get(f"{STABLE}/historical-price-eod/light", {"symbol": symbol, "apikey": k})
    if isinstance(d, list) and d:
        return _rows_to_closes(d[:points])
    d = _get(f"{V3}/historical-price-full/{symbol}",
             {"serietype": "line", "timeseries": points, "apikey": k})
    hist = (d or {}).get("historical") if isinstance(d, dict) else None
    if hist:
        return _rows_to_closes(hist)
    return [], None


def refresh_fmp(pairs, db, only_missing_profile=True):
    """pairs: {cik: ticker}. Pulls profile (cached) + price history. `db` is the
    database module (passed in to avoid a circular import)."""
    global LAST_ERROR
    LAST_ERROR = None
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
            p = fetch_profile(tk, k); time.sleep(0.25)
            if p:
                db.upsert_company_profile(cik, p); prof_done += 1
        closes, last = fetch_prices(tk, k); time.sleep(0.25)
        if closes:
            db.upsert_prices(cik, closes, last); px_done += 1
    msg = f"[fmp] profiles {prof_done} · price series {px_done} (of {len(pairs)} names)"
    if prof_done == 0 and px_done == 0 and LAST_ERROR:
        msg += f"  — FMP said: {LAST_ERROR}"
    print(msg)
    return px_done
