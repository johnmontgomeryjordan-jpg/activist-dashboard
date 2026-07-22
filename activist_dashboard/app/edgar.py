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
# 2026-07-22: read EX-99.1 for Item 2.02. An Item 2.02 8-K's primary document is only a cover page
#             ("...furnished as Exhibit 99.1"), so the miss/guidance language was NEVER visible to
#             the classifier — `earnings_miss` was unreachable universe-wide and every results 8-K
#             fell through to note-only `results_update`. Re-classification is required to re-tag.
CLASSIFIER_VERSION = "2026-07-22-item202-exhibit991-r8"  # r8: classify 2.02 on the press-release exhibit

_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")

# Item codes trusted by code (no text confirm). 4.02 = Non-Reliance on Previously Issued
# Financial Statements — a restatement/non-reliance event, one of the strongest accounting
# catalysts activists key off (it forces board/audit-committee accountability).
ITEM_DIRECT = {"2.06": "impairment", "2.05": "layoffs", "4.02": "restatement"}

# ---- Item 5.02 officer-change detection --------------------------------------------------------
# The bar (per FGS): flag only C-SUITE + PRESIDENT + GENERAL COUNSEL changes — a company-level
# officer, not a divisional EVP/SVP/VP, a board seat, or a committee assignment. And the title must
# be tied to the actual ACTION ("appointed X AS Chief Financial Officer" / "CFO ... STEPPED DOWN"),
# not merely present in the text. That distinction is what kills the false positives:
#   * a director's résumé that mentions officer roles at OTHER companies (Shake Shack / Pendarvis);
#   * employment-agreement severance boilerplate ("termination of employment", "for Cause") that
#     rides along on a HIRING (Shake Shack / Hook);
#   * a director resignation triggered by an ownership threshold (Concentrix);
#   * annual-meeting equity-plan + director-election housekeeping (Teradata, INSP).
# "vice president" is normalized out first so EVP/SVP/VP never satisfy the "president" alternative.
_OFFICER = (
    r"(?:chief\s+[a-z]+(?:\s+[a-z]+){0,2}\s+officer"          # chief [x][ y] officer
    r"|chief\s+executive|chief\s+financial|chief\s+operating|chief\s+legal"
    r"|principal\s+(?:executive|financial|accounting)\s+officer"
    r"|general\s+counsel"
    r"|president"                                             # VP variants stripped beforehand
    r"|\bceo\b|\bcfo\b|\bcoo\b|\bcio\b|\bcto\b|\bclo\b|\bcao\b)"
)
_LINK = r"(?:\s+(?:the|its|a|an|our|new|interim|acting)\b)*\s+"   # optional articles between as/of & title
# Appointment of an officer: an appoint/promote/hire verb whose assigned role is an officer title.
_APPT_OFFICER = re.compile(
    r"(?:appoint\w*|nam\w+|promot\w*|elevat\w*|hir\w*|elect\w*|assum\w*)\b[^.]{0,100}?"
    r"\b(?:as|to\s+serve\s+as|to\s+be|role\s+of|position\s+of|office\s+of)\b" + _LINK + _OFFICER,
    re.I)
# Departure of an officer: "<officer> ... <leaves>" OR "<leaves> ... as <officer>".
_DEP_A = (r"(?:resign\w*|step(?:s|ped|ping)?\s+(?:down|aside)|retir\w*|depart\w*|"
          r"leav\w+\s+the\s+company|separat\w+\s+from\s+the\s+company|"
          r"relieved\s+of|removed\s+as|no\s+longer\s+(?:serve|be\s+employed)|"
          r"tender\w*\s+(?:his|her|their)\s+resignation)")
