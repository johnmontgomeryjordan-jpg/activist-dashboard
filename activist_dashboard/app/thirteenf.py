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
from datetime import date

import requests

from . import config, database, activists, pipeline

EFTS = "https://efts.sec.gov/LATEST/search-index"     # EDGAR full-text search (JSON API)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{}.json"   # date-authoritative filing list
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_session = requests.Session()
_session.headers.update(HEADERS)

_TIMEOUT = getattr(config, "THIRTEENF_TIMEOUT", 25)

# The CIK is embedded in the efts entity bucket key, e.g. "Elliott ... (CIK 0001791786)".
_BUCKET_CIK_RE = re.compile(r"\(CIK\s+(\d{10})\)", re.I)

# Several of our fund tokens are tuned for headline / filer matching and are too GENERIC to
# resolve an entity ("elliott" matches dozens of filers). Map those to the proper firm name so
# the full-text query lands on the right 13F filer. efts confirms the match via its entity
# aggregation, and a wrong name simply returns nothing (safe skip) rather than bad data.
_RESOLVE = {
    "elliott": "Elliott Investment Management", "trian": "Trian Fund Management",
    "valueact": "ValueAct Holdings", "value act": "ValueAct Holdings",
    "pershing square": "Pershing Square Capital Management",
    "ancora": "Ancora Advisors", "corvex": "Corvex Management",
    "glenview": "Glenview Capital Management", "soroban": "Soroban Capital Partners",
    "irenic": "Irenic Capital Management", "irenic capital": "Irenic Capital Management",
    "engine no": "Engine No. 1", "engine no. 1": "Engine No. 1",
    "d.e. shaw": "D. E. Shaw", "the children's investment": "TCI Fund Management",
    "tci fund": "TCI Fund Management", "kimmeridge": "Kimmeridge Energy Management",
    "inclusive capital": "Inclusive Capital Partners", "saba capital": "Saba Capital Management",
    "legion partners": "Legion Partners Asset Management", "scopia": "Scopia Capital Management",
    "gatemore": "Gatemore Capital Management", "kanen": "Kanen Wealth Management",
    "bulldog investors": "Bulldog Investors", "steel partners": "Steel Partners Holdings",
    "sarissa": "Sarissa Capital Management", "toms capital": "Toms Capital",
    "wynnefield": "Wynnefield Capital", "macellum": "Macellum",
    "vision one": "Vision One Management", "mhr fund": "MHR Fund Management",
    "adw capital": "ADW Capital", "alta fox": "Alta Fox Capital",
    "amber capital": "Amber Capital", "anson funds": "Anson Funds Management",
    "holdco asset": "HoldCo Asset Management", "p2 capital": "P2 Capital Partners",
    "palliser": "Palliser Capital", "parvus": "Parvus Asset Management",
    "pl capital": "PL Capital", "dalton investments": "Dalton Investments",
    "oasis management": "Oasis Management", "carronade": "Carronade Capital",
    "stilwell": "Stilwell Value", "cevian": "Cevian Capital",
}
# Marquee activists whose 13F filer isn't captured by a FUNDS token (e.g. individuals whose fund
# files under a firm name). Resolved in addition to activists.FUNDS.
_EXTRA_FUNDS = ["Icahn Capital", "JANA Partners", "Third Point", "Impactive Capital"]


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


def _latest_13f_for_cik(cik10):
    """Newest (accession, filing_date) 13F-HR for a filer, from the SEC submissions API — which
    lists filings date-descending, so this is authoritative (efts hits are relevance-ranked, so
    their newest-by-date can miss the actual latest filing)."""
    j = _get(SUBMISSIONS.format(cik10), want="json")
    if not j:
        return None
    rec = ((j.get("filings", {}) or {}).get("recent", {}) or {})
    forms = rec.get("form", []) or []
    accns = rec.get("accessionNumber", []) or []
    dates = rec.get("filingDate", []) or []
    best = None
    for i, f in enumerate(forms):
        if f != "13F-HR":                              # original quarterly report (has holdings)
            continue
        d = dates[i] if i < len(dates) else ""
        a = accns[i] if i < len(accns) else ""
        if a and (best is None or d > best[1]):
            best = (a, d)
    return best


def _too_old(filed):
    """True if a filing date (YYYY-MM-DD) is older than STALE_13F_DAYS — i.e. the filer has
    stopped filing quarterly, so its book is stale and shouldn't be shown."""
    if not filed or len(filed) < 10:
        return False
    try:
        age = (date.today() - date(int(filed[:4]), int(filed[5:7]), int(filed[8:10]))).days
    except (ValueError, TypeError):
        return False
    return age > getattr(config, "STALE_13F_DAYS", 400)


