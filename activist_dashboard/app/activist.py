"""
Activist-filing detection via SEC EDGAR full-text search (free, efts.sec.gov).

A 13D is filed BY the activist, so it never appears in the target company's own
submissions feed. The reliable free way to ask "which of my companies just drew an
activist?" is EDGAR full-text search, filtered by the company's CIK and the activist
form types. Each hit's `ciks` array contains both the subject company and the filer,
so we match the subject against our universe.

This is a COINCIDENT marker, not a predictive one: a company with one of these filings
already has an activist, so we route it to "Active situations" (too late to pitch
proactively) rather than the shortlist. It captures both the >5% crowd (13D) and the
sub-5% crowd that never files a 13D (contested-proxy / dissident solicitations).

We deliberately EXCLUDE plain SC 13G (passive index-fund ownership) to avoid noise.
"""
import time

import requests

from . import config, database

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
# Activist campaigns persist for years -- a 13D filed two summers ago can still be a live
# situation today (the activist holds a board seat, the stake is unchanged so there's no
# new amendment to "refresh" the date). We look back two years so long-running campaigns
# aren't silently dropped. (Anything older than that, with no recent SEC activity at all,
# is best handled with a Manual tag.)
WINDOW_DAYS = 730
HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_session = requests.Session()
_session.headers.update(HEADERS)

# Form types we sweep for. We list amendments explicitly (SC 13D/A) and the full family of
# dissident / contested-proxy variants -- once a campaign opens, follow-on activity comes as
# amendments and these variant forms, so matching only the base forms would miss live
# campaigns that have moved past their opening filing.
ROOT_FORMS = [
    "SC 13D", "SC 13D/A",                                  # >5% stake + its amendments
    "DFAN14A",                                             # dissident soliciting materials
    "PREC14A", "DEFC14A",                                  # contested proxy (prelim / definitive)
    "PRRN14A", "DEFN14A", "DFRN14A",                       # non-management (dissident) proxy variants
    "PX14A6G", "PX14A6N",                                  # exempt solicitations by a shareholder
    "SC 14N", "SC 14N/A",                                  # shareholder director nominations
]
FORMS_PARAM = ",".join(ROOT_FORMS)

# Map a specific form to (kind, human label). 13D = >5% stake; others = proxy campaign.
def _kind_label(form):
    f = (form or "").upper()
    if f.startswith("SC 13D"):
        return "13d", "Activist filed a Schedule 13D (>5% stake, intent to influence)"
    if f in ("PREC14A", "DEFC14A", "PRRN14A", "DEFN14A", "DFRN14A"):
        return "proxy", "Contested proxy statement filed (proxy fight under way)"
    if f == "DFAN14A":
        return "proxy", "Dissident soliciting materials filed (activist campaign)"
    if f in ("PX14A6G", "PX14A6N"):
        return "proxy", "Exempt solicitation filed by a shareholder (activist pressure)"
    if f.startswith("SC 14N"):
        return "proxy", "Shareholder nominated directors (board challenge)"
    return "proxy", "Activist / dissident filing"


def _pad(cik):
    return str(cik).lstrip("0").zfill(10)


def _get(params):
    for i in range(3):
        try:
            r = _session.get(EFTS_URL, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(1.0 * (i + 1))
    return None


def _doc_url(hit, subject_cik10):
    """Build a link to the filing. It's stored under the FILER (activist) CIK, which
    is the cik in the hit that isn't the subject company."""
    src = hit.get("_source", {})
    _id = hit.get("_id", "")
    adsh, _, doc = _id.partition(":")
    if not adsh:
        adsh = src.get("adsh", "")
    ciks = src.get("ciks", []) or []
    subj = subject_cik10.lstrip("0")
    filer = next((c for c in ciks if c.lstrip("0") != subj), ciks[0] if ciks else subject_cik10)
    nod = adsh.replace("-", "")
    if doc:
        return f"{ARCHIVE}/{int(filer)}/{nod}/{doc}"
    return f"{ARCHIVE}/{int(filer)}/{nod}/{adsh}-index.htm"


def latest_activist_filing(cik, window_days=WINDOW_DAYS):
    """Return the most recent activist filing for one company, or None."""
    from datetime import datetime, timedelta
    cik10 = _pad(cik)
    start = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    end = datetime.utcnow().date().isoformat()
    j = _get({"q": "", "forms": FORMS_PARAM, "ciks": cik10,
              "startdt": start, "enddt": end})
    if not j:
        return None
    hits = (j.get("hits", {}) or {}).get("hits", []) or []
    if not hits:
        return None
    # results come back newest-first when a ciks filter is used
    best = hits[0]
    src = best.get("_source", {})
    form = src.get("form") or (src.get("root_forms") or [""])[0]
    kind, label = _kind_label(form)
    return {"kind": kind, "form": form, "label": label,
            "filed": src.get("file_date"), "url": _doc_url(best, cik10)}


def refresh_activist(ciks, window_days=WINDOW_DAYS):
    """Sweep the given CIKs for recent activist filings. Sets an activist flag for
    companies that have one and clears it for those that don't. Returns the count
    of companies currently flagged."""
    flagged = 0
    for cik in ciks:
        try:
            hit = latest_activist_filing(cik, window_days)
        except Exception:
            hit = None
        time.sleep(0.15)  # stay well under SEC's 10 req/s
        if hit:
            database.set_activist_flag(cik, hit["kind"], hit["form"], hit["label"],
                                       hit["filed"], hit["url"])
            flagged += 1
        else:
            database.clear_activist_flag(cik)
    print(f"[activist] swept {len(ciks)} names; {flagged} have an active activist filing")
    return flagged
