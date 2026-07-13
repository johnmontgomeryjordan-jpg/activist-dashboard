"""
SEC EDGAR ingestion + 8-K classification.

We list each company's recent 8-K/10-K/10-Q filings (free submissions API) and
classify them. For the two ambiguous-but-important item codes we READ the filing
text to confirm the signal, instead of trusting the item code alone:

  * Item 5.02 (officer/director change): tag "ceo_departure" only if the text
    shows a real resignation/departure; otherwise "leadership_change" (low-weight).
  * Item 2.02 (results of operations): tag "earnings_miss" only if the text shows
    a miss / guidance cut; otherwise "results_update" (note only, 0 points).

Item 2.06 (impairment), 2.05 (restructuring/exit costs), and 4.02 (non-reliance on
previously issued financials — i.e. a restatement) are specific enough to trust by
code: 4.02 is filed ONLY for a non-reliance/restatement event, so no text confirm is
needed. Text is fetched only for NEW 5.02/2.02 filings (skip already-stored ones), so
the extra requests stay bounded.
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
# 2026-07-07: added Item 4.02 (restatement / non-reliance) — the re-classification pass
# re-tags 8-Ks already in the window so existing non-reliance filings light up.
# 2026-07-13: precise Item 5.02 — separate real departures from routine appointments so the
#             standard term-of-office boilerplate ("...death, resignation or removal...") stops
#             tagging appointments/annual-meeting 8-Ks as "Executive departure".
CLASSIFIER_VERSION = "2026-07-13-item502-departure-precision"

_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")

# Item codes trusted by code (no text confirm). 4.02 = Non-Reliance on Previously Issued
# Financial Statements — a restatement/non-reliance event, one of the strongest accounting
# catalysts activists key off (it forces board/audit-committee accountability).
ITEM_DIRECT = {"2.06": "impairment", "2.05": "layoffs", "4.02": "restatement"}

# Real-departure phrases. Deliberately EXCLUDES bare "resign"/"resignation"/"retire"/
# "retirement"/"terminat"/"removal", which appear verbatim in the standard board-appointment
# term-of-office boilerplate ("...to hold office until his earlier death, resignation or
# removal...") and were tagging routine APPOINTMENTS as "Executive departure" (Amphastar,
# Trade Desk) and annual-meeting/equity-plan 8-Ks too (INSP).
DEPART_TERMS = [
    "has resigned", "have resigned", "resigned as", "resigned from", "resigned effective",
    "is resigning", "are resigning", "will resign", "tendered his resignation",
    "tendered her resignation", "tendered their resignation", "step down", "stepping down",
    "stepped down", "will step down", "to step down", "will retire", "to retire",
    "intends to retire", "his retirement", "her retirement", "their retirement",
    "retirement of", "departure of", "will depart", "departs", "no longer serve as",
    "no longer be employed", "will leave the company", "to leave the company",
    "terminated as", "termination of employment", "relieved of", "removed as",
    "separation from the company", "ceases to serve", "mutual agreement to",
]
# New-director / new-officer appointment phrases. A 5.02 with an appointment but no departure
# is a low-weight "leadership_change", NOT a departure. Routine annual RE-election language
# ("were elected", "election of directors") is deliberately excluded so a 5.07 vote-results
# 8-K doesn't read as a leadership event.
APPOINT_TERMS = [
    "appointed", "appointment of", "named to the board", "named as a director",
    "joins the board", "joined the board", "increased the size of the board",
    "increase the size of the board", "increased the number of directors",
    "increase the number of directors", "newly created", "to fill the vacancy",
    "appointed to serve", "to serve as a class",
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
        # Strip the standard term-of-office boilerplate ("...until his earlier death,
        # resignation or removal...") so a routine appointment isn't read as a departure.
        tclean = re.sub(r"death,?\s+resignation\s+or\s+removal", " ", t)
        if any(d in tclean for d in DEPART_TERMS):
            sigs.add("ceo_departure")
        elif any(a in tclean for a in APPOINT_TERMS):
            sigs.add("leadership_change")
        # else: 5.02 with no actual departure or new appointment (equity-plan amendment,
        # bylaw/charter change, comp arrangement, annual-meeting housekeeping) → no signal.
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
    "restatement": "Restatement / non-reliance",
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
            # Exec-reaction rows are derived from filing classification. Wipe them too so a
            # filing that is no longer a leadership event (a re-classified appointment or an
            # annual-meeting 8-K) can't leave a stale "stock dropped on a leadership 8-K"
            # reaction behind; refresh_exec_reactions rebuilds only current leadership filings.
            try:
                conn.execute("DELETE FROM exec_reactions")
            except Exception:
                pass
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