def resolve_and_latest(fund):
    """(cik10, accession, filing_date) for the newest 13F-HR filed by `fund`. efts phrase-matches
    the (proper) fund name and its `entity_filter` aggregation pins the actual filer CIK
    (disambiguating generic names browse-edgar couldn't); the newest filing then comes from the
    date-authoritative submissions API. Returns None if nothing resolves."""
    query = _RESOLVE.get(fund, fund)
    j = _get(EFTS, {"q": f'"{query}"', "forms": "13F-HR"}, want="json")
    if not j:
        return None
    buckets = (((j.get("aggregations", {}) or {}).get("entity_filter", {}) or {})
               .get("buckets", []) or [])
    if not buckets:
        return None
    m = _BUCKET_CIK_RE.search(buckets[0].get("key", ""))   # dominant filer = most 13F docs
    if not m:
        return None
    cik10 = m.group(1)
    latest = _latest_13f_for_cik(cik10)
    time.sleep(0.15)
    if latest:
        return cik10, latest[0], latest[1]
    # fallback: newest 13F-HR among the efts hits (used only if submissions is unavailable)
    best = None
    for h in (j.get("hits", {}) or {}).get("hits", []) or []:
        s = h.get("_source", {}) or {}
        if (s.get("ciks") or [None])[0] != cik10 or "13F-HR" not in (s.get("root_forms") or []):
            continue
        fd, adsh = s.get("file_date"), s.get("adsh")
        if adsh and (best is None or (fd or "") > best[1]):
            best = (adsh, fd or "")
    return (cik10, best[0], best[1]) if best else None


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
    funds = activists.FUNDS[:getattr(config, "THIRTEENF_MAX_FUNDS", 80)] + _EXTRA_FUNDS
    universe = {(c.get("ticker") or "").upper() for c in database.get_companies()
                if c.get("ticker")}
    shares_out = {}
    for f in database.get_all_fundamentals():
        tk = (f.get("ticker") or "").upper()
        if tk and f.get("shares"):
            shares_out[tk] = f["shares"]

    # Wipe first (pre-loop reads have succeeded) so no stale rows from a prior run survive; the
    # per-fund guard below means the run still completes and repopulates even if some funds fail.
    database.clear_all_holdings()
    n_funds = 0
    n_rows = 0
    n_err = 0
    seen_ciks = set()                                  # dedupe funds that resolve to one filer
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
            if _too_old(filed):                        # filer stopped filing -> skip stale book
                continue
            # Two list entries can name the same firm ("starboard" / "starboard value",
            # "irenic" / "irenic capital"): both resolve to one 13F CIK, so skip the repeat
            # rather than store the same book twice.
            if cik10 in seen_ciks:
                continue
            seen_ciks.add(cik10)
            database.upsert_activist_filer(fund, cik10, "browse-edgar")
            xml = latest_infotable(cik10, accession)
            time.sleep(0.2)
            holds = parse_infotable(xml)
            if not holds:
                n_funds += 1
                continue
            port_total = sum(h["value"] for h in holds if h.get("value")) or 0.0
            period = _period_label(filed)
            # Aggregate BY TICKER within the fund: a 13F can list one issuer under several
            # CUSIPs/share classes (or two CUSIPs can map to one ticker). Summing value + shares
            # gives the fund's true total position in that name and avoids a duplicate-key row.
            agg = {}
            for h in holds:
                # NOTE (2026-08): was database.ticker_for_cusip() directly, which only checks
                # the FTD-derived cusip_map + entity.cusip and silently drops anything missing
                # from both -- an ordinary, non-heavily-shorted name (e.g. MNRO) can have no FTD
                # history at all and never enter that map. pipeline.resolve_cusip() is the same
                # cached lookup PLUS an OpenFIGI gap-fill (caching the hit back into cusip_map),
                # and is already the documented single call the 13F parser is meant to use.
                tk = pipeline.resolve_cusip(h["cusip"])
                if not tk:
                    continue
                tk = tk.upper()
                if tk not in universe:                 # only names we actively monitor
                    continue
                a = agg.setdefault(tk, {"value": 0.0, "shares": 0.0, "cusip": h["cusip"]})
                a["value"] += (h.get("value") or 0.0)
                a["shares"] += (h.get("shares") or 0.0)
            rows = []
            for tk, a in agg.items():
                weight = (a["value"] / port_total) if port_total else None
                so = shares_out.get(tk)
                own = (a["shares"] / so) if (so and a["shares"]) else None
                if own is not None and own > getattr(config, "OWNERSHIP_MAX", 1.0):
                    own = None                         # >100% -> bad shares-outstanding, discard
                rows.append((tk, fund, cik10, a["cusip"], a["value"] or None,
                             a["shares"] or None, weight, own, filed, period))
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
