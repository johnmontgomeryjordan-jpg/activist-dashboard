"""
13F activist-holder signal (the "early-warning" tier).

A known activist that has already BOUGHT into one of our names but hasn't yet agitated
(no 13D, no proxy fight) is the sweet spot our whole thesis is built on: the smart money is
in the stock before the campaign starts. This module finds those quiet positions.

How it works, all on free SEC data:
  1. For each activist fund on our list, EDGAR's company search (browse-edgar, output=atom)
     filtered to type=13F-HR returns the fund's 13F-FILING CIK *and* its newest 13F-HR
     accession in one call. (The 13F filer CIK is often a different entity than the fund's
     13D/proxy filer, so we resolve it fresh rather than reuse activists.py CIKs.)
  2. The filing's information table (infotable.xml) lists every long US-equity position with
     CUSIP, market value, and share count -- the standard SEC 13F schema.
  3. We map each CUSIP -> ticker via the free FTD/OpenFIGI cusip_map (U2b) and keep only names
     in our monitored universe.
  4. For each kept position we compute two materiality measures: conviction (position value as
     a share of the fund's whole 13F book) and ownership (shares held / shares outstanding).
     scoring.py fires the `activist_holder` signal when EITHER clears its bar.

13F is quarterly and lagged ~45 days, which is fine for a "they're in the stock" early warning
-- activist positions don't churn daily. A weekly refresh (main.py) is ample.
"""
import re
import time
import xml.etree.ElementTree as ET

import requests

from . import config, database, activists

BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_session = requests.Session()
_session.headers.update(HEADERS)

_TIMEOUT = getattr(config, "THIRTEENF_TIMEOUT", 25)

# In the browse-edgar atom, the filer's own CIK sits in <company-info><cik>..</cik> (before any
# filing entries), and each filing entry carries an <accession-number> newest-first.
_CIK_RE = re.compile(r"<cik>\s*(\d{10})\s*</cik>", re.I)
_ACCN_RE = re.compile(r"<accession-number>\s*([\dA-Za-z-]+)\s*</accession-number>", re.I)
_DATE_RE = re.compile(r"<filing-date>\s*([\d-]+)\s*</filing-date>", re.I)


