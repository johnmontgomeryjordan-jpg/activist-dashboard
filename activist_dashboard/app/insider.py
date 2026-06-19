"""
Insider transaction signal from SEC Form 4 (free, EDGAR).

This is a LEADING signal: clustered open-market *selling* by insiders is a crack in
confidence that often precedes trouble; open-market *buying* is the opposite -- a sign
insiders are aligned and confident, which makes a company a less inviting activist target.

We parse only OPEN-MARKET transactions:
  code P -- open-market / private purchase  (acquired)
  code S -- open-market / private sale      (disposed)
and deliberately IGNORE grants (A), option exercises (M), tax withholding (F), gifts (G)
and the like, which are routine compensation mechanics, not conviction trades.

Parsed only for the names that matter (shortlist / watchlist / active situations) and
CACHED per Form 4 accession, so each filing is parsed once. Aggregates are recomputed
over a trailing window each refresh.

NOTE: a Form 4 is filed BY the insider, so it does NOT appear in the company's own
submissions feed (that feed is filer-centric). We therefore find a company's Form 4s
via EDGAR full-text search filtered by the issuer's CIK -- each hit's `ciks` array
contains both the insider (filer) and the issuer, and the filing's XML lives under the
filer's CIK.
"""
import time
import xml.etree.ElementTree as ET

import requests

from . import config, database

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
WINDOW_DAYS = 120          # trailing window for the insider aggregate
_session = requests.Session()
_session.headers.update(HEADERS)


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


def _localname(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _find(el, name):
    for c in el.iter():
        if _localname(c.tag) == name:
            return c
    return None


def _text(el, name):
    c = _find(el, name)
    return (c.text or "").strip() if c is not None else ""


def _val(el, name):
    """Form 4 wraps amounts as <name><value>X</value></name>; return the inner value
    (or the element's own text when there's no nested <value>)."""
    node = _find(el, name)
    if node is None:
        return ""
    v = _find(node, "value")
    if v is not None and v.text:
        return v.text.strip()
    return (node.text or "").strip()


def _float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _recent_form4s(cik10, start, end):
    """Return [{accn, doc, date, filer}] for Form 4 filings where cik10 is the issuer,
    via EDGAR full-text search (the company's own submissions feed does NOT list them)."""
    j = None
    for i in range(3):
        try:
            r = _session.get(EFTS_URL, params={"q": "", "forms": "4", "ciks": cik10,
                             "startdt": start, "enddt": end}, timeout=25)
            if r.status_code == 200:
                j = r.json(); break
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return []
        except (requests.RequestException, ValueError):
            time.sleep(1.0 * (i + 1))
    if not j:
        return []
    subj = cik10.lstrip("0")
    out = []
    for h in (j.get("hits", {}) or {}).get("hits", []) or []:
        src = h.get("_source", {})
        adsh, _, doc = (h.get("_id") or "").partition(":")
        if not adsh:
            adsh = src.get("adsh", "")
        ciks = src.get("ciks", []) or []
        # the Form 4 XML lives under the filer (insider) CIK, i.e. the non-issuer cik
        filer = next((c for c in ciks if c.lstrip("0") != subj),
                     ciks[0] if ciks else cik10)
        out.append({"accn": adsh, "doc": doc, "date": src.get("file_date"),
                    "filer": filer})
    return out


def parse_form4(xml_text):
    """Return (name, role, buy_value, sell_value) of OPEN-MARKET trades in one Form 4."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    name = _text(root, "rptOwnerName")
    rel = _find(root, "reportingOwnerRelationship")
    role = "Insider"
    if rel is not None:
        is_dir = _text(rel, "isDirector") in ("1", "true")
        is_off = _text(rel, "isOfficer") in ("1", "true")
        title = _text(rel, "officerTitle")
        if is_off and title:
            role = title
        elif is_off:
            role = "Officer"
        elif is_dir:
            role = "Director"
        elif _text(rel, "isTenPercentOwner") in ("1", "true"):
            role = "10% owner"
    buy_value = sell_value = 0.0
    for tx in root.iter():
        if _localname(tx.tag) != "nonDerivativeTransaction":
            continue
        code = _val(tx, "transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _float(_val(tx, "transactionShares"))
        price = _float(_val(tx, "transactionPricePerShare"))
        if shares is None:
            continue
        value = shares * (price or 0)
        if code == "P":
            buy_value += value
        else:
            sell_value += value
    return name, role, buy_value, sell_value


def refresh_insider(ciks, window_days=WINDOW_DAYS):
    """Parse new Form 4s for each (padded) CIK and recompute its insider aggregate.
    Returns the number of newly-parsed Form 4 filings."""
    from datetime import datetime, timedelta
    start = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    end = datetime.utcnow().date().isoformat()
    parsed = 0
    for cik in ciks:
        cik10 = _pad(cik)
        forms = _recent_form4s(cik10, start, end); time.sleep(0.15)
        for f in forms:
            accn = f.get("accn")
            if not accn or database.insider_txn_seen(accn):
                continue
            doc = f.get("doc") or ""
            filer = f.get("filer") or cik10
            nod = accn.replace("-", "")
            url = f"{ARCHIVE}/{int(filer)}/{nod}/{doc}"
            idx_url = f"{ARCHIVE}/{int(filer)}/{nod}/{accn}-index.htm"
            r = _get(url); time.sleep(0.1)
            if not r or not r.text or "<" not in r.text:
                # Cache a no-op so we don't refetch a non-XML primary doc forever.
                database.add_insider_txn(accn, cik10, f.get("date"), "", "", 0, 0, idx_url)
                continue
            res = parse_form4(r.text)
            if not res:
                database.add_insider_txn(accn, cik10, f.get("date"), "", "", 0, 0, idx_url)
                continue
            name, role, buy_v, sell_v = res
            database.add_insider_txn(accn, cik10, f.get("date"),
                                     f"{name} ({role})" if name else "", role,
                                     buy_v, sell_v, idx_url)
            parsed += 1
        database.recompute_insider(cik10, window_days)
    print(f"[insider] parsed {parsed} new Form 4 filings across {len(ciks)} names")
    return parsed
