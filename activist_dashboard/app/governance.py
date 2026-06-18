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

from . import config, database

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


def _proxy_text(cik_int, accn, doc):
    if not accn or not doc:
        return ""
    nod = accn.replace("-", "")
    r = _get(f"{ARCHIVE}/{cik_int}/{nod}/{doc}")
    time.sleep(0.1)
    if not r or not r.text:
        return ""
    return _TAG.sub(" ", r.text).lower()[:1500000]


def detect(text):
    t = text or ""
    return {
        "classified_board": any(p in t for p in CLASSIFIED),
        "poison_pill": any(p in t for p in POISON),
        "dual_class": any(p in t for p in DUAL),
    }


def refresh_governance(ciks):
    """For each (padded) CIK, parse the latest DEF 14A unless we've already parsed
    that exact accession. Returns the number of newly-parsed proxies."""
    done = 0
    for cik in ciks:
        cik10 = _pad(cik)
        prx = _latest_proxy(cik10); time.sleep(0.12)
        if not prx or not prx.get("accn"):
            continue
        cached = database.get_governance(cik10)
        if cached and cached.get("proxy_accn") == prx["accn"]:
            continue  # already parsed this proxy
        text = _proxy_text(int(cik10), prx["accn"], prx["doc"])
        if not text:
            continue
        flags = detect(text)
        nod = prx["accn"].replace("-", "")
        url = f"{ARCHIVE}/{int(cik10)}/{nod}/{prx['doc']}"
        database.upsert_governance(cik10, flags, prx["accn"], prx.get("date"), url)
        done += 1
    print(f"[governance] parsed {done} new proxies")
    return done