def _get(url, params=None, want="text"):
    """GET with SEC-friendly retries/backoff. Returns text (or parsed json), else None."""
    for i in range(4):
        try:
            r = _session.get(url, params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json() if want == "json" else r.text
            if r.status_code == 429:                 # rate-limited -- back off and retry
                time.sleep(2.0 * (i + 1)); continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(1.5 * (i + 1))
    return None


def _localname(tag):
    return tag.split("}")[-1] if tag else tag


def _period_label(filed):
    """Rough 'as-of' quarter for a 13F from its FILING month (filed ~45 days after quarter-end).
    Cosmetic only -- the materiality math uses value/shares, not the label."""
    if not filed or len(filed) < 7:
        return ""
    try:
        y, m = int(filed[:4]), int(filed[5:7])
    except ValueError:
        return ""
    # Feb -> prior Q4, May -> Q1, Aug -> Q2, Nov -> Q3 (of the same/prior year).
    if m <= 3:
        return f"Q4 {y - 1}"
    if m <= 6:
        return f"Q1 {y}"
    if m <= 9:
        return f"Q2 {y}"
    return f"Q3 {y}"


def resolve_and_latest(fund):
    """(cik10, accession, filing_date) for the newest 13F-HR filed under `fund`, or None if the
    fund can't be resolved to a single 13F filer (ambiguous name -> no filing entries -> skip)."""
    txt = _get(BROWSE, {"action": "getcompany", "company": fund, "type": "13F-HR",
                        "dateb": "", "owner": "include", "count": "10", "output": "atom"})
    if not txt:
        return None
    mc = _CIK_RE.search(txt)
    ma = _ACCN_RE.search(txt)          # first (newest) 13F-HR accession
    if not mc or not ma:               # no single filer, or no 13F on file -> skip this fund
        return None
    md = _DATE_RE.search(txt)
    return mc.group(1), ma.group(1), (md.group(1) if md else None)


def latest_infotable(cik10, accession):
    """Fetch the information-table XML text for a 13F filing (via its folder index.json)."""
    cik = str(int(cik10))
    nod = accession.replace("-", "")
    idx = _get(f"{ARCHIVE}/{cik}/{nod}/index.json", want="json")
    if not idx:
        return None
    items = (idx.get("directory") or {}).get("item") or []
    name = None
    for it in items:                                   # prefer an obvious info-table file name
        n = (it.get("name") or "").lower()
        if n.endswith(".xml") and ("infotable" in n or "form13f" in n or "information" in n):
            name = it["name"]; break
    if not name:                                       # else any xml that isn't the cover doc
        for it in items:
            n = (it.get("name") or "").lower()
            if n.endswith(".xml") and "primary_doc" not in n:
                name = it["name"]; break
    if not name:
        return None
    return _get(f"{ARCHIVE}/{cik}/{nod}/{name}")


def parse_infotable(xml_text):
    """Parse a 13F information table into [{cusip, name, value, shares}]. Namespace-agnostic
    (matches by local tag name) so it works regardless of the SEC schema prefix."""
    out = []
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter():
        if _localname(it.tag) != "infoTable":
            continue
        rec = {"cusip": None, "name": None, "value": None, "shares": None}
        for ch in it.iter():
            ln = _localname(ch.tag)
            txt = (ch.text or "").strip()
            if not txt:
                continue
            if ln == "cusip" and rec["cusip"] is None:
                rec["cusip"] = txt.upper()
            elif ln == "nameOfIssuer" and rec["name"] is None:
                rec["name"] = txt
            elif ln == "value" and rec["value"] is None:
                try: rec["value"] = float(txt.replace(",", ""))
                except ValueError: pass
            elif ln == "sshPrnamt" and rec["shares"] is None:
                try: rec["shares"] = float(txt.replace(",", ""))
                except ValueError: pass
        if rec["cusip"]:
            out.append(rec)
    return out


def refresh_13f():
    """Full refresh: resolve every activist fund's 13F filer, pull its latest info table, map
    holdings onto our universe, compute conviction + ownership, and store. Returns the number
    of universe holdings mapped. Non-destructive per fund: a fund we can't resolve this run
    keeps its previously stored holdings (they only get replaced when a fresh 13F parses)."""
    funds = activists.FUNDS[:getattr(config, "THIRTEENF_MAX_FUNDS", 80)]
    universe = {(c.get("ticker") or "").upper() for c in database.get_companies()
                if c.get("ticker")}
    shares_out = {}
    for f in database.get_all_fundamentals():
        tk = (f.get("ticker") or "").upper()
        if tk and f.get("shares"):
            shares_out[tk] = f["shares"]

    n_funds = 0
    n_rows = 0
    n_err = 0
    for fund in funds:
        # Per-fund guard: one bad fund (a network hiccup, a rate-limit, an odd response) must
        # NEVER abort the whole sweep. We log the offender and move on, and always reach the
        # last_run stamp below so the run is visibly complete even if some funds failed.
        try:
            res = resolve_and_latest(fund)
            time.sleep(0.2)                            # stay under SEC's 10 req/s
            if not res:
                continue
            cik10, accession, filed = res
            database.upsert_activist_filer(fund, cik10, "browse-edgar")
            xml = latest_infotable(cik10, accession)
            time.sleep(0.2)
            holds = parse_infotable(xml)
            if not holds:
                n_funds += 1
                continue
            port_total = sum(h["value"] for h in holds if h.get("value")) or 0.0
            period = _period_label(filed)
            rows = []
            for h in holds:
                tk = database.ticker_for_cusip(h["cusip"])
                if not tk:
                    continue
                tk = tk.upper()
                if tk not in universe:                 # only names we actively monitor
                    continue
                weight = (h["value"] / port_total) if (port_total and h.get("value")) else None
                so = shares_out.get(tk)
                own = (h["shares"] / so) if (so and h.get("shares")) else None
                rows.append((tk, fund, cik10, h["cusip"], h.get("value"),
                             h.get("shares"), weight, own, filed, period))
            if rows:
                database.replace_holdings_for_fund(fund, rows)
                n_rows += len(rows)
            n_funds += 1
        except Exception as e:                         # keep sweeping; surface the offender
            n_err += 1
            print(f"[13f] fund '{fund}' failed: {type(e).__name__}: {e}")

    database.set_meta("thirteenf_last_run", database.now_iso())
    print(f"[13f] resolved {n_funds} funds; {n_rows} universe holdings mapped; {n_err} errored")
    return n_rows
