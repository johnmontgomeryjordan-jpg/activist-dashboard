"""
SEC EDGAR ingestion + 8-K classification.

We list each company's recent 8-K/10-K/10-Q filings (free submissions API) and
classify them. For the ambiguous-but-important item codes we READ the filing
text to confirm the signal, instead of trusting the item code alone:

  * Item 5.02 (officer/director change): tag "ceo_departure" only if the text
    shows a real resignation/departure; otherwise "leadership_change" (low-weight).
  * Item 2.02 (results of operations): tag "earnings_miss" only if the text shows
    a miss / guidance cut; otherwise "results_update" (note only, 0 points).
  * Item 1.01 (material definitive agreement) and Item 2.01 (completion of
    acquisition/disposition): tag "divestiture" only if the text shows SELLER-side
    transaction language (selling/divesting a business, segment, subsidiary, or
    asset) and does NOT read as an acquisition (the company as buyer). Both codes
    are bidirectional/overloaded on EDGAR -- 1.01 alone covers everything from
    credit facilities to M&A, and 2.01 fires for both buying and selling -- so an
    ambiguous or acquisition-only read gets no signal rather than a false tag.

Item 2.06 (impairment), 2.05 (restructuring/exit costs), and 4.02 (non-reliance on
previously issued financials — i.e. a restatement) are specific enough to trust by
code: 4.02 is filed ONLY for a non-reliance/restatement event, so no text confirm is
needed. Text is fetched only for NEW 5.02/2.02/1.01/2.01 filings (skip already-stored
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

# EDGAR daily index: one file lists EVERY filing filed that day across ALL filers
# (CIK|Company|Form|Date|Filename, pipe-delimited). We use it to discover WHICH universe
# names filed recently, so ingest() only makes a submissions call for those — instead of a
# per-CIK call for all ~1,500 names every cycle. The old full per-CIK sweep was so large it
# rate-limited (a 429 that exhausts _get's 3 retries yields None -> the name is silently
# skipped), so the tail of the universe never got reached: the feed froze on one day's batch
# and later filers (e.g. INTC) never appeared. This makes the sweep actually complete.
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
# Steady state, scan only the last ~2 weeks of daily indexes (new + slightly-late filings;
# anything older was ingested on a prior cycle) — a handful of GETs. The FULL window is scanned
# only when the filings table is empty (fresh boot or a classifier-version wipe).
_INDEX_INCREMENTAL_DAYS = 14

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
# 2026-08-21: added Item 1.01/2.01 divestiture detection. These two codes previously had ZERO
#             handling at all (not even in ITEM_DIRECT, and their text was never fetched), so a
#             pending or completed divestiture produced no signal whatsoever — caught auditing
#             AHCO, whose Jul 20 2026 Cardinal Health divestiture (Diabetes Health Business,
#             $235M, announced/pending) never appeared anywhere on the profile. Text-confirmed,
#             seller-side only (see module docstring). Re-classification required to backfill.
CLASSIFIER_VERSION = "2026-08-21-divestiture-r10"  # r9: miss-tiered-anchored (Item 2.02 exhibit read)

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
# --- earnings_miss vocabulary --------------------------------------------------------------------
# TWO TIERS. This split exists because reading EX-99.1 changed the target from a ~1-page 8-K cover
# to a ~20-page press release full of segment tables, and a flat substring list does not survive
# that. Verified failure: Moody's Q1-2026 release — headline "ACHIEVED RECORD RESULTS", guidance
# REAFFIRMED — contains "Transactional revenue declined 54%..." and "Leveraged loan revenue declined
# year-over-year". The old bare term "revenue decline" substring-matched "revenue declined" and
# tagged a record quarter as an earnings miss. Philip Morris (beat EPS and revenue, guidance
# maintained) failed the same way. Some segment ALWAYS declines, so that term was a universal
# false positive once we scanned the exhibit.
#
# STRONG: one-directional and self-sufficient. A raise/beat never phrases itself this way.
MISS_TERMS_STRONG = [
    "below expectations", "below consensus", "below estimates",
    "missed expectations", "missed estimates", "missed consensus",
    "fell short of expectations", "fell short of estimates", "fell short of consensus",
    "profit warning", "disappointing",
    "below prior guidance", "below previous guidance", "below our prior guidance",
    "revised downward", "revising downward", "guidance revised lower",
]

# The guidance-cut family is a REGEX, not a string list. Enumerating variants silently missed real
# cuts: the list had "reducing our full-year" and "reducing full-year" but not "reducing ITS
# full-year", so a genuine cut phrased that way read as routine. One pattern covers the whole
# verb x determiner x noun space. Strictly one-directional — a raise never uses these verbs.
_CUT_VERB = (r"(?:lower(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|cut(?:s|ting)?|"
             r"trim(?:s|med|ming)?|slash(?:es|ed|ing)?|withdraw(?:s|n|ing|ew)?|"
             r"suspend(?:s|ed|ing)?)")
_GUIDE_NOUN = r"(?:guidance|outlook|forecast|full[-\s]?year|fiscal[-\s]\d{4})"
_CUT_GUIDANCE = re.compile(
    _CUT_VERB + r"\s+(?:its|our|their|the)?\s*" + _GUIDE_NOUN, re.I)

# WEAK: real miss language, but each one also appears verbatim in routine segment commentary,
# tables and tax footnotes. Only counts when it sits within _GUIDANCE_WINDOW characters of a
# forward-guidance anchor. This is the same header-anchoring discipline that fixed the governance
# "What We Don't Do" false positives — proximity, not presence.
#
# DELETED OUTRIGHT (unfixable even with an anchor, because they match ordinary reporting):
#   "revenue decline" / "decline in revenue"  -> match "revenue declined" in any segment table
#   "missed the"                              -> matches "missed the deadline" and similar
#   "fell short" / "falls short" (bare)       -> kept only in the "fell short of <bar>" forms above
#   "shortfall" (bare, earlier fix)           -> "shortfalls related to stock-based compensation"
MISS_TERMS_WEAK = [
    "represents a decline", "representing a decline", "represents a decrease",
    "representing a decrease", "reflects a decline",
    "below the low end", "below the midpoint", "below the prior", "below its prior",
    "expects revenue to decline", "expect revenue to decline", "revenue to decline",
    "weaker than expected", "lower than expected", "lower than previously",
    "less than previously expected", "below prior expectations",
    "revised lower", "revising lower",
    "revenue shortfall", "earnings shortfall",
]

# Kept as a flat union for any caller that still imports MISS_TERMS.
MISS_TERMS = MISS_TERMS_STRONG + MISS_TERMS_WEAK

_GUIDANCE_ANCHORS = ("guidance", "outlook", "now expects", "now expect",
                     "full-year", "full year", "fiscal year")
_GUIDANCE_WINDOW = 260

# A release that RAISED or REAFFIRMED guidance cannot be tagged a miss on weak evidence alone.
# Strong evidence still wins (a company can beat on EPS and still say "below consensus" on revenue).
# REAFFIRM/MAINTAIN matter as much as RAISE: Philip Morris beat on both lines and said "reaffirms
# its full-year 2026 adjusted diluted EPS growth guidance" — but its routine "cigarette shipment
# volume ... representing a decline of 1.5%" sat inside the guidance proximity window, so anchoring
# alone still tagged it a miss. The window is deliberately wide (8 intervening words) because the
# bias here is one-sided: missing a tag costs us a signal, a false tag costs us credibility.
_HELD_OR_RAISED = (r"(?:rais\w*|increas\w*|lift\w*|reaffirm\w*|reiterat\w*|maintain\w*|"
                   r"confirm\w*|unchanged|on track)")
_RAISE_NEAR_GUIDANCE = re.compile(
    _HELD_OR_RAISED + r"\s+(?:\S+\s+){0,8}?(?:guidance|outlook)"
    r"|(?:guidance|outlook)\s+(?:\S+\s+){0,8}?" + _HELD_OR_RAISED, re.I)


# --- Item 1.01 / 2.01: divestiture detection ------------------------------------------------------
# Item 1.01 ("Entry into a Material Definitive Agreement") is one of the most overloaded item codes
# on EDGAR — credit facilities, supply contracts, leases, JV formation, executive agreements, and
# M&A purchase agreements all file under it. Item 2.01 ("Completion of Acquisition or Disposition
# of Assets") is narrower but still bidirectional — it fires for the company buying something AND
# for the company selling something.
#
# The naive design (a "sell"-verb pattern vs. an "acquire"-verb pattern, tag when the former
# fires and not the latter) FAILS on the exact case that motivated this fix: a divestiture is
# almost always press-released from the BUYER's grammatical point of view — "[Cardinal Health]
# will ACQUIRE the Company's Diabetes Health Business" — so a verb-only test reads our own
# divestiture as an acquisition and drops it. Verbs don't carry the direction here; the OBJECT
# does. Since every 8-K is filed BY the company the item describes, "the Company's" / "its" /
# "the Registrant's" is always a self-reference — so whenever a deal verb (sell/acquire/divest/
# dispose/purchase/spin off/carve out) sits near an object phrased as OUR OWN business/segment/
# subsidiary/unit/operations/assets, that thing is leaving our balance sheet, regardless of who
# is grammatically buying. A pure acquisition of a THIRD PARTY's business ("acquire XYZ Corp",
# "XYZ's widget business") never produces that self-referential object, so it never matches —
# no separate "acquirer-language" guard is needed. Ambiguous or off-pattern text gets no signal,
# same bias as 5.02/2.02: a missed tag costs a signal, a false tag costs credibility.
_DEAL_VERB = (r"(?:sell\w*|sold|sale\w*|divest\w*|dispos\w*|acquir\w*|purchas\w*|"
             r"spin[-\s]?off\w*|carve[-\s]?out\w*)")
# The object noun for "its"/"the Company's" itself, kept a few words out so "its recently
# announced ACQUISITION of ..." (a nominalized action, not a business being sold) doesn't match.
_OWN_BIZ = (r"(?:the\s+company'?s|the\s+registrant'?s|its|our)"
           r"(?!\s+(?:recent(?:ly)?|previously|planned|pending)?\s*(?:acquisition|purchase)\b)"
           r"\s+(?:[a-z]+\s+){0,2}?"
           r"(?:business(?:es)?|segment|division|subsidiary|unit|operations?|assets?|"
           r"product\s+line)")
_DIVEST_RE = re.compile(
    r"(?:" + _DEAL_VERB + r"(?:\s+\S+){0,10}?\s+" + _OWN_BIZ +          # verb ... own-biz object
    r"|" + _OWN_BIZ + r"(?:\s+\S+){0,10}?\s+" + _DEAL_VERB +            # own-biz object ... verb (passive)
    r"|exit(?:ed|ing|s)?\s+(?:its|the)\s+[a-z]+(?:\s+[a-z]+){0,2}\s+business)",  # "exit its X business"
    re.I)


def is_divestiture(deal_text):
    """True only when a deal verb (sell/acquire/divest/dispose/purchase/spin off/carve out) sits
    near an object phrased as the filer's OWN business/segment/subsidiary/unit/assets — see the
    block comment above for why the object, not the verb, carries the buy/sell direction here."""
    return bool(_DIVEST_RE.search(deal_text or ""))


def _weak_miss_hit(t):
    """True if a weak miss phrase sits near forward-guidance language.

    Inspire's real Q1-2026 cut reads: '...revising its previously announced revenue OUTLOOK to a
    range of $X to $Y, which REPRESENTS A DECLINE of 4% to 10%...' — anchor and phrase ~60 chars
    apart, so it fires. Moody's 'Leveraged loan revenue declined year-over-year' has no guidance
    anchor anywhere near it, so it does not.
    """
    for term in MISS_TERMS_WEAK:
        start = 0
        while True:
            i = t.find(term, start)
            if i == -1:
                break
            window = t[max(0, i - _GUIDANCE_WINDOW): i + len(term) + _GUIDANCE_WINDOW]
            if any(a in window for a in _GUIDANCE_ANCHORS):
                return True
            start = i + 1
    return False


def is_earnings_miss(results_text):
    """Tiered miss detection over the 8-K cover page + EX-99.1 press release."""
    t = results_text or ""
    if any(m in t for m in MISS_TERMS_STRONG) or _CUT_GUIDANCE.search(t):
        return True
    if _RAISE_NEAR_GUIDANCE.search(t):
        return False
    return _weak_miss_hit(t)


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

    `text` is the 8-K primary document; `exhibit_text` is a companion press-release exhibit
    (fetched for 2.02, and now 1.01/2.01 too, since deal terms are sometimes furnished as a
    press release rather than spelled out in the body). The 5.02 officer-change logic reads
    ONLY the primary document on purpose — a results press release is full of executive quotes
    and "transition"/"CEO" language that would otherwise manufacture false departure tags. The
    2.02 miss test and the 1.01/2.01 divestiture test read both, since the substantive language
    for those isn't reliably confined to the primary document alone."""
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
        sigs.add("earnings_miss" if is_earnings_miss(results_text) else "results_update")
    if "1.01" in codes or "2.01" in codes:
        deal_text = t + " " + (exhibit_text or "")
        if is_divestiture(deal_text):
            sigs.add("divestiture")
        # else: routine agreement (1.01), a pure third-party acquisition, or text with no clear
        # deal-verb/own-business proximity -> no signal. See _DIVEST_RE comment above.
    return sorted(sigs)


