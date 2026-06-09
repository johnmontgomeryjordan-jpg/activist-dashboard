"""
SEC EDGAR ingestion + 8-K classification.

We list each company's recent 8-K/10-K/10-Q filings (free submissions API) and
classify them. For the two ambiguous-but-important item codes we READ the filing
text to confirm the signal, instead of trusting the item code alone:

  * Item 5.02 (officer/director change): tag "ceo_departure" only if the text
    shows a real resignation/departure; otherwise "leadership_change" (low-weight).
  * Item 2.02 (results of operations): tag "earnings_miss" only if the text shows
    a miss / guidance cut; otherwise "results_update" (note only, 0 points).

Item 2.06 (impairment) and 2.05 (restructuring/exit costs) are specific enough to
trust by code. Text is fetched only for NEW 5.02/2.02 filings (skip already-stored
ones), so the extra requests stay bounded.
"""
import re
import time

import requests

from . import config, database

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
FORMS = {"8-K", "10-K", "10-Q"}

# Bump this string to force a one-time re-classification of stored filings.
CLASSIFIER_VERSION = "2026-06-08-text-confirm"

_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")

ITEM_DIRECT = {"2.06": "impairment", "2.05": "layoffs"}

DEPART_TERMS = [
    "resign", "resignation", "step down", "stepping down", "stepped down",
    "will step down", "to step down", "departure of", "will depart", "departs",
    "terminat", "no longer serve", "no longer be", "will leave", "to leave",
    "leave the company", "retire", "retirement", "relieved of", "removed as",
    "separation from", "ceases to serve", "mutual agreement to",
]
MISS_TERMS = [
    "below expectations", "below consensus", "below estimates", "below the prior",
    "missed", "fell short", "falls short", "shortfall", "lowered guidance",
    "lower guidance", "lowers guidance", "lowering guidance", "reduced guidance",
    "reduces guidance", "cut guidance", "cuts guidance", "cutting guidance",
    "lowered its outlook", "lowered outlook", "reduced outlook", "cut its outlook",
    "profit warning", "weaker than expected", "lower than expected",
    "reduced its full-year", "lowered its full-year", "disappointing",
    "decline in revenue", "revenue decline", "below its prior",
]


def _get(url):
    for i in range(3):
        try:
            r = _session.get(url, timeout=25)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(1.0 * (i + 1))
    return None


def pad_cik(cik):
    return str(cik).lstrip("0").zfill(10)


def _doc_text(cik_int, accession_nodash, primary_doc):
    if not primary_doc:
        return ""
    r = _get(f"{ARCHIVE_BASE}/{cik_int}/{accession_nodash}/{primary_doc}")
    time.sleep(0.1)
    if not r or not r.text:
        return ""
    return _TAG.sub(" ", r.text).lower()[:120000]


def classify(form, item_codes, text):
    """Return sorted list of signal keys for this filing."""
    sigs = set()
    codes = re.findall(r"\d+\.\d+", item_codes or "")
    for c in codes:
        if c in ITEM_DIRECT:
            sigs.add(ITEM_DIRECT[c])
    t = text or ""
    if "5.02" in codes:
        sigs.add("ceo_departure" if any(d in t for d in DEPART_TERMS)
                 else "leadership_change")
    if "2.02" in codes:
        sigs.add("earnings_miss" if any(m in t for m in MISS_TERMS)
                 else "results_update")
    return sorted(sigs)


PRETTY = {
    "ceo_departure": "Executive departure",
    "leadership_change": "Leadership change",
    "earnings_miss": "Earnings miss / guidance cut",
    "results_update": "Results",
    "impairment": "Material impairment",
    "layoffs": "Restructuring / exit costs",
}


def _make_title(form, item_codes, sigs):
    label = ", ".join(PRETTY[s] for s in sigs if s in PRETTY) or "Material event"
    items = f" (Item {item_codes})" if item_codes else ""
    return f"{form}: {label}{items}"


def fetch_recent_filings_for_cik(cik, ticker, company, days, existing):
    cik10 = pad_cik(cik)
    resp = _get(SUBMISSIONS_URL.format(cik10=cik10))
    time.sleep(0.12)
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    items_l = recent.get("items", [])

    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()

    out = []
    for i, form in enumerate(forms):
        if form not in FORMS:
            continue
        filed = dates[i] if i < len(dates) else ""
        if filed < cutoff:
            continue
        acc = accs[i] if i < len(accs) else ""
        if not acc or acc in existing:
            continue
        acc_nodash = acc.replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        codes = items_l[i] if i < len(items_l) else ""
        url = (f"{ARCHIVE_BASE}/{int(cik)}/{acc_nodash}/{doc}" if doc
               else f"{ARCHIVE_BASE}/{int(cik)}/{acc_nodash}")

        need_text = form == "8-K" and ("5.02" in codes or "2.02" in codes)
        text = _doc_text(int(cik), acc_nodash, doc) if need_text else ""
        sigs = classify(form, codes, text)

        if form != "8-K" and not sigs:
            continue  # keep 8-Ks for the feed; skip routine 10-K/10-Q

        out.append({
            "id": acc, "cik": cik10, "ticker": ticker, "company": company,
            "form": form, "filed_at": filed, "title": _make_title(form, codes, sigs),
            "url": url, "signals": ",".join(sigs),
        })
    return out


def ingest(universe, days=None, max_companies=None):
    days = days or config.SCORE_WINDOW_DAYS
    # One-time re-classification when the classifier version changes.
    with database.get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key='edgar_classifier'").fetchone()
        if (row["value"] if row else None) != CLASSIFIER_VERSION:
            conn.execute("DELETE FROM filings")
            conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('edgar_classifier',?)",
                         (CLASSIFIER_VERSION,))
        existing = set(r["id"] for r in conn.execute("SELECT id FROM filings"))

    count = 0
    subset = universe[:max_companies] if max_companies else universe
    for c in subset:
        for f in fetch_recent_filings_for_cik(c["cik"], c.get("ticker"),
                                              c.get("name"), days, existing):
            database.upsert_filing(f)
            existing.add(f["id"])
            count += 1
    return count
