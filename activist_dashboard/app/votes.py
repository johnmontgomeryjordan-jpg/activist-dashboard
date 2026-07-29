"""
Shareholder-vote discontent from the 8-K Item 5.07 (free, EDGAR).

After the annual meeting, a company files an 8-K with Item 5.07 reporting how every
proposal was voted. The single cleanest discontent signal is the SAY-ON-PAY result --
the advisory vote on executive compensation. When support drops well below the ~90%+
norm, it's a recognized early warning that shareholders are unhappy with the board, and
it often precedes an activist campaign.

We parse ONLY say-on-pay support (For / (For + Against)). It's the most standardized line
in the filing, so we can read it reliably; we deliberately do NOT try to parse per-director
results, which vary too much format-to-format to trust in a partner-facing tool.

8-K is filed by the company itself, so (unlike 13D/Form 4) it lives in the company's own
record -- we locate the most recent one carrying Item 5.07 via the EDGAR submissions API
(the canonical per-company filing index), then fetch and parse the document.
"""
import re
import time

import requests

from . import config, database

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
# The EDGAR submissions API returns a company's filing index (form, item codes, accession,
# primary document) as JSON. This is the reliable way to find an 8-K carrying Item 5.07 --
# the previous full-text-search lookup passed an empty query and matched nothing (0 names).
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
WINDOW_DAYS = 420          # one annual-meeting cycle (+ slack)
_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")
# A genuine say-on-pay result is a For / Against / Abstain triple, each a comma-grouped tally
# (e.g. "65,000,000"). Requiring all three (in order) is what separates the real vote from
#   (a) the say-on-pay FREQUENCY proposal, which shares the "compensation of named executive
#       officers" wording but reports 1-Year / 2-Year / 3-Year options, never For/Against, and
#   (b) narrative prose that merely mentions "for" or "compensation".
# This replaced a fragile "first For number, first Against number" read that anchored on the wrong
# row and mis-scored Simply Good Foods at 50% when the real say-on-pay passed with 96.5%. Two
# layouts survive de-tagging:
#   interleaved:  "For 65,000,000 Against 2,400,000 Abstained 50,000"
#   header+data:  "For Against Abstained Broker Non-Votes  65,000,000 2,400,000 50,000 4,000,000"
_ROW_INTERLEAVED = re.compile(
    r"\bfor\b[^\d]{0,20}(\d{1,3}(?:,\d{3})+)"
    r"[^\d]{0,60}?\bagainst\b[^\d]{0,20}(\d{1,3}(?:,\d{3})+)"
    r"[^\d]{0,80}?\babstain")
_ROW_HEADER = re.compile(
    r"\bfor\b[^\d]{0,20}\bagainst\b[^\d]{0,20}\babstain\w*[^\d]{0,80}?"
    r"(\d{1,3}(?:,\d{3})+)[^\d]{0,20}(\d{1,3}(?:,\d{3})+)")
# A real say-on-pay "For" essentially never falls below ~20%, even when the vote FAILS (failed
# say-on-pay votes cluster around 40-60%). A parsed value below this means we anchored on the
# wrong numbers, so we treat it as UNPARSED (None) rather than emit a false near-0% signal.
MIN_PLAUSIBLE_SOP = 0.20
# For + Against on a real S&P 1500 say-on-pay vote is in the millions of shares; a total far below
# that means we anchored on small/wrong numbers, so reject it.
MIN_VOTES = 1_000_000
# Bump to force a one-time re-parse of cached votes when the parser changes. Votes are otherwise
# cached by meeting accession, so a fix wouldn't reach a name until its NEXT annual meeting -- e.g.
# Simply Good Foods' mis-parsed 50% would linger for a year.
VOTES_PARSER_VERSION = "2026-07-29-submissions-lookup"

# Phrases that identify the advisory say-on-pay proposal (lowercased).
SOP_PHRASES = [
    "advisory vote to approve executive compensation",
    "advisory resolution to approve executive compensation",
    "advisory vote to approve named executive officer compensation",
    "advisory vote on executive compensation",
    "advisory vote to approve the compensation",
    "approve, on an advisory basis, the compensation",
    "advisory approval of the compensation",
    "compensation of our named executive officers",
    "compensation of the named executive officers",
    "compensation of named executive officers",   # bare (e.g. Simply Good Foods' proposal title)
    "say-on-pay", "say on pay",
]
_HELD = re.compile(r"held on\s+([A-Z][a-z]+ \d{1,2},? \d{4})")


def _pad(cik):
    return str(cik).lstrip("0").zfill(10)


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


