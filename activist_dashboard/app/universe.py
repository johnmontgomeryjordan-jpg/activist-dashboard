"""
Defines the company universe we monitor.

A bundled CSV (app/universe.csv) lists the tickers to watch. At startup we join
those tickers to their SEC CIK numbers using the official, free mapping at
https://www.sec.gov/files/company_tickers.json so EDGAR lookups work.

The bundled list is the full S&P 1500 (S&P 500 + 400 + 600), the brief's suggested
practical proxy. The $1B market-cap filter is applied on top.

U1 (Jul 2026): the CSV is now kept fresh automatically. rebuild_universe_csv()
refreshes S&P 1500 membership weekly from the free iShares core ETF holdings feeds
(IVV / IJH / IJR) so names never silently go stale (e.g., Coterra). It FAILS SAFE:
the committed universe.csv is the fallback and is overwritten only when a rebuild
passes size + churn sanity gates. The rebuild is a scheduled weekly job, never part
of startup -- boot always reads whatever CSV is already on disk.
"""
import csv
import json
import os
import tempfile

import requests

from . import config

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
HEADERS = {"User-Agent": config.SEC_USER_AGENT}

# --- U1: membership feeds + sanity thresholds ------------------------------
# Free daily holdings CSVs from BlackRock. If BlackRock ever changes these URLs,
# the rebuild fails safe (keeps the committed CSV) and logs -- nothing breaks.
ISHARES_HOLDINGS = {
    "SP500": ("https://www.ishares.com/us/products/239726/"
              "ishares-core-sp-500-etf/1467271812596.ajax"
              "?fileType=csv&fileName=IVV_holdings&dataType=fund"),
    "SP400": ("https://www.ishares.com/us/products/239763/"
              "ishares-core-sp-midcap-etf/1467271812596.ajax"
              "?fileType=csv&fileName=IJH_holdings&dataType=fund"),
    "SP600": ("https://www.ishares.com/us/products/239774/"
              "ishares-core-sp-smallcap-etf/1467271812596.ajax"
              "?fileType=csv&fileName=IJR_holdings&dataType=fund"),
}
MIN_UNIVERSE = 1400   # reject a rebuild that returns fewer names than this
MAX_CHURN = 0.08      # reject a rebuild whose add+drop churn exceeds this share
_MIN_LEG_ROWS = 300   # each S&P leg should have hundreds of equity rows

_cik_cache = None


