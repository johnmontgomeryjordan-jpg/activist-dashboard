"""
Defines the company universe we monitor.

A small bundled CSV (app/universe.csv) lists the tickers to watch. At startup we
join those tickers to their SEC CIK numbers using the official, free mapping at
https://www.sec.gov/files/company_tickers.json so EDGAR lookups work.

To monitor more companies, simply add rows to universe.csv -- no code changes.
The bundled list is the full S&P 1500 (S&P 500 + 400 + 600), the brief's
suggested practical proxy. The $1B market-cap filter is applied on top.
"""
import csv
import json

import requests

from . import config

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
HEADERS = {"User-Agent": config.SEC_USER_AGENT}

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
