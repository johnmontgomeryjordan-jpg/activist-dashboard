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

# Sentinel: the EDGAR query FAILED (network/transient), which is different from a clean
# "no activist filing." A failed query must never cause us to clear an existing flag.
ERROR = object()
# Activist campaigns persist for years -- a 13D filed two summers ago can still be a live
# situation today (the activist holds a board seat, the stake is unchanged so there's no
# new amendment to "refresh" the date). We look back two years so long-running campaigns
# aren't silently dropped. (Anything older than that, with no recent SEC activity at all,
# is best handled with a Manual tag.)
WINDOW_DAYS = 730
HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_session = requests.Session()
_session.headers.update(HEADERS)

# root_forms we sweep for. EDGAR full-text search groups a form's amendments under its
# base type, so "SC 13D" also surfaces SC 13D/A, and the contested-proxy forms cover their
# revised/definitive variants. (Proven list -- a speculative expansion that listed the
# slash-amendment codes explicitly coincided with confirmed situations vanishing, so we
# keep the base forms that reliably match.)
ROOT_FORMS = ["SC 13D", "DFAN14A", "PREC14A", "DEFC14A", "PX14A6G", "SC 14N"]
FORMS_PARAM = ",".join(ROOT_FORMS)

# Map a specific form to (kind, human label). 13D = >5% stake; others = proxy campaign.
def _kind_label(form):
    f = (form or "").upper()
    if f.startswith("SC 13D"):
        return "13d", "Activist filed a Schedule 13D (>5% stake, intent to influence)"
    if f in ("PREC14A", "DEFC14A"):
        return "proxy", "Contested proxy statement filed (proxy fight under way)"
    if f == "DFAN14A":
        return "proxy", "Dissident soliciting materials filed (activist campaign)"
    if f == "PX14A6G":
        return "proxy", "Exempt solicitation filed by a shareholder (activist pressure)"
    if f == "SC 14N":
        return "proxy", "Shareholder nominated directors (board challenge)"
    return "proxy", "Activist / dissident filing"


def _pad(cik):
    return str(cik).lstrip("0").zfill(10)


def _get(params):
    for i in range(4):
        try:
            r = _session.get(EFTS_URL, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:               # rate-limited -- back off and retry
                time.sleep(2.0 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(1.5 * (i + 1))
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
    """Most recent activist filing where THIS company is the SUBJECT (target). Returns a
    dict on a hit, None on a clean 'not a subject', or ERROR if the EDGAR query failed
    (so the caller knows not to clear an existing flag).

    EDGAR full-text search lists the SUBJECT/issuer first in each hit's `ciks` array and
    the activist filer(s) after it. So a company is the TARGET only when its CIK is
    ciks[0]; a 13D the company FILED against someone else (it's the activist, e.g. IAC ->
    ANGI) puts the company in ciks[1:], and is correctly skipped -- no extra lookup needed."""
    from datetime import datetime, timedelta
    cik10 = _pad(cik)
    start = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    end = datetime.utcnow().date().isoformat()
    j = _get({"q": "", "forms": FORMS_PARAM, "ciks": cik10,
              "startdt": start, "enddt": end})
    if j is None:
        return ERROR                       # query failed -- do NOT treat as "no filing"
    hits = (j.get("hits", {}) or {}).get("hits", []) or []
    if not hits:
        return None
    for best in hits:                      # newest-first when a ciks filter is used
        src = best.get("_source", {})
        ciks = src.get("ciks") or []
        if not ciks or ciks[0] != cik10:   # ciks[0] is the SUBJECT; if not us, we're the filer
            continue
        form = src.get("form") or (src.get("root_forms") or [""])[0]
        kind, label = _kind_label(form)
        return {"kind": kind, "form": form, "label": label,
                "filed": src.get("file_date"), "url": _doc_url(best, cik10)}
    return None                            # company only appears as a filer -> not a target


def _sweep_one(cik, window_days, clear_missing):
    """Check one CIK and apply the result. Returns 'flagged', 'cleared', 'noop', or
    'error' (the query failed -- leave any existing flag untouched)."""
    try:
        hit = latest_activist_filing(cik, window_days)
    except Exception:
        hit = ERROR
    if isinstance(hit, dict):
        database.set_activist_flag(cik, hit["kind"], hit["form"], hit["label"],
                                   hit["filed"], hit["url"])
        return "flagged"
    if hit is ERROR:
        return "error"
    if clear_missing:                          # clean "not a subject" -> only full sweep clears
        database.clear_activist_flag(cik)
        return "cleared"
    return "noop"


def refresh_activist(ciks, window_days=WINDOW_DAYS, clear_missing=True):
    """Sweep the given CIKs for recent activist filings where the company is the SUBJECT.
    `clear_missing` controls whether names WITHOUT a hit get their flag cleared -- only the
    FULL universe sweep should clear (it authoritatively checks everyone); a PARTIAL sweep
    (tracked names only, e.g. the Run-enrichment button) must NOT clear, or it would erase
    Confirmed situations outside the small tracked subset.

    SEC rate-limits aggressively, so a sweep this size always sheds a few transient errors.
    Those names keep their existing flag (never wrongly cleared), and -- on a full sweep --
    get a SECOND, slower pass so a stale or mis-attributed flag (e.g. a filer like IAC)
    reliably clears within a single run instead of lingering until the error happens to
    clear on its own. Returns the count flagged in this sweep."""
    flagged = 0
    errored = []
    for cik in ciks:
        res = _sweep_one(cik, window_days, clear_missing)
        time.sleep(0.2)                        # stay comfortably under SEC's 10 req/s
        if res == "flagged":
            flagged += 1
        elif res == "error":
            errored.append(cik)

    retried_ok = 0
    if errored:
        time.sleep(2.0)                        # let any rate-limit window cool off
        still = []
        for cik in errored:
            res = _sweep_one(cik, window_days, clear_missing)
            time.sleep(0.5)                    # slower pace on the recovery pass
            if res == "error":
                still.append(cik)
            else:
                retried_ok += 1
                if res == "flagged":
                    flagged += 1
        errored = still

    scope = "full" if clear_missing else "partial (non-destructive)"
    tail = f"; {len(errored)} still erroring after retry (flags kept)" if errored else ""
    if retried_ok:
        tail = f"; recovered {retried_ok} on retry" + tail
    print(f"[activist] swept {len(ciks)} names ({scope}); "
          f"{flagged} have an active activist filing{tail}")
    return flagged