def _load_sec_ticker_map():
    """Return {TICKER: (cik_str, title)} from SEC. Cached in memory."""
    global _cik_cache
    if _cik_cache is not None:
        return _cik_cache
    try:
        r = requests.get(SEC_TICKERS_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        _cik_cache = {}
        return _cik_cache
    out = {}
    for row in data.values():
        out[row["ticker"].upper()] = (str(row["cik_str"]), row["title"])
    _cik_cache = out
    return out


def load_universe():
    """
    Read universe.csv and return a list of {cik, ticker, name} dicts.
    Tickers without a CIK match are skipped (logged to stdout).
    """
    sec_map = _load_sec_ticker_map()
    rows = []
    try:
        with open(config.UNIVERSE_CSV, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                ticker = (r.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                name = (r.get("name") or "").strip()
                match = sec_map.get(ticker)
                if match:
                    cik, title = match
                    rows.append({"cik": cik, "ticker": ticker,
                                 "name": name or title})
                else:
                    # Keep it with a placeholder CIK so market data still works,
                    # but EDGAR lookups will no-op.
                    if name:
                        rows.append({"cik": "", "ticker": ticker, "name": name})
    except FileNotFoundError:
        print(f"[universe] CSV not found at {config.UNIVERSE_CSV}")
        return []
    return rows


# --- U1: weekly auto-rebuild ------------------------------------------------
def _norm_ticker(t):
    """Match SEC convention: uppercase, dots -> dashes (BRK.B -> BRK-B)."""
    return (t or "").strip().upper().replace(".", "-")


def _parse_ishares_csv(text):
    """Yield (ticker, name) for equity rows in an iShares holdings CSV.

    The file has ~9 metadata lines, then a blank line, then a header row that
    begins 'Ticker,Name,Sector,Asset Class,...'. We locate that header and read
    equity rows only (skipping cash/derivative lines, which use a '-' ticker).
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip("﻿").strip().lower().startswith("ticker,"):
            start = i
            break
    if start is None:
        return []
    out = []
    for row in csv.DictReader(lines[start:]):
        tk = _norm_ticker(row.get("Ticker"))
        ac = (row.get("Asset Class") or "").strip().lower()
        nm = (row.get("Name") or "").strip()
        if not tk or tk == "-" or ac != "equity":
            continue
        out.append((tk, nm))
    return out


def _fetch_membership():
    """Union S&P 500/400/600 constituents from iShares.
    Returns {ticker: name}, or {} on ANY failure (which aborts the rebuild)."""
    members = {}
    for label, url in ISHARES_HOLDINGS.items():
        try:
            r = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT},
                             timeout=30)
            r.raise_for_status()
            rows = _parse_ishares_csv(r.text)
        except (requests.RequestException, ValueError) as e:
            print(f"[universe] iShares fetch failed for {label}: {e}")
            return {}
        if len(rows) < _MIN_LEG_ROWS:
            print(f"[universe] iShares {label} returned only {len(rows)} rows; "
                  "aborting rebuild")
            return {}
        for tk, nm in rows:
            members.setdefault(tk, nm)
    return members


def ishares_membership():
    """Return {NORMALIZED_TICKER: "SP500"|"SP400"|"SP600"} from the iShares feeds, so
    the entity master (U2) can tag index membership with no extra HTTP beyond U1's fetch.
    First index wins (dict is ordered 500 -> 400 -> 600). Empty dict on any failure."""
    out = {}
    for label, url in ISHARES_HOLDINGS.items():
        try:
            r = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT},
                             timeout=30)
            r.raise_for_status()
            for tk, _nm in _parse_ishares_csv(r.text):
                out.setdefault(tk, label)
        except (requests.RequestException, ValueError):
            continue
    return out


def _read_current_csv():
    """Return {ticker: name} from the current committed/on-disk CSV."""
    out = {}
    try:
        with open(config.UNIVERSE_CSV, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                tk = _norm_ticker(r.get("ticker"))
                if tk:
                    out[tk] = (r.get("name") or "").strip()
    except FileNotFoundError:
        pass
    return out


def _display_name(tk, members, current):
    """Prefer the stable existing display name; else title-case the iShares name."""
    cur = current.get(tk)
    if cur:
        return cur
    nm = members.get(tk, "")
    return nm.title() if nm.isupper() else nm


def rebuild_universe_csv(force=False):
    """Weekly refresh of universe.csv from iShares S&P 1500 membership.

    Fails safe: returns False and leaves the existing CSV untouched on any fetch
    failure or sanity-gate rejection. Returns True only after an atomic overwrite.

    force=True bypasses ONLY the churn gate (use once, after reviewing the logged
    diff, if the hand-cut list has drifted). It never bypasses the fetch-failure or
    minimum-size gates, so a forced rebuild still cannot empty the universe.
    """
    members = _fetch_membership()
    if not members:
        print("[universe] rebuild aborted (fetch failure); keeping existing CSV")
        return False

    if len(members) < MIN_UNIVERSE:
        print(f"[universe] rebuild REJECTED: {len(members)} names < "
              f"{MIN_UNIVERSE}; keeping existing CSV")
        return False

    current = _read_current_csv()
    if current:
        new_set, old_set = set(members), set(current)
        churn = len(new_set ^ old_set) / max(len(old_set), 1)
        if churn > MAX_CHURN and not force:
            print(f"[universe] rebuild REJECTED: churn {churn:.1%} > "
                  f"{MAX_CHURN:.0%} vs current ({len(old_set)} names); "
                  "keeping existing CSV. Review the diff, then rebuild with "
                  "force=True to accept.")
            return False
        if churn > MAX_CHURN and force:
            print(f"[universe] churn {churn:.1%} accepted via force=True")
        added = sorted(new_set - old_set)
        dropped = sorted(old_set - new_set)
    else:
        added, dropped = sorted(members), []

    rows = sorted(((tk, _display_name(tk, members, current)) for tk in members),
                  key=lambda x: x[0])

    # Atomic write: temp file in the same dir, validate, then os.replace().
    try:
        d = os.path.dirname(os.path.abspath(config.UNIVERSE_CSV)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ticker", "name"])
            w.writerows(rows)
        with open(tmp, newline="", encoding="utf-8") as fh:
            n = sum(1 for _ in csv.DictReader(fh))
        if n < MIN_UNIVERSE:
            os.unlink(tmp)
            print("[universe] temp file failed validation; aborting rebuild")
            return False
        os.replace(tmp, config.UNIVERSE_CSV)
    except OSError as e:
        print(f"[universe] rebuild write failed ({e}); keeping existing CSV")
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except (OSError, NameError):
            pass
        return False

    def _preview(lst):
        return f"{lst[:15]}{'...' if len(lst) > 15 else ''}"

    print(f"[universe] rebuilt: {len(rows)} names "
          f"(+{len(added)} / -{len(dropped)}). "
          f"added={_preview(added)} dropped={_preview(dropped)}")
    return True
