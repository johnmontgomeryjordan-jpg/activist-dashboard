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

from . import config, database, activists

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"

# Sentinel: the EDGAR query FAILED (network/transient), which is different from a clean
# "no activist filing." A failed query must never cause us to clear an existing flag.
ERROR = object()
# 18-month agitation gate: a name is an "active situation" only if the activist agitated
# (13D / contested proxy) within the last ~18 months. Older campaigns are treated as stale
# and drop off -- an ongoing campaign almost always refreshes its date via a 13D/A amendment,
# and a genuinely dormant 20-month-old filing is best re-added with a Manual tag. Kept in
# sync with scoring.AGITATION_MAX_DAYS. (Previously 730; tightened per the recency rule.)
WINDOW_DAYS = 548
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

# Forms we keep ONLY when a KNOWN activist filed them. DFAN14A (dissident soliciting
# materials) and PX14A6G (exempt solicitation) are filed by lots of non-activists — ESG
# gadflies, CLO managers, SPAC founders, one-off individuals — so on their own they flood
# the list with mega-cap noise (Microsoft, Coca-Cola, Berkshire...). The remaining forms
# (SC 13D, DEFC14A/PREC14A, SC 14N) are inherently a real campaign regardless of filer, so
# they stay broad — a brand-new activist's first 13D still surfaces even if not on the list.
GATED_FORMS = {"DFAN14A", "PX14A6G"}


# Map a form to (kind, a filer-friendly verb phrase). The filer name is prepended by the
# caller, e.g. "Starboard Value LP — filed a Schedule 13D (...)".
def _kind_label(form):
    f = (form or "").upper()
    if f.startswith("SC 13D"):
        return "13d", "filed a Schedule 13D (>5% stake, intent to influence)"
    if f in ("PREC14A", "DEFC14A"):
        return "proxy", "filed a contested proxy statement (proxy fight under way)"
    if f == "DFAN14A":
        return "proxy", "filed dissident soliciting materials (activist campaign)"
    if f == "PX14A6G":
        return "proxy", "filed an exempt solicitation (activist pressure)"
    if f == "SC 14N":
        return "proxy", "nominated directors (board challenge)"
    return "proxy", "filed activist / dissident materials"


def _is_gated(form, root_forms):
    """True if this filing is a noisy form that requires a known-activist filer."""
    cands = [form] + list(root_forms or [])
    return any((str(x) or "").upper().replace(" ", "") in GATED_FORMS for x in cands if x)


# A contested proxy can be filed by a shareholder ACTIVIST (a fund) or by a corporate ACQUIRER
# running an M&A/control contest (UWM Holdings Corp bidding for Two Harbors; Paramount Skydance
# Corp for Warner Bros. Discovery). The latter is a takeover fight, NOT shareholder activism, and
# must not sit under a "an activist filing is on record" header. We tell them apart by the filer's
# name: fund markers => activist; corporate/acquirer markers (and no fund marker) => M&A.
_FUND_MARKERS = (
    "capital", " management", " manage ", "partners", " fund", " funds", "advisor", "adviser",
    "asset manage", " value ", " value,", "master fund", " lp", " l.p", "investment management",
    "associates", " gp ", " holdings lp", "offshore", " ltd fund",
)
_MA_MARKERS = (
    "corporation", " corp", " inc ", " inc,", " inc.", "incorporated", " plc", "bancorp",
    "acquisition", "merger sub", " holdco", " bidco", " parent", " n.v", " s.a", " ag ",
    " se ", " ltd", " limited", " company", " co.", " group ",
)


# A campaign that SETTLED (cooperation agreement / standstill) has stood down — the activist agreed
# to support the board and stop agitating. We detect it from the company's OWN 8-K filed AFTER the
# activist's filing that carries a cooperation / settlement agreement (Teradata↔Lynrock, Feb 2026).
_SETTLE_PHRASES = ('"cooperation agreement"', '"settlement agreement"')


