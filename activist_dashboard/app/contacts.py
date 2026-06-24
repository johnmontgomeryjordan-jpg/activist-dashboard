"""
IR / Comms contact extraction from 8-K press-release exhibits (EX-99.1) on SEC EDGAR.

Companies print their investor and media contacts at the bottom of earnings / news press
releases. We pull the most recent 8-K exhibit, strip it to text, and parse the contact
blocks into {name, email, phone}, classified as Investor (IR) or Media (Comms). This is
the "who to call" line of the pitch kit.

Formatting is wildly inconsistent across filers, so the parser is heuristic and
deliberately conservative: it only accepts an email that sits near a contact cue
(investor / media / press / relations / contact), and it would rather return an email
with no name than guess a wrong name. Gated by SEC fair-access pacing. Free SEC data.
"""
import re

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{nod}/{name}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nod}/index.json"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
NAME_RE = re.compile(r"[A-Z][a-zA-Z'’.\-]+(?:\s+[A-Z]\.?){0,2}\s+[A-Z][a-zA-Z'’.\-]+")

# Words that look like a name to the regex but are titles / labels, not people.
_NOT_NAME = {
    "investor", "investors", "relations", "media", "press", "contact", "contacts",
    "communications", "communication", "vice", "president", "chief", "executive",
    "officer", "director", "senior", "manager", "corporate", "public", "global",
    "head", "external", "affairs", "group", "financial", "company", "inc",
    "incorporated", "corp", "corporation", "information", "about", "source", "for",
    "more", "investor relations", "relations contact", " &", "and", "the", "ir",
    "vp", "evp", "svp", "cfo", "ceo", "department", "team", "office", "north",
    "america", "general", "counsel", "strategy", "finance", "treasurer",
}
_IR_CUE = re.compile(r"investor|\bir\b|shareholder", re.I)
_PR_CUE = re.compile(r"media|press|communication|public relation|corporate affairs|\bpr\b", re.I)
_ANY_CUE = re.compile(r"contact|investor|media|press|relations|communication|inquiries", re.I)


def _name_ok(s):
    toks = [t for t in re.split(r"\s+", s.strip()) if t]
    if not (2 <= len(toks) <= 4):
        return False
    for t in toks:
        if t.lower().strip(".,&'’-") in _NOT_NAME:
            return False
    return True


def _nearest_name(window):
    """Best 'First Last' in the window that isn't a title/label; prefer the LAST one
    (closest to the email, which usually trails the name + title)."""
    best = None
    for m in NAME_RE.finditer(window):
        cand = re.sub(r"\s+", " ", m.group(0)).strip()
        if _name_ok(cand):
            best = cand
    return best


def _clean_phone(s):
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"


def _nearest_phone(text, epos, floor, ceil):
    """Phone closest to the email, searched only within this contact's block (clamped by
    the surrounding emails so we never grab the next/previous contact's number)."""
    ws, we = max(floor, epos - 160), min(ceil, epos + 60)
    best, bestd = None, 10 ** 9
    for m in PHONE_RE.finditer(text, ws, we):
        p = _clean_phone(m.group(0))
        if not p:
            continue
        d = min(abs(m.start() - epos), abs(m.end() - epos))
        if d < bestd:
            bestd, best = d, p
    return best


def parse_contacts(text):
    """text: plain text of a press release. Returns {'ir': {...}|None, 'comms': {...}|None}.
    Each contact is {name, email, phone} (name/phone may be None)."""
    if not text:
        return {"ir": None, "comms": None}
    text = re.sub(r"[ \t]+", " ", text)
    emails = [m for m in EMAIL_RE.finditer(text)]
    ir = comms = general = None
    for i, m in enumerate(emails):
        email = m.group(0).rstrip(".,;)")
        if email.lower().endswith((".png", ".jpg", ".gif")):
            continue
        start = m.start()
        floor = emails[i - 1].end() if i > 0 else 0      # don't cross the prior email's block
        ceil = emails[i + 1].start() if i + 1 < len(emails) else len(text)
        window = text[max(floor, start - 300):start + 20]
        if not _ANY_CUE.search(window):
            continue                                     # not a contact email -> skip
        rec = {"name": _nearest_name(window), "email": email,
               "phone": _nearest_phone(text, start, floor, ceil)}
        ir_hit = bool(_IR_CUE.search(window))
        pr_hit = bool(_PR_CUE.search(window))
        # classify by the cue closest to the email when both appear
        if ir_hit and pr_hit:
            ir_pos = max((mm.start() for mm in _IR_CUE.finditer(window)), default=-1)
            pr_pos = max((mm.start() for mm in _PR_CUE.finditer(window)), default=-1)
            ir_hit, pr_hit = (ir_pos >= pr_pos, pr_pos > ir_pos)
        if ir_hit and not ir:
            ir = rec
        elif pr_hit and not comms:
            comms = rec
        elif not ir_hit and not pr_hit and not general:
            general = rec
    # a lone "general" contact (e.g. just "Investor Relations dept") fills IR if empty
    if not ir and general:
        ir = general
    return {"ir": ir, "comms": comms}


def _strip_html(html):
    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
                .replace("&rsquo;", "’").replace("&#39;", "'").replace("&ldquo;", '"')
                .replace("&rdquo;", '"').replace("&mdash;", "—"))
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html


_EX99_RE = re.compile(r"ex.?-?_?99", re.I)


def _pick_exhibit(items):
    """From an index.json directory listing, choose the press-release exhibit document."""
    htmls = [it for it in items if str(it.get("name", "")).lower().endswith((".htm", ".html"))]
    ex99 = [it for it in htmls if _EX99_RE.search(it.get("name", ""))]
    # prefer 99.1 specifically
    for it in ex99:
        if re.search(r"99[._-]?1", it.get("name", ""), re.I):
            return it.get("name")
    if ex99:
        return ex99[0].get("name")
    return None


def fetch_contacts(cik, sess, max_filings=6):
    """Pull the most recent 8-K press-release exhibit for one company and parse its
    contacts. `sess` is a requests session with a proper SEC User-Agent. Returns
    {'ir':..., 'comms':..., 'source_url':...} or None. Network failures degrade to None."""
    import time
    cik_int = str(int(cik))
    cik10 = str(int(cik)).zfill(10)
    try:
        r = sess.get(SUBMISSIONS_URL.format(cik10=cik10), timeout=20)
        if r.status_code != 200:
            return None
        sub = r.json()
    except Exception:
        return None
    recent = (sub.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    accns = recent.get("accessionNumber", []) or []
    seen = 0
    for form, accn in zip(forms, accns):
        if form != "8-K":
            continue
        seen += 1
        if seen > max_filings:
            break
        nod = accn.replace("-", "")
        try:
            ir = sess.get(INDEX_URL.format(cik=cik_int, nod=nod), timeout=20); time.sleep(0.12)
            if ir.status_code != 200:
                continue
            items = (ir.json().get("directory", {}) or {}).get("item", []) or []
        except Exception:
            continue
        doc = _pick_exhibit(items)
        if not doc:
            continue
        url = ARCHIVE.format(cik=cik_int, nod=nod, name=doc)
        try:
            dr = sess.get(url, timeout=25); time.sleep(0.12)
            if dr.status_code != 200:
                continue
            parsed = parse_contacts(_strip_html(dr.text))
        except Exception:
            continue
        if parsed.get("ir") or parsed.get("comms"):
            parsed["source_url"] = url
            return parsed
    return None
