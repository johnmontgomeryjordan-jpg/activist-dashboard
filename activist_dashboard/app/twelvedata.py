"""
Twelve Data — daily price history for the profile price chart (free; non-commercial).

Free tier: 800 credits/day (1 credit per symbol), 8 requests/minute. We use /time_series
with BATCH requests (comma-separated symbols) so all tracked names come back in one or
two HTTP calls instead of one-per-name -- fast, and it never trips the 8-requests/min
limit. Gated by the TWELVEDATA_API_KEY env var (absent key -> charts stay empty).

Note on licensing: the free tier is "internal, non-display" use. Fine for building and
internal review; a client-facing/commercial deployment needs a paid market-data plan.
"""
import os
import time

import requests

URL = "https://api.twelvedata.com/time_series"
BATCH = 25                 # symbols per request (well within batch limits)
LAST_ERROR = None
_session = requests.Session()
_session.headers.update({"User-Agent": "ActivistDashboard/1.0"})


def key():
    return os.getenv("TWELVEDATA_API_KEY", "")


def _closes_from(obj):
    """One symbol's payload -> (closes oldest-first, last_close)."""
    vals = (obj or {}).get("values") or []
    closes = []
    for row in reversed(vals):            # Twelve Data returns newest-first
        try:
            closes.append(round(float(row.get("close")), 4))
        except (ValueError, TypeError):
            continue
    return closes, (closes[-1] if closes else None)


def _fetch_batch(symbols, k, points=260):
    """Return {symbol: (closes, last)} for a list of symbols in ONE request."""
    global LAST_ERROR
    try:
        r = _session.get(URL, params={"symbol": ",".join(symbols), "interval": "1day",
                                      "outputsize": points, "apikey": k}, timeout=25)
        d = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError) as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
        return {}
    if isinstance(d, dict) and d.get("status") == "error":
        LAST_ERROR = str(d.get("message"))[:160]
        return {}
    out = {}
    if len(symbols) == 1:
        # single-symbol responses are returned flat (not keyed by symbol)
        if isinstance(d, dict) and d.get("status") == "error":
            LAST_ERROR = str(d.get("message"))[:160]
        else:
            out[symbols[0]] = _closes_from(d)
        return out
    for sym in symbols:
        obj = (d or {}).get(sym)
        if isinstance(obj, dict) and obj.get("status") == "error":
            LAST_ERROR = str(obj.get("message"))[:160]
            continue
        if obj:
            out[sym] = _closes_from(obj)
    return out


def refresh_prices(pairs, db):
    """pairs: {cik: ticker}. Stores a daily-close series per name for the chart, using
    batch requests. `db` is the database module (passed in to avoid a circular import)."""
    global LAST_ERROR
    LAST_ERROR = None
    k = key()
    if not k:
        print("[prices] no TWELVEDATA_API_KEY set; skipping price charts")
        return 0
    sym_to_cik = {}
    for cik, tk in pairs.items():
        if tk:
            sym_to_cik.setdefault(tk, cik)
    symbols = list(sym_to_cik)
    done = 0
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        results = _fetch_batch(chunk, k)
        for sym, (closes, last) in results.items():
            if closes:
                db.upsert_prices(sym_to_cik[sym], closes, last)
                done += 1
        time.sleep(1.0)                   # gentle gap between batches
    msg = f"[prices] {done}/{len(symbols)} via Twelve Data (batched)"
    if done == 0 and LAST_ERROR:
        msg += f"  — said: {LAST_ERROR}"
    print(msg)
    return done