_DEP_B = r"(?:resign\w*|step(?:s|ped|ping)?\s+down|retir\w*|depart\w*|terminat\w+|removed|relieved)"
_DEP_OFFICER_A = re.compile(_OFFICER + r"[^.]{0,70}?\b" + _DEP_A, re.I)
_DEP_OFFICER_B = re.compile(r"\b" + _DEP_B + r"\b[^.]{0,45}?\bas\b" + _LINK + _OFFICER, re.I)
# A MATERIAL leadership departure is the CEO / CFO / COO / President — the changes an activist
# actually cares about. A General Counsel / Chief Legal / Chief [HR/Tech/...] Officer leaving is a
# real officer change but NOT a leadership vulnerability, so it should read as a minor
# "leadership change", not a "CEO/exec departure" (Intel's Chief Legal Officer departure was being
# framed as a C-suite transition / activist opening). We keep the broad _OFFICER for the minor tier.
_OFFICER_MATERIAL = (
    r"(?:chief\s+executive(?:\s+officer)?"
    r"|chief\s+financial(?:\s+officer)?"
    r"|chief\s+operating(?:\s+officer)?"
    r"|principal\s+(?:executive|financial)\s+officer"
    r"|\bpresident\b"                                   # VP variants stripped beforehand
    r"|\bceo\b|\bcfo\b|\bcoo\b)"
)
_DEP_MATERIAL_A = re.compile(_OFFICER_MATERIAL + r"[^.]{0,70}?\b" + _DEP_A, re.I)
_DEP_MATERIAL_B = re.compile(r"\b" + _DEP_B + r"\b[^.]{0,45}?\bas\b" + _LINK + _OFFICER_MATERIAL, re.I)
_VP_STRIP = re.compile(r"(?:executive\s+|senior\s+|sr\.?\s+|group\s+|first\s+|corporate\s+)?"
                       r"vice[-\s]presidents?", re.I)