PRETTY = {
    "ceo_departure": "Executive departure",
    "leadership_change": "Leadership change",
    "earnings_miss": "Earnings miss / guidance cut",
    "results_update": "Results",
    "impairment": "Material impairment",
    "layoffs": "Restructuring / exit costs",
    "restatement": "Restatement / non-reliance",
    "divestiture": "Divestiture / asset sale",
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

        need_text = form == "8-K" and (
            "5.02" in codes or "2.02" in codes or "1.01" in codes or "2.01" in codes)
        text = _doc_text(int(cik), acc_nodash, doc) if need_text else ""
        # For results filings and potential M&A/divestiture filings, also pull the press-release
        # exhibit — for 2.02 the miss/guidance language is only ever there; for 1.01/2.01 some
        # filers lead with a terse cover page and put deal terms in an accompanying press release.
        ex_text = (_exhibit_text(int(cik), acc_nodash)
                   if (form == "8-K" and ("2.02" in codes or "1.01" in codes or "2.01" in codes))
                   else "")
        sigs = classify(form, codes, text, ex_text)

        if form != "8-K" and not sigs:
            continue  # keep 8-Ks for the feed; skip routine 10-K/10-Q

        out.append({
            "id": acc, "cik": cik10, "ticker": ticker, "company": company,
            "form": form, "filed_at": filed, "title": _make_title(form, codes, sigs),
            "url": url, "signals": ",".join(sigs),
        })
    return out


def _ciks_with_recent_filings(days):
    """Set of 10-digit CIKs that filed a form we track within `days`, read from the EDGAR daily
    index (one GET per weekday; weekends/holidays 404 and are skipped). Returns an empty set on
    total failure, so ingest() falls back to the full per-CIK sweep — strictly no regression."""
    from datetime import datetime, timedelta
    out = set()
    by_day = []                                    # (ymd, tracked_filer_count, http_ok) — diagnostic
    today = datetime.utcnow().date()
    for i in range(days + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:                       # Sat/Sun — no daily index published
            continue
        q = (d.month - 1) // 3 + 1
        r = _get(DAILY_INDEX_URL.format(year=d.year, q=q, ymd=d.strftime("%Y%m%d")))
        if r is None:                              # 404 (holiday) / rate-limited — skip the day
            by_day.append((d.strftime("%Y%m%d"), 0, False))
            continue
        n = 0
        for line in r.text.splitlines():
            parts = line.split("|")
            if len(parts) != 5:                    # header/separator lines don't split into 5
                continue
            form = parts[2].strip()
            if form in FORMS or form.split("/")[0] in FORMS:   # include /A amendments
                cik = parts[0].strip()
                if cik.isdigit():
                    out.add(pad_cik(cik)); n += 1
        by_day.append((d.strftime("%Y%m%d"), n, True))
    # DIAGNOSTIC: most-recent 6 weekdays — shows whether the index even HAS Jul 24+ tracked filers.
    recent = " ".join(f"{ymd}={cnt}{'' if ok else '(404)'}" for ymd, cnt, ok in by_day[:6])
    print(f"[edgar] index recent days: {recent}")
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

    # Discover which universe names actually filed recently, via the daily index. Scan the full
    # window on a cold/empty table (fresh boot or classifier wipe), else just the recent slice.
    # Only those names get a submissions call, so the sweep completes instead of dropping the tail.
    targets = subset
    index_days = days if not existing else min(days, _INDEX_INCREMENTAL_DAYS)
    try:
        active = _ciks_with_recent_filings(index_days)
        if active:
            by_cik = {pad_cik(c["cik"]): c for c in subset if c.get("cik")}
            hits = [by_cik[k] for k in active if k in by_cik]
            if hits:
                targets = hits
                print(f"[edgar] daily-index: {len(active)} filers over {index_days}d · "
                      f"{len(hits)} in universe (vs {len(subset)} full sweep)")
    except Exception as e:
        print(f"[edgar] daily-index unavailable ({e}); full per-CIK sweep")

    # DIAGNOSTIC: is the submissions endpoint (data.sec.gov) even reachable? One probe call.
    if targets:
        _probe_cik = pad_cik(targets[0]["cik"])
        _pr = _get(SUBMISSIONS_URL.format(cik10=_probe_cik))
        print(f"[edgar] submissions probe CIK{_probe_cik}: {'OK' if _pr else 'FAILED'}")

    matched = 0  # names that returned >=1 NEW filing (after dedup)
    for c in targets:
        got = fetch_recent_filings_for_cik(c["cik"], c.get("ticker"),
                                           c.get("name"), days, existing)
        if got:
            matched += 1
        for f in got:
            database.upsert_filing(f)
            existing.add(f["id"])
            count += 1
    # DIAGNOSTIC: targeted vs. how many yielded new filings vs. total upserts. If probe=OK and
    # names-with-new-filings=0, the source simply has nothing newer than what's stored (no bug).
    print(f"[edgar] ingest: targeted {len(targets)} · names-with-new-filings {matched} · "
          f"upserted {count} · already-stored {len(existing)}")
    return count
