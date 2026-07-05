"""
CEO pay-for-performance: parse the Summary Compensation Table from the DEF 14A and
pull the CEO's TOTAL compensation for the reported fiscal years.

"Pay rose while the stock fell" is the single most recognizable activist governance
argument. We parse the proxy we already fetch (governance.py), extract the principal
executive officer's (PEO/CEO) Total-column figures across the years the SCT shows
(usually 3), and hand scoring.py a comp trajectory it compares against TSR.

Design choices (precision over recall — a wrong pay figure in a client pitch is worse
than no figure):
  * Parse the RAW html table (not de-tagged text) so column structure survives.
  * The CEO is identified by a row whose title cell says "chief executive officer"
    (skipping "former"/"interim"); rowspan name cells are handled by carrying the
    current officer across its year sub-rows.
  * Require >= 2 distinct years with plausible Total figures ($50k..$200M) or we
    return None (no signal) rather than guess.
"""
import re
import html as _html

_TR = re.compile(r"<tr\b", re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_NUM = re.compile(r"\(?\$?\s*([0-9][0-9,]{2,})(?:\.\d+)?\)?")

_CEO_TITLE = re.compile(
    r"chief executive officer|president and chief executive|"
    r"\bceo\b|principal executive officer", re.I)
_OTHER_TITLE = re.compile(
    r"chief financial officer|\bcfo\b|chief operating officer|\bcoo\b|"
    r"executive vice president|general counsel|chief accounting|"
    r"president,? [a-z]|senior vice president|chief [a-z]+ officer", re.I)
_SKIP_CEO = re.compile(r"former|interim|principal financial", re.I)

MIN_TOTAL = 50_000
MAX_TOTAL = 200_000_000


def _clean(cell):
    cell = _TAG.sub("", cell)
    cell = _html.unescape(cell)
    cell = cell.replace(" ", " ").replace("—", "-").replace("–", "-")
    return _WS.sub(" ", cell).strip()


def _rows(table_html):
    out = []
    for chunk in _TR.split(table_html)[1:]:
        cells = [_clean(c) for c in _CELL.findall(chunk)]
        cells = [c for c in cells if c != ""]
        if cells:
            out.append(cells)
    return out


def _num(cell):
    m = _NUM.search(cell or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _find_tables(html_text, anchor_re, span=120000, max_tables=4):
    """Yield raw <table> html blocks that begin near an occurrence of anchor_re."""
    low = html_text.lower()
    for m in anchor_re.finditer(low):
        window = html_text[m.end():m.end() + span]
        found = 0
        for tm in re.finditer(r"<table\b", window, re.I):
            ts = tm.start()
            te = window.lower().find("</table>", ts)   # first close (tables rarely nest in SCT)
            if te == -1:
                continue
            yield window[ts:te + 8]
            found += 1
            if found >= max_tables:
                break


def _rightmost_total(cells, year):
    """Total is the rightmost compensation column; scanning right-to-left is robust to
    rowspan'd name cells that shift earlier columns out of a fixed index."""
    for c in reversed(cells):
        n = _num(c)
        if n is not None and n != year and MIN_TOTAL <= n <= MAX_TOTAL:
            return n
    return None


def _parse_sct(table_html):
    rows = _rows(table_html)
    if len(rows) < 2:
        return None
    # locate the header row (Name/Year/Salary/.../Total)
    header_at = None
    for ri, cells in enumerate(rows[:6]):
        joined = " | ".join(c.lower() for c in cells)
        if "salary" in joined and "total" in joined:
            header_at = ri
            break
    if header_at is None:
        return None

    ceo_name = None
    in_ceo = False
    pairs = {}                                   # year -> total
    for cells in rows[header_at + 1:]:
        rowtext = " ".join(cells)
        is_ceo_title = bool(_CEO_TITLE.search(rowtext)) and not _SKIP_CEO.search(rowtext)
        is_other = bool(_OTHER_TITLE.search(rowtext)) and not _CEO_TITLE.search(rowtext)
        if is_ceo_title:
            in_ceo = True
            if ceo_name is None:
                lead = cells[0]
                nm = re.split(_CEO_TITLE, lead)[0].strip(" ,.-")
                if nm and re.search(r"[A-Za-z]", nm):
                    ceo_name = nm
        elif is_other:
            if in_ceo:
                break                            # reached the next (non-CEO) officer
            in_ceo = False
        if not in_ceo:
            continue
        ym = _YEAR.search(rowtext)
        if not ym:
            continue
        year = int(ym.group(0))
        total = _rightmost_total(cells, year)
        if total is not None:
            pairs.setdefault(year, total)

    if len(pairs) < 2:
        return None
    years = sorted(pairs)
    earliest, latest = years[0], years[-1]
    e_tot, l_tot = pairs[earliest], pairs[latest]
    if e_tot <= 0:
        return None
    pct = (l_tot - e_tot) / e_tot
    # CEO-transition detection. The pay-rise % is only meaningful for ONE CEO across the window.
    # If the CEO changed, the earliest year is the new CEO's partial/first year, so a normal ramp
    # reads as a huge false "pay rose" (Signet: a ~3-month stub of $2.8M vs the first full year of
    # $12.0M = 333%). Flag it (two independent tells, scanned over the whole table) so scoring can
    # suppress the signal rather than pitch a transition as a pay-for-performance abuse.
    body = rows[header_at + 1:]
    former_ceo = any(_CEO_TITLE.search(" ".join(c)) and _SKIP_CEO.search(" ".join(c)) for c in body)
    row_years = [int(m.group(0)) for c in body for m in [_YEAR.search(" ".join(c))] if m]
    ceo_started_late = bool(row_years) and earliest > min(row_years)
    return {
        "ceo_name": ceo_name,
        "years": [{"year": y, "total": pairs[y]} for y in years],
        "earliest_year": earliest, "earliest_total": e_tot,
        "latest_year": latest, "latest_total": l_tot,
        "pct_change": pct,
        "ceo_transition": bool(former_ceo or ceo_started_late),
    }


# ---- self-selected compensation peer group ----------------------------------
# Activists love "you trail even the peer group you picked yourself." We locate the
# proxy's compensation peer-group section and match the named companies against OUR
# universe (high precision: we only keep peers we actually have fundamentals for, by
# matching known company names — no fragile free-text name extraction).
_PEER_ANCHORS = (
    "compensation peer group", "compensation peer companies", "compensation comparator",
    "comparator group", "peer group used", "peer group consist", "peer group for",
    "benchmarking peer", "our peer group", "the peer group", "peer group",
)
_PEER_NAME_SUFFIX = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "plc", "ltd", "limited", "lp", "llc", "holdings", "holding", "group", "the",
    "&", "and", "international", "intl", "industries", "enterprises",
}


def _peer_section(detagged_lower):
    for anc in _PEER_ANCHORS:
        i = detagged_lower.find(anc)
        if i != -1:
            return detagged_lower[i:i + 8000]
    return ""


def _name_core(name):
    toks = re.findall(r"[a-z0-9&]+", (name or "").lower())
    while toks and toks[-1] in _PEER_NAME_SUFFIX:
        toks.pop()
    return toks


def find_peers(detagged_text, universe, self_cik=None, min_peers=5):
    """detagged_text: proxy text with tags stripped (lowercased internally).
    universe: list of (cik, name). Returns [cik, ...] of named peers we also cover, or []
    if we can't confidently find at least `min_peers` (precision over recall)."""
    if not detagged_text or not universe:
        return []
    seg = " " + re.sub(r"[^a-z0-9&]+", " ", _peer_section(detagged_text.lower())) + " "
    if seg.strip() == "":
        return []
    out = []
    for cik, name in universe:
        if self_cik is not None and str(cik) == str(self_cik):
            continue
        toks = _name_core(name)
        if not toks:
            continue
        if len(toks) >= 2:                      # multiword core: require the full phrase
            if (" " + " ".join(toks) + " ") in seg:
                out.append(cik)
        else:                                   # single token: must be distinctive (>=6)
            t = toks[0]
            if len(t) >= 6 and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", seg):
                out.append(cik)
    out = list(dict.fromkeys(out))              # dedupe, preserve order
    return out if len(out) >= min_peers else []


def parse_ceo_comp(html_text):
    """Return CEO comp trajectory dict, or None if not confidently parseable."""
    if not html_text:
        return None
    anchor = re.compile(r"summary compensation table", re.I)
    for tbl in _find_tables(html_text, anchor):
        low = tbl.lower()
        if "salary" not in low or "total" not in low:
            continue
        res = _parse_sct(tbl)
        if res:
            return res
    return None
