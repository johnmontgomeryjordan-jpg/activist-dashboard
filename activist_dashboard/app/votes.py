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
_NUM = re.compile(r"\d[\d,]{3,}")   # comma-grouped vote tallies

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


def parse_say_on_pay(html):
    """Return (approval_fraction, meeting_date) from an 8-K 5.07, or (None, None).
    approval = For / (For + Against) on the advisory executive-compensation vote."""
    text = _TAG.sub(" ", html or "")
    low = text.lower()
    pos = -1
    for p in SOP_PHRASES:
        i = low.find(p)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return None, None
    window = low[pos:pos + 700]
    nums = [int(m.group().replace(",", "")) for m in _NUM.finditer(window)]
    nums = [n for n in nums if n > 1000]          # ignore proposal numbers, etc.
    if len(nums) < 2:
        return None, None
    for_v, against_v = nums[0], nums[1]
    denom = for_v + against_v
    if denom <= 0:
        return None, None
    approval = for_v / denom
    if not (0 < approval <= 1):
        return None, None
    m = _HELD.search(text)
    return approval, (m.group(1) if m else None)


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
