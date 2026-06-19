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
"""
import time
import xml.etree.ElementTree as ET

import requests

from . import config, database

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
WINDOW_DAYS = 120          # trailing window for the insider aggregate
MAX_FORMS_PER_CIK = 60     # safety cap on Form 4s parsed per company per refresh
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


def _recent_form4s(cik10, cut_date):
    """Return [{accn, doc, date}] for Form 4 filings on/after cut_date."""
    r = _get(SUB_URL.format(cik10=cik10))
    if not r:
        return []
    try:
        j = r.json()
    except ValueError:
        return []
    recent = j.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    accs = recent.get("accessionNumber", []) or []
    docs = recent.get("primaryDocument", []) or []
    dates = recent.get("filingDate", []) or []
    out = []
    for i, f in enumerate(forms):
        if f != "4":
            continue
        dt = dates[i] if i < len(dates) else ""
        if dt and dt < cut_date:
            continue
        out.append({"accn": accs[i] if i < len(accs) else "",
                    "doc": docs[i] if i < len(docs) else "",
                    "date": dt})
        if len(out) >= MAX_FORMS_PER_CIK:
            break
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
    cut = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    parsed = 0
    for cik in ciks:
        cik10 = _pad(cik)
        forms = _recent_form4s(cik10, cut); time.sleep(0.12)
        for f in forms:
            accn = f.get("accn")
            if not accn or database.insider_txn_seen(accn):
                continue
            doc = f.get("doc") or ""
            nod = accn.replace("-", "")
            url = f"{ARCHIVE}/{int(cik10)}/{nod}/{doc}"
            idx_url = f"{ARCHIVE}/{int(cik10)}/{nod}/{accn}-index.htm"
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
