"""
Governance red-flag detection from the latest DEF 14A proxy statement (free, SEC).

Activists screen hard on entrenchment. We detect three high-precision, high-signal
red flags via text patterns in the proxy:

  * classified_board -- staggered board (directors elected in classes)
  * poison_pill      -- shareholder rights plan in place
  * dual_class       -- super-voting / multiple-vote share structure

Parsed only for the names that matter (shortlist / watchlist / active situations)
and CACHED by filing accession, so each proxy is parsed once (they're annual).
We deliberately skip "combined Chair/CEO" -- the phrase appears in proxies that
*separated* the roles too, so it's false-positive-prone.
"""
import re
import time

import requests

from . import config, database, compensation

HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
_session = requests.Session()
_session.headers.update(HEADERS)
_TAG = re.compile(r"<[^>]+>")

# High-precision phrases (lowercased). Chosen to avoid common false friends, e.g.
# "registration rights agreement" must NOT trip the poison-pill flag.
CLASSIFIED = ["classified board", "staggered board", "divided into three classes",
              "three classes of directors", "elected for three-year terms",
              "board is divided into classes"]
POISON = ["poison pill", "shareholder rights plan", "stockholder rights plan",
          "preferred share purchase right", "preferred stock purchase right"]
DUAL = ["super-voting", "supervoting", "multiple voting", "10 votes per share",
        "ten votes per share", "high-vote shares"]

# Bump to force a one-time re-parse of every cached proxy (proxies are otherwise parsed once and
# cached by accession, so a detector change wouldn't reach already-parsed names — e.g. Uber's
# false flags would persist). This re-run also backfills compensation's ceo_transition flag.
GOV_PARSER_VERSION = "2"


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


def _latest_proxy(cik10):
    r = _get(SUB_URL.format(cik10=cik10))
    if not r:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    recent = j.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    accs = recent.get("accessionNumber", []) or []
    docs = recent.get("primaryDocument", []) or []
    dates = recent.get("filingDate", []) or []
    for i, f in enumerate(forms):
        if f == "DEF 14A":
            return {"accn": accs[i] if i < len(accs) else "",
                    "doc": docs[i] if i < len(docs) else "",
                    "date": dates[i] if i < len(dates) else ""}
    return None


def _proxy_html(cik_int, accn, doc):
    """Raw proxy HTML (table structure preserved — needed for comp parsing)."""
    if not accn or not doc:
        return ""
    nod = accn.replace("-", "")
    r = _get(f"{ARCHIVE}/{cik_int}/{nod}/{doc}")
    time.sleep(0.1)
    if not r or not r.text:
        return ""
    return r.text[:3000000]


def _detag(html):
    return _TAG.sub(" ", html or "").lower()[:1500000]


# Negations that turn a "match" into a NON-match when they appear just before the phrase.
# Good-governance proxies advertise the ABSENCE of these provisions ("No poison pill", "we do
# not have a stockholder rights plan", "the board was declassified"), which naive substring
# matching flagged as if they were PRESENT (e.g. Uber, CrowdStrike).
_NEG = re.compile(
    r"\b(?:no|not|never|without|none|elimin\w+|declassif\w+|de-classif\w+|redeem\w+|"
    r"terminat\w+|repeal\w+|rescind\w+|do(?:es)?\s+not|ha(?:ve|s)\s+not|is\s+not|"
    r"are\s+not|was\s+not|were\s+not|no\s+longer|absence\s+of)\b")


def _present(text, phrases):
    """True only if a phrase appears (a) as a WHOLE word — so 'classified board' can't match
    inside 'declassified board' — and (b) NOT negated in the ~80 chars before it — so 'No poison
    pill' / 'we do not maintain a rights plan' don't count as the provision being in place."""
    for p in phrases:
        for m in re.finditer(r"\b" + re.escape(p), text):   # leading word boundary
            if not _NEG.search(text[max(0, m.start() - 80):m.start()]):
                return True
    return False


def detect(text):
    t = text or ""
    return {
        "classified_board": _present(t, CLASSIFIED),
        "poison_pill": _present(t, POISON),
        "dual_class": _present(t, DUAL),
    }


def refresh_governance(ciks):
    """For each (padded) CIK, parse the latest DEF 14A unless we've already parsed
    that exact accession. Returns the number of newly-parsed proxies."""
    done = 0
    ncomp = 0
    npeers = 0
    # Universe name index (cik, name) for matching the self-selected comp peer group.
    universe = [(c.get("cik"), c.get("name")) for c in database.get_companies()
                if c.get("name") and c.get("cik")]
    # One-time full re-parse when the detector version changed (see GOV_PARSER_VERSION).
    force = database.get_meta("gov_parser_version") != GOV_PARSER_VERSION
    for cik in ciks:
        cik10 = _pad(cik)
        prx = _latest_proxy(cik10); time.sleep(0.12)
        if not prx or not prx.get("accn"):
            continue
        cached = database.get_governance(cik10)
        # Skip only if we've parsed this exact proxy AND already attempted comp + peers
        # (both stored non-None — a real value or the "attempted, none found" sentinel).
        # Names cached before a parser existed have NULL there, so they get a one-time
        # backfill re-parse. `force` (detector version bump) re-parses everything once.
        if (not force and cached and cached.get("proxy_accn") == prx["accn"]
                and cached.get("comp_json") is not None
                and cached.get("peers_json") is not None):
            continue
        html = _proxy_html(int(cik10), prx["accn"], prx["doc"])
        if not html:
            continue
        detagged = _detag(html)
        flags = detect(detagged)
        try:
            comp = compensation.parse_ceo_comp(html) or {}     # {} = attempted, none found
        except Exception:
            comp = {}
        try:
            raw_peers = compensation.find_peers(detagged, universe, self_cik=cik10)
            peers = [_pad(p) for p in raw_peers if _pad(p) != cik10]
        except Exception:
            peers = []
        nod = prx["accn"].replace("-", "")
        url = f"{ARCHIVE}/{int(cik10)}/{nod}/{prx['doc']}"
        database.upsert_governance(cik10, flags, prx["accn"], prx.get("date"), url,
                                   comp, peers)
        done += 1
        if comp:
            ncomp += 1
        if peers:
            npeers += 1
    if force:
        database.set_meta("gov_parser_version", GOV_PARSER_VERSION)
    print(f"[governance] parsed {done} new proxies · CEO comp {ncomp} · peer groups {npeers}"
          + ("  (full re-parse: detector v" + GOV_PARSER_VERSION + ")" if force else ""))
    return done