def _latest_507(cik10, start, end):
    """Most recent 8-K carrying Item 5.07 for this company, from the EDGAR submissions API.
    Returns {'accn','doc','date'} or None. `start` is the oldest filing date we care about
    (the submissions 'recent' block is newest-first, so we stop once we pass it)."""
    r = _get(SUBMISSIONS.format(cik10))
    if not r:
        return None
    try:
        recent = (r.json().get("filings") or {}).get("recent") or {}
    except ValueError:
        return None
    forms = recent.get("form") or []
    items = recent.get("items") or []
    accns = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    dates = recent.get("filingDate") or []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        it = items[i] if i < len(items) else ""
        if "5.07" not in (it or ""):
            continue
        d = dates[i] if i < len(dates) else ""
        if start and d and d < start:      # older than the window -> stop (list is newest-first)
            break
        return {"accn": accns[i] if i < len(accns) else "",
                "doc": docs[i] if i < len(docs) else "",
                "date": d}
    return None


def _iter_positions(low):
    """Every position where a say-on-pay phrase appears, in document order. The phrase is often
    mentioned in the narrative intro BEFORE the results table, so we can't just take the first
    hit -- we try each until one yields a plausible For/Against pair (usually the table row)."""
    seen = set()
    for p in SOP_PHRASES:
        start = 0
        while True:
            i = low.find(p, start)
            if i == -1:
                break
            seen.add(i)
            start = i + 1
    return sorted(seen)


def _for_against(window):
    """(for, against) from a genuine say-on-pay result row, or None. Requires a For/Against/Abstain
    structure (interleaved or header-then-data) so arbitrary numbers can't be grabbed."""
    m = _ROW_INTERLEAVED.search(window) or _ROW_HEADER.search(window)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def parse_say_on_pay(html):
    """Return (approval_fraction, meeting_date) from an 8-K 5.07, or (None, meeting).
    approval = For / (For + Against) on the advisory executive-compensation vote. Anchors on the
    real say-on-pay result row (For/Against/Abstain, millions of shares) and skips the say-on-pay
    FREQUENCY proposal; returns None rather than a mis-anchored value."""
    text = _TAG.sub(" ", html or "")
    low = text.lower()
    m = _HELD.search(text)
    meeting = m.group(1) if m else None
    for pos in _iter_positions(low):
        # The say-on-pay FREQUENCY proposal reuses the "compensation of named executive officers"
        # wording, but it reports 1-Year / 2-Year / 3-Year options with NO For/Against -- so the
        # structured row match below (which requires a For/Against/Abstain triple) can't anchor on
        # it, and a window opened on the frequency proposal simply reads forward to the real
        # say-on-pay result row.
        fa = _for_against(low[pos:pos + 1500])
        if not fa:
            continue
        for_v, against_v = fa
        denom = for_v + against_v
        if denom < MIN_VOTES:          # anchored on small/wrong numbers, not a real company vote
            continue
        approval = for_v / denom
        # Only accept a plausible result; skip mis-anchored parses (e.g. a year read as a tally).
        if MIN_PLAUSIBLE_SOP <= approval <= 1.0:
            return approval, meeting
    return None, meeting


def refresh_votes(ciks, window_days=WINDOW_DAYS):
    """Parse the latest say-on-pay result for each (padded) CIK. Cached by accession.
    Returns the number of companies with a freshly-parsed vote."""
    from datetime import datetime, timedelta
    start = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    end = datetime.utcnow().date().isoformat()
    # One-time full re-parse when the parser version changes (see VOTES_PARSER_VERSION): re-read
    # even already-seen accessions, and overwrite any stale value (a corrected number, or None to
    # clear a bad one) so a fixed parse reaches cached names immediately.
    force = database.get_meta("votes_parser_version") != VOTES_PARSER_VERSION
    done = 0
    for cik in ciks:
        cik10 = _pad(cik)
        f = _latest_507(cik10, start, end); time.sleep(0.15)
        if not f or not f.get("accn"):
            continue
        if (not force) and database.votes_accn_seen(cik10, f["accn"]):
            continue  # already parsed this meeting's filing
        nod = f["accn"].replace("-", "")
        url = f"{ARCHIVE}/{int(cik10)}/{nod}/{f['doc']}" if f.get("doc") else \
              f"{ARCHIVE}/{int(cik10)}/{nod}/{f['accn']}-index.htm"
        r = _get(url); time.sleep(0.1)
        if not r or not r.text:
            continue
        approval, meeting = parse_say_on_pay(r.text)
        if approval is None and not force:
            continue
        # On a forced re-parse, store even None so a previously mis-parsed value is overwritten.
        database.upsert_votes(cik10, approval, None, None, meeting, f["accn"], url)
        if approval is not None:
            done += 1
    if force:
        database.set_meta("votes_parser_version", VOTES_PARSER_VERSION)
    print(f"[votes] parsed say-on-pay for {done} of {len(ciks)} names"
          + ("  (full re-parse: parser " + VOTES_PARSER_VERSION + ")" if force else ""))
    return done
