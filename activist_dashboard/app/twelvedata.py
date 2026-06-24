"""
Twelve Data — daily price history for the profile price chart (free; non-commercial).

Free tier: 800 credits/day (1 credit per symbol), 8 requests/minute. We try BATCH
requests first (comma-separated symbols, one HTTP call) and fall back to one-per-symbol
if batch isn't available on the tier. Gated by TWELVEDATA_API_KEY (absent key -> charts
stay empty). Any failure is captured in LAST_ERROR and printed so problems are visible.

Licensing: the free tier is "internal, non-display" use. Fine for building / internal
review; a client-facing deployment needs a paid market-data plan.
"""
import os
import time

import requests

URL = "https://api.twelvedata.com/time_series"
BATCH = 25
RATE_SLEEP = 8.0           # per-symbol fallback: free tier = 8 req/min
LAST_ERROR = None
_session = requests.Session()
_session.headers.update({"User-Agent": "ActivistDashboard/1.0"})


def key():
    return os.getenv("TWELVEDATA_API_KEY", "")


def _request(symbols, k, points=260):
    """GET /time_series for one or more symbols. Returns parsed JSON or None, capturing
    the failure reason (HTTP status, body, or API error message) in LAST_ERROR."""
    global LAST_ERROR
    try:
        r = _session.get(URL, params={"symbol": ",".join(symbols), "interval": "1day",
                                      "outputsize": points, "apikey": k}, timeout=25)
    except requests.RequestException as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
        return None
    if r.status_code != 200:
        LAST_ERROR = f"HTTP {r.status_code}: {(r.text or '')[:180]}"
        return None
    try:
        d = r.json()
    except ValueError:
        LAST_ERROR = "non-JSON response"
        return None
    if isinstance(d, dict) and d.get("status") == "error":
        LAST_ERROR = str(d.get("message"))[:180]
        return None
    return d


def _closes_from(obj):
    vals = (obj or {}).get("values") or []
    closes = []
    for row in reversed(vals):            # Twelve Data returns newest-first
        try:
            closes.append(round(float(row.get("close")), 4))
        except (ValueError, TypeError):
            continue
    return closes, (closes[-1] if closes else None)


def _fetch_chunk(symbols, k):
    """Return {symbol: (closes, last)} for a batch of symbols (one request)."""
    global LAST_ERROR
    d = _request(symbols, k)
    if d is None:
        return {}
    out = {}
    if len(symbols) == 1:
        obj = d if "values" in d else (d.get(symbols[0]) or {})
        out[symbols[0]] = _closes_from(obj)
        return out
    for sym in symbols:
        obj = (d or {}).get(sym)
        if isinstance(obj, dict) and obj.get("status") == "error":
            LAST_ERROR = f"{sym}: {str(obj.get('message'))[:120]}"
            continue
        if obj:
            out[sym] = _closes_from(obj)
    return out


def fetch_dated(symbols, k, start_date, end_date):
    """Return {symbol: [(date, close), ...]} (oldest-first) over a date window. Used for
    point-in-time event studies (e.g. the 1-day move around an 8-K filing). One HTTP call."""
    global LAST_ERROR
    try:
        r = _session.get(URL, params={"symbol": ",".join(symbols), "interval": "1day",
                                      "start_date": start_date, "end_date": end_date,
                                      "apikey": k}, timeout=25)
    except requests.RequestException as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
        return {}
    if r.status_code != 200:
        LAST_ERROR = f"HTTP {r.status_code}: {(r.text or '')[:180]}"
        return {}
    try:
        d = r.json()
    except ValueError:
        LAST_ERROR = "non-JSON response"
        return {}

    def rows_to_pairs(obj):
        vals = (obj or {}).get("values") or []
        out = []
        for row in reversed(vals):                 # API returns newest-first
            try:
                out.append((row.get("datetime"), round(float(row.get("close")), 4)))
            except (ValueError, TypeError):
                continue
        return out

    out = {}
    if len(symbols) == 1:
        obj = d if "values" in d else (d.get(symbols[0]) or {})
        out[symbols[0]] = rows_to_pairs(obj)
    else:
        for sym in symbols:
            obj = (d or {}).get(sym)
            if isinstance(obj, dict) and obj.get("status") == "error":
                LAST_ERROR = f"{sym}: {str(obj.get('message'))[:120]}"
                continue
            out[sym] = rows_to_pairs(obj)
    return out


def fetch_monthly(symbols, k, points=80):
    """Return {symbol: [(date, close), ...]} (oldest-first) of MONTHLY closes — a tiny
    payload (1 credit/symbol) that's enough for multi-year (3/5-yr) total-return math."""
    global LAST_ERROR
    try:
        r = _session.get(URL, params={"symbol": ",".join(symbols), "interval": "1month",
                                      "outputsize": points, "apikey": k}, timeout=25)
    except requests.RequestException as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
        return {}
    if r.status_code != 200:
        LAST_ERROR = f"HTTP {r.status_code}: {(r.text or '')[:180]}"
        return {}
    try:
        d = r.json()
    except ValueError:
        LAST_ERROR = "non-JSON response"
        return {}

    def rows_to_pairs(obj):
        vals = (obj or {}).get("values") or []
        out = []
        for row in reversed(vals):                 # API returns newest-first
            try:
                out.append((row.get("datetime"), round(float(row.get("close")), 4)))
            except (ValueError, TypeError):
                continue
        return out

    out = {}
    if len(symbols) == 1:
        obj = d if "values" in d else (d.get(symbols[0]) or {})
        out[symbols[0]] = rows_to_pairs(obj)
    else:
        for sym in symbols:
            obj = (d or {}).get(sym)
            if isinstance(obj, dict) and obj.get("status") == "error":
                LAST_ERROR = f"{sym}: {str(obj.get('message'))[:120]}"
                continue
            out[sym] = rows_to_pairs(obj)
    return out


def refresh_prices(pairs, db):
    """pairs: {cik: ticker}. Stores a daily-close series per name. Tries batch first;
    if batch yields nothing, falls back to one request per symbol. `db` is the database
    module (passed in to avoid a circular import)."""
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

    def store(results):
        n = 0
        for sym, (closes, last) in results.items():
            if closes:
                db.upsert_prices(sym_to_cik[sym], closes, last); n += 1
        return n

    done = 0
    for i in range(0, len(symbols), BATCH):
        done += store(_fetch_chunk(symbols[i:i + BATCH], k))
        time.sleep(1.0)

    mode = "batched"
    if done == 0 and symbols:             # batch unsupported? fall back to per-symbol
        mode = "per-symbol"
        for sym in symbols:
            done += store(_fetch_chunk([sym], k))
            time.sleep(RATE_SLEEP)

    msg = f"[prices] {done}/{len(symbols)} via Twelve Data ({mode})"
    if done == 0 and LAST_ERROR:
        msg += f"  — said: {LAST_ERROR}"
    print(msg)
    return done
