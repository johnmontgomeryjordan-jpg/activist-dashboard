"""
Recent advisors — the law firms and investment banks a company has recently used, pulled free
from its SEC 8-K press-release exhibits.

Why it matters: knowing a target is already lawyered up (Wachtell, Skadden) or who its banker is
(Goldman, Centerview) shapes how — and whether — we approach. We scan the text of recent
press-release / deal 8-Ks (Items 1.01, 2.01, 7.01, 8.01) against a CURATED ALLOWLIST of major
firms. Allowlist (not open-ended name extraction) keeps precision high — a wrong "advisor" in a
partner-facing profile is worse than none. Proxy solicitors are deliberately excluded (per FGS).

Bounded: runs on TRACKED names only, a handful of recent 8-Ks each, reusing edgar's SEC fetchers.
"""
import re
from datetime import datetime, timedelta

from . import edgar

# ---- allowlist ---------------------------------------------------------------
# canonical display name -> match pattern (lowercased, word-boundaried at use).
BANKS = {
    "Goldman Sachs": r"goldman,?\s+sachs",
    "Morgan Stanley": r"morgan stanley",
    "J.P. Morgan": r"j\.?\s?p\.?\s?morgan|jpmorgan",
    "BofA Securities": r"bofa securities|bank of america|merrill lynch",
    "Citi": r"citigroup|citibank|\bciti\b",
    "Evercore": r"evercore",
    "Centerview Partners": r"centerview",
    "Lazard": r"lazard",
    "Moelis & Company": r"moelis",
    "PJT Partners": r"pjt partners",
    "Jefferies": r"jefferies",
    "Perella Weinberg": r"perella weinberg",
    "Houlihan Lokey": r"houlihan lokey",
    "Rothschild & Co": r"rothschild",
    "Guggenheim Securities": r"guggenheim (?:securities|partners)",
    "Barclays": r"barclays",
    "UBS": r"\bubs\b",
    "Deutsche Bank": r"deutsche bank",
    "Wells Fargo Securities": r"wells fargo securities",
    "RBC Capital Markets": r"rbc capital",
    "Qatalyst Partners": r"qatalyst",
    "Ducera Partners": r"ducera",
    "Raymond James": r"raymond james",
    "William Blair": r"william blair",
    "Piper Sandler": r"piper sandler",
    "Truist Securities": r"truist securities",
}
LAW = {
    "Wachtell Lipton": r"wachtell",
    "Skadden": r"skadden",
    "Sullivan & Cromwell": r"sullivan\s+(?:&|and)\s+cromwell",
    "Cravath": r"cravath",
    "Davis Polk": r"davis polk",
    "Simpson Thacher": r"simpson thacher",
    "Latham & Watkins": r"latham\s+(?:&|and)\s+watkins",
    "Kirkland & Ellis": r"kirkland\s+(?:&|and)\s+ellis",
    "Paul Weiss": r"paul,?\s+weiss",
    "Cleary Gottlieb": r"cleary gottlieb",
    "Gibson Dunn": r"gibson,?\s+dunn",
    "Sidley Austin": r"sidley",
    "Weil Gotshal": r"weil,?\s+gotshal",
    "Wilson Sonsini": r"wilson sonsini",
    "Cooley": r"cooley llp",
    "Jones Day": r"jones day",
    "Fried Frank": r"fried,?\s+frank",
    "Debevoise": r"debevoise",
    "Ropes & Gray": r"ropes\s+(?:&|and)\s+gray",
    "Morrison Foerster": r"morrison\s+(?:&\s+)?foerster",
    "Vinson & Elkins": r"vinson\s+(?:&|and)\s+elkins",
    "Freshfields": r"freshfields",
    "Richards Layton & Finger": r"richards,?\s+layton",
    "Potter Anderson": r"potter anderson",
    "Schulte Roth": r"schulte roth",
    "Olshan Frome": r"olshan",
    "White & Case": r"white\s+(?:&|and)\s+case",
    "Willkie Farr": r"willkie",
    "Hogan Lovells": r"hogan lovells",
}

_BANK_RE = [(k, re.compile(r"\b" + v + r"\b", re.I)) for k, v in BANKS.items()]
_LAW_RE = [(k, re.compile(r"\b" + v + r"\b", re.I)) for k, v in LAW.items()]

# 8-K items that typically carry a press release naming advisors (deals / strategic events).
_ADVISOR_ITEMS = ("1.01", "2.01", "7.01", "8.01")
# A real advisor mention sits near advisory language — guards against a stray brand mention.
_CONTEXT = re.compile(
    r"financial advisor|financial adviser|legal (?:advisor|adviser|counsel)|"
    r"acting as (?:advisor|adviser|counsel)|is serving as|served as|is acting as|"
    r"advising|counsel to|advisor to|adviser to|exclusive financial|lead financial|"
    r"is representing|represented by", re.I)


def scan(text):
    """Return [{'name','type'}] of allowlisted advisors named in `text` — but only when the text
    also contains advisory language, so a passing brand mention (e.g. a bank as lender) is ignored."""
    t = text or ""
    if not t or not _CONTEXT.search(t):
        return []
    out = []
    for name, rx in _LAW_RE:
        if rx.search(t):
            out.append({"name": name, "type": "law"})
    for name, rx in _BANK_RE:
        if rx.search(t):
            out.append({"name": name, "type": "bank"})
    return out


def advisors_for_cik(cik, ticker="", company="", days=730, max_docs=8):
    """Scan a tracked company's recent press-release/deal 8-Ks for allowlisted advisors.
    Returns {'advisors': [{'name','type'}...], 'source_url': str|None, 'source_date': str|None}."""
    cik10 = edgar.pad_cik(cik)
    r = edgar._get(edgar.SUBMISSIONS_URL.format(cik10=cik10))
    if not r:
        return {"advisors": [], "source_url": None, "source_date": None}
    try:
        data = r.json()
    except ValueError:
        return {"advisors": [], "source_url": None, "source_date": None}
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accs = recent.get("accessionNumber", []) or []
    docs = recent.get("primaryDocument", []) or []
    items_l = recent.get("items", []) or []
    cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()

    found = {}
    source_url = source_date = None
    scanned = 0
    for i, f in enumerate(forms):
        if scanned >= max_docs:
            break
        if f != "8-K":
            continue
        filed = dates[i] if i < len(dates) else ""
        if filed < cutoff:
            continue
        codes = items_l[i] if i < len(items_l) else ""
        if not any(c in codes for c in _ADVISOR_ITEMS):
            continue
        acc = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        if not acc or not doc:
            continue
        nod = acc.replace("-", "")
        text = edgar._doc_text(int(cik10), nod, doc)
        scanned += 1
        hits = scan(text)
        for h in hits:
            found.setdefault(h["name"], h)
        if hits and source_url is None:          # remember the newest filing that named advisors
            source_url = f"{edgar.ARCHIVE_BASE}/{int(cik10)}/{nod}/{doc}"
            source_date = filed
    return {"advisors": sorted(found.values(), key=lambda x: (x["type"], x["name"])),
            "source_url": source_url, "source_date": source_date}
