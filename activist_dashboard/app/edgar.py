"""
SEC EDGAR ingestion.

Two endpoints are used, both free and public:
  * https://efts.sec.gov/LATEST/search-index?q=...   (full-text search)
  * https://data.sec.gov/submissions/CIK##########.json (per-company filing list)

We pull recent 8-K, 10-K and 10-Q filings for the monitored universe, read the
filing's index, and classify each one against the SIGNAL_KEYWORDS dictionary.
SEC rate-limits to ~10 requests/sec and requires a descriptive User-Agent.
"""
import time
import json
import re

import requests

from . import config

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"

# Forms we care about
FORMS = {"8-K", "10-K", "10-Q"}

_session = requests.Session()
_session.headers.update(HEADERS)


def _get(url, **kwargs):
    """GET with polite rate limiting and basic retry."""
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=20, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except requests.RequestException:
            time.sleep(1.0 * (attempt + 1))
    return None


def pad_cik(cik):
    return str(cik).lstrip("0").zfill(10)


def fetch_recent_filings_for_cik(cik, ticker, company, days):
    """Return a list of classified filing dicts for one company."""
    cik10 = pad_cik(cik)
    resp = _get(SUBMISSIONS_URL.format(cik10=cik10))
    time.sleep(0.12)  # stay under SEC's ~10 req/sec limit
    if resp is None or resp.status_code != 200:
        return []

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    primary_descs = recent.get("primaryDescription", [])
    items = recent.get("items", [])

    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()

    out = []
    for i, form in enumerate(forms):
        if form not in FORMS:
            continue
        filed = dates[i] if i < len(dates) else ""
        if filed < cutoff:
            continue
        accession = accessions[i].replace("-", "") if i < len(accessions) else ""
        doc = primary_docs[i] if i < len(primary_docs) else ""
        desc = primary_descs[i] if i < len(primary_descs) else ""
        item_codes = items[i] if i < len(items) else ""
        url = f"{ARCHIVE_BASE}/{int(cik)}/{accession}/{doc}" if doc else \
              f"{ARCHIVE_BASE}/{int(cik)}/{accession}"

        signals = classify_filing(form, item_codes, desc)
        if not signals:
            # Keep 8-Ks even if unclassified so the live feed shows activity,
            # but skip routine 10-K/10-Q with no detectable signal.
            if form != "8-K":
                continue

        title = _make_title(form, item_codes, desc)
        out.append({
            "id": accessions[i] if i < len(accessions) else url,
            "cik": cik10,
            "ticker": ticker,
            "company": company,
            "form": form,
            "filed_at": filed,
            "title": title,
            "url": url,
            "signals": ",".join(signals),
        })
    return out


# 8-K item codes that map directly to our signals (faster & more reliable than
# text scraping for the common cases).
ITEM_SIGNAL_MAP = {
    "5.02": "ceo_departure",   # Departure/Election of Directors or Officers
    "2.05": "layoffs",         # Costs Associated with Exit or Disposal Activities
    "2.06": "impairment",      # Material Impairments
    "2.02": "earnings_miss",   # Results of Operations (check text for a miss)
}


def classify_filing(form, item_codes, description):
    """Return a set of signal keys triggered by this filing."""
    signals = set()
    codes = re.findall(r"\d+\.\d+", item_codes or "")
    for code in codes:
        if code in ITEM_SIGNAL_MAP:
            signals.add(ITEM_SIGNAL_MAP[code])

    text = f"{description or ''}".lower()
    for sig, kws in config.SIGNAL_KEYWORDS.items():
        if any(kw in text for kw in kws):
            signals.add(sig)
    return sorted(signals)


def _make_title(form, item_codes, description):
    pretty = {
        "ceo_departure": "Executive change",
        "earnings_miss": "Results / guidance",
        "impairment": "Material impairment",
        "layoffs": "Restructuring / exit costs",
    }
    sigs = classify_filing(form, item_codes, description)
    label = ", ".join(pretty[s] for s in sigs) if sigs else "Material event"
    items = f" (Item {item_codes})" if item_codes else ""
    return f"{form}: {label}{items}"


def ingest(universe, days=None, max_companies=None):
    """
    Pull recent filings for every company in `universe` (list of dicts with
    cik/ticker/name) and write them to the database.
    Returns the number of filings ingested.
    """
    from . import database
    days = days or config.SCORE_WINDOW_DAYS
    count = 0
    subset = universe[:max_companies] if max_companies else universe
    for c in subset:
        filings = fetch_recent_filings_for_cik(
            c["cik"], c.get("ticker"), c.get("name"), days)
        for f in filings:
            database.upsert_filing(f)
            count += 1
    return count
