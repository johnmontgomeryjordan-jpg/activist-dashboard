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
record -- we still locate it via full-text search to read the item codes, then fetch and
parse the document.
"""
import re
import time

import requests

from . import config, database

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
WINDOW_DAYS = 420          # one annual-meeting cycle (+ slack)
_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")
# A real vote tally in an 8-K 5.07 is ALWAYS comma-grouped (e.g. "123,456,789"). Requiring a
# comma is what stops the old bug: the bare-number regex matched "2026" (the meeting year, which
# sits right next to the say-on-pay phrase) as a "vote count", then paired it with a real share
# tally -> 2026 / 300,000,000 ≈ 0% "support". A comma-grouped pattern can't match a plain year.
_TALLY = re.compile(r"\d{1,3}(?:,\d{3})+")
# Label-anchored For/Against: tie each tally to its column label so we don't grab arbitrary
# numbers. Allow a little non-digit slack between the label and its number.
_FOR = re.compile(r"\bfor\b[^\d]{0,25}?(\d{1,3}(?:,\d{3})+)")
_AGAINST = re.compile(r"\bagainst\b[^\d]{0,25}?(\d{1,3}(?:,\d{3})+)")
# A real say-on-pay "For" essentially never falls below ~20%, even when the vote FAILS (failed
# say-on-pay votes cluster around 40-60%). A parsed value below this means we anchored on the
# wrong numbers, so we treat it as UNPARSED (None) rather than emit a false near-0% signal.
MIN_PLAUSIBLE_SOP = 0.20

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
    """Most recent 8-K carrying Item 5.07 for this company, via full-text search."""
    j = None
    for i in range(3):
        try:
            r = _session.get(EFTS_URL, params={"q": "", "forms": "8-K", "ciks": cik10,
                             "startdt": start, "enddt": end}, timeout=25)
            if r.status_code == 200:
                j = r.json(); break
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(1.0 * (i + 1))
    if not j:
        return None
    for h in (j.get("hits", {}) or {}).get("hits", []) or []:
        src = h.get("_source", {})
        if "5.07" not in (src.get("items") or []):
            continue
        adsh, _, doc = (h.get("_id") or "").partition(":")
        if not adsh:
            adsh = src.get("adsh", "")
        return {"accn": adsh, "doc": doc, "date": src.get("file_date")}
    return None


def _iter_positions(low):
    """Every position where a say-on-pay phrase appears, in document order. The phrase is often
    mentioned in the narrative intro BEFORE the results table, so we can't just take the first
    hit — we try each until one yields a plausible For/Against pair (usually the table row)."""
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
    """(for, against) vote tallies from a window. Prefer label-anchored numbers ("For 12,345,678
    … Against 234,567"); fall back to the first two comma-grouped tallies (a flattened
    For/Against/Abstain row). Returns None if two tallies can't be found."""
    fm, am = _FOR.search(window), _AGAINST.search(window)
    if fm and am:
        return int(fm.group(1).replace(",", "")), int(am.group(1).replace(",", ""))
    tallies = [int(t.replace(",", "")) for t in _TALLY.findall(window)]
    if len(tallies) >= 2:
        return tallies[0], tallies[1]
    return None


def parse_say_on_pay(html):
    """Return (approval_fraction, meeting_date) from an 8-K 5.07, or (None, meeting).
    approval = For / (For + Against) on the advisory executive-compensation vote. Returns None
    for approval when no PLAUSIBLE say-on-pay tally is found — never a garbage near-0% value."""
    text = _TAG.sub(" ", html or "")
    low = text.lower()
    m = _HELD.search(text)
    meeting = m.group(1) if m else None
    for pos in _iter_positions(low):
        fa = _for_against(low[pos:pos + 1200])
        if not fa:
            continue
        for_v, against_v = fa
        denom = for_v + against_v
        if denom <= 0:
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
    done = 0
    for cik in ciks:
        cik10 = _pad(cik)
        f = _latest_507(cik10, start, end); time.sleep(0.15)
        if not f or not f.get("accn"):
            continue
        if database.votes_accn_seen(cik10, f["accn"]):
            continue  # already parsed this meeting's filing
        nod = f["accn"].replace("-", "")
        url = f"{ARCHIVE}/{int(cik10)}/{nod}/{f['doc']}" if f.get("doc") else \
              f"{ARCHIVE}/{int(cik10)}/{nod}/{f['accn']}-index.htm"
        r = _get(url); time.sleep(0.1)
        if not r or not r.text:
            continue
        approval, meeting = parse_say_on_pay(r.text)
        if approval is None:
            continue
        database.upsert_votes(cik10, approval, None, None, meeting, f["accn"], url)
        done += 1
    print(f"[votes] parsed say-on-pay for {done} of {len(ciks)} names")
    return done