MISS_TERMS = [
    "below expectations", "below consensus", "below estimates", "below the prior",
    # TIGHTENED for exhibit-scanning: bare "missed" / "shortfall" were safe against a one-page
    # 8-K cover but are far too loose against a full press release — "shortfalls related to
    # stock-based compensation" is routine tax-accounting boilerplate (it appears verbatim in
    # Inspire's Q1-2026 release) and would spuriously tag an earnings miss universe-wide.
    "missed expectations", "missed estimates", "missed consensus", "missed the",
    "fell short", "falls short", "revenue shortfall", "earnings shortfall",
    "lowered guidance",
    "lower guidance", "lowers guidance", "lowering guidance", "reduced guidance",
    "reduces guidance", "cut guidance", "cuts guidance", "cutting guidance",
    "lowered its outlook", "lowered outlook", "reduced outlook", "cut its outlook",
    "profit warning", "weaker than expected", "lower than expected",
    "reduced its full-year", "lowered its full-year", "disappointing",
    "decline in revenue", "revenue decline", "below its prior",
    # Downward-revision language, now reachable because we read the EX-99.1 press release.
    # DELIBERATELY EXCLUDES neutral verbs ("updates guidance", "revises guidance", "narrows
    # guidance") — a guidance RAISE uses exactly those words, and a false "earnings miss" on a
    # partner-facing report is far worse than a missed tag. Every phrase below is one-directional.
    "below prior guidance", "below previous guidance", "below our prior",
    "lowering our full-year", "lowering full-year", "lowering its full-year",
    "reducing our full-year", "reducing full-year", "reduces its full-year",
    "cuts its full-year", "cutting its full-year", "lowers its full-year",
    "lowers outlook", "lowers its outlook", "lowering outlook", "lowering its outlook",
    "reduces outlook", "reduces its outlook", "reduced its guidance", "lowered its guidance",
    "withdraws guidance", "withdrawing guidance", "withdrew its guidance",
    "suspends guidance", "suspending guidance", "withdraws its outlook",
    "revised lower", "revising lower", "revised downward", "revising downward",
    "trims guidance", "trimmed guidance", "trims its outlook", "trimmed its outlook",
    "expects revenue to decline", "expect revenue to decline", "revenue to decline",
    "lower than previously", "less than previously expected", "below prior expectations",
    # The pattern a real guidance cut actually uses. Inspire's Q1-2026 release reads: "revising
    # its previously announced revenue outlook to ... which REPRESENTS A DECLINE of 4% to 10%
    # compared to 2025" — none of the phrasings above catch that, so the cut read as routine.
    # "revising/updating guidance" alone stays out (a RAISE says the same); the decline framing
    # is the one-directional part.
    "represents a decline", "representing a decline", "represents a decrease",
    "representing a decrease", "reflects a decline", "below the low end",
    "below the midpoint", "lowering our outlook", "reducing our outlook",
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


# --- Item 2.02: the earnings language lives in the EXHIBIT, not the 8-K -------------------------
# An Item 2.02 8-K's primary document is only a cover page: "the Company issued a press release
# announcing its financial results... furnished as Exhibit 99.1". None of the miss/guidance language
# is ever in it. Classifying on the primary doc alone therefore made `earnings_miss` unreachable —
# EVERY results 8-K fell through to the note-only `results_update` (Inspire's May-2026 guidance cut
# read as a routine result). We now also read EX-99.1 for 2.02 filings and scan that for MISS_TERMS.
# Two extra requests per NEW 2.02 filing, bounded by the same "skip already-stored" gate.
_EX_PRIORITY = ("ex991", "ex99", "991", "press", "earn")


def _exhibit_text(cik_int, accession_nodash):
    """Text of the EX-99.1 press release for a filing, via the free filing-index JSON.
    Exhibit filenames vary a lot (insp-ex991_6.htm, d123dex991.htm, a991pressrelease.htm), so we
    normalise and score candidates by priority. Returns "" when nothing plausible is found."""
    r = _get(f"{ARCHIVE_BASE}/{cik_int}/{accession_nodash}/index.json")
    time.sleep(0.1)
    if not r:
        return ""
    try:
        items = (r.json() or {}).get("directory", {}).get("item", []) or []
    except ValueError:
        return ""
    best, best_rank = None, len(_EX_PRIORITY)
    for it in items:
        name = it.get("name") or ""
        low = name.lower()
        if not low.endswith((".htm", ".html", ".txt")):
            continue
        norm = low.replace("-", "").replace("_", "")
        for rank, cue in enumerate(_EX_PRIORITY):
            if cue in norm and rank < best_rank:
                best, best_rank = name, rank
                break
    if not best:
        return ""
    r2 = _get(f"{ARCHIVE_BASE}/{cik_int}/{accession_nodash}/{best}")
    time.sleep(0.1)
    if not r2 or not r2.text:
        return ""
    return _TAG.sub(" ", r2.text).lower()[:120000]


def classify(form, item_codes, text, exhibit_text=""):
    """Return sorted list of signal keys for this filing.

    `text` is the 8-K primary document; `exhibit_text` is the EX-99.1 press release (2.02 only).
    The 5.02 officer-change logic reads ONLY the primary document on purpose — a results press
    release is full of executive quotes and "transition"/"CEO" language that would otherwise
    manufacture false departure tags. The 2.02 miss test reads both, since the guidance language
    is only ever in the exhibit."""
    sigs = set()
    codes = re.findall(r"\d+\.\d+", item_codes or "")
    for c in codes:
        if c in ITEM_DIRECT:
            sigs.add(ITEM_DIRECT[c])
    t = text or ""
    if "5.02" in codes:
        # Item 5.02's SEC-mandated caption — "Departure of Directors or Certain Officers;
        # Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of
        # Certain Officers" — appears verbatim in EVERY 5.02 body and contains both "departure
        # of" and "appointment of". If we don't strip it, that boilerplate header tags every
        # 5.02 (appointments, annual-meeting housekeeping, comp-plan changes) as a departure.
        # Strip the caption AND the standard term-of-office boilerplate, then classify on the
        # ACTUAL event text.
        tclean = re.sub(r"departure of directors or certain officers", " ", t)
        tclean = re.sub(r"appointment of certain officers", " ", tclean)
        tclean = re.sub(r"compensatory arrangements of certain officers", " ", tclean)
        tclean = re.sub(r"death,?\s+resignation,?\s+or\s+removal", " ", tclean)
        # Normalize VP variants out so EVP/SVP/VP never satisfy the "president" officer alternative.
        tclean = _VP_STRIP.sub(" vp ", tclean)
        # Flag only when a C-suite/President/GC title is tied to an actual departure or appointment
        # ACTION. Departure outranks appointment (an outgoing + incoming CEO is a departure).
        if _DEP_MATERIAL_A.search(tclean) or _DEP_MATERIAL_B.search(tclean):
            sigs.add("ceo_departure")                 # CEO / CFO / COO / President left -> material
        elif _DEP_OFFICER_A.search(tclean) or _DEP_OFFICER_B.search(tclean):
            sigs.add("leadership_change")             # GC / other officer left -> minor, not a "CEO departure"
        elif _APPT_OFFICER.search(tclean):
            sigs.add("leadership_change")
        # else: 5.02 with no actual departure or new appointment (equity-plan amendment,
        # bylaw/charter change, comp arrangement, annual-meeting housekeeping) → no signal.
    if "2.02" in codes:
        # Scan the cover page AND the press-release exhibit — the miss/guidance language is
        # essentially always in the exhibit, never the cover.
        results_text = t + " " + (exhibit_text or "")
        sigs.add("earnings_miss" if any(m in results_text for m in MISS_TERMS)
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
        # For results filings, also pull EX-99.1 — the miss/guidance language is only there.
        ex_text = (_exhibit_text(int(cik), acc_nodash)
                   if (form == "8-K" and "2.02" in codes) else "")
        sigs = classify(form, codes, text, ex_text)

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