def _settlement_after(cik10, after_date):
    """Return (settle_date, url) of a cooperation/settlement 8-K the company filed AFTER
    `after_date`, else None. Signals the activist campaign has stood down. (If the activist later
    re-agitates, its newer filing post-dates the settlement, so latest_activist_filing won't mark
    it settled — the date ordering handles re-engagement.)"""
    if not after_date:
        return None
    from datetime import datetime, timedelta
    try:                                    # start the day AFTER, so the filing itself never counts
        start = (datetime.fromisoformat(str(after_date)).date() + timedelta(days=1)).isoformat()
    except ValueError:
        start = str(after_date)
    end = datetime.utcnow().date().isoformat()
    for q in _SETTLE_PHRASES:
        j = _get({"q": q, "forms": "8-K", "ciks": cik10, "startdt": start, "enddt": end})
        if not j:
            continue
        for h in (j.get("hits", {}) or {}).get("hits", []) or []:
            src = h.get("_source", {}) or {}
            ciks = src.get("ciks") or []
            if ciks and ciks[0] == cik10:   # the company itself filed it (subject = filer)
                return src.get("file_date"), _doc_url(h, cik10)
    return None


def _filer_type(name, known):
    """Return 'activist' (a fund / known activist) or 'ma' (a corporate acquirer running a
    takeover / control contest). Known activists and fund-named filers are always 'activist';
    only a clearly corporate/acquirer filer with NO fund marker is treated as M&A."""
    if known:
        return "activist"
    n = " " + (name or "").lower() + " "
    if any(m in n for m in _FUND_MARKERS):
        return "activist"
    if any(m in n for m in _MA_MARKERS):
        return "ma"
    return "activist"          # unknown -> default to activist (never HIDE a possible real one)


def _filer_names(src, subject_cik10):
    """Clean filer name(s) = the display_names that aren't the subject (index 0)."""
    dn = src.get("display_names") or []
    return [activists.clean_filer(n) for n in dn[1:] if n]


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
    # The newest valid hit drives date/form/url. But a company's OWN contested proxy carries no
    # external filer in display_names, so on its own it reads "An activist". The dissident filed
    # their own materials in a SEPARATE hit — so we scan ALL valid hits to RESOLVE the dissident's
    # name (a known activist wins; else the first external filer seen) instead of giving up.
    best_src = best_hit = None
    resolved_who = None
    for h in hits:                         # newest-first when a ciks filter is used
        src = h.get("_source", {})
        ciks = src.get("ciks") or []
        if not ciks or ciks[0] != cik10:   # ciks[0] is the SUBJECT; if not us, we're the filer
            continue
        form = src.get("form") or (src.get("root_forms") or [""])[0]
        filers = _filer_names(src, cik10)
        filer_known = any(activists.is_known_activist(f) for f in filers)
        # Filer gate: keep a noisy DFAN14A / PX14A6G only when a KNOWN activist filed it.
        if _is_gated(form, src.get("root_forms")) and not filer_known:
            continue
        if best_src is None:               # newest valid hit -> drives date / form / url
            best_src, best_hit = src, h
        if filers:                         # resolve the dissident name across all valid hits
            known = next((f for f in filers if activists.is_known_activist(f)), None)
            if known:
                resolved_who = known                       # a known activist wins outright
            elif resolved_who is None:
                resolved_who = filers[0]                   # else the first external filer we see
    if best_src is None:
        return None                        # company only appears as a filer -> not a target
    src, form = best_src, (best_src.get("form") or (best_src.get("root_forms") or [""])[0])
    own = _filer_names(src, cik10)
    who = (own[0] if own else None) or resolved_who or "A dissident shareholder"
    filer_known = activists.is_known_activist(who)
    kind, base = _kind_label(form)
    # Separate a corporate ACQUIRER's takeover/control contest from shareholder activism.
    if _filer_type(who, filer_known) == "ma":
        kind = "ma"
        base = ("is running a takeover / control contest (M&A — a corporate bidder, "
                "not a shareholder activist)")
    else:
        # Stand down a SETTLED campaign: a cooperation/settlement 8-K filed after this filing
        # means the activist agreed to a standstill -> show it in "Recently settled", not live.
        settle = _settlement_after(cik10, src.get("file_date"))
        if settle:
            sdate, surl = settle
            return {"kind": "settled", "form": form,
                    "label": f"{who} — settled: cooperation agreement (filed {sdate})",
                    "who": who, "filed": src.get("file_date"),
                    "url": surl or _doc_url(best_hit, cik10)}
    label = f"{who} — {base}"
    return {"kind": kind, "form": form, "label": label, "who": who,
            "filed": src.get("file_date"), "url": _doc_url(best_hit, cik10)}


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
