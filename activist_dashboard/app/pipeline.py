"""
Orchestration + SEC XBRL fundamentals + Alpha Vantage enrichment.

Jobs:
  refresh_data()             -- EDGAR filings + news + rescore (fast, every 30m).
  refresh_fundamentals()     -- SEC XBRL fundamentals + sector + shares + equity.
  refresh_enrichment()       -- Alpha Vantage: market cap + P/B for the shortlist.
  daily_rescore_and_digest() -- 4pm ET: data + fundamentals + enrich, rescore, email.
  startup_full_refresh()     -- once after boot: data, fundamentals, enrich, score.

Fundamentals come from SEC's free APIs. Market cap + P/B for the shortlist come
from Alpha Vantage's free tier (keyed, works from cloud hosts). Both degrade
gracefully: missing data is skipped and scoring continues on what's available.
"""
import os
import time
import gc
import traceback
from datetime import datetime, timedelta

import requests

from . import config, database, universe, edgar, news, scoring, emailer

_UNIVERSE = None

_HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
_SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_AV_URL = "https://www.alphavantage.co/query"
_sec = requests.Session(); _sec.headers.update(_HEADERS)
_web = requests.Session(); _web.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ActivistDashboard/1.0)"})

_REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
_OPINC = ["OperatingIncomeLoss"]
_SGA = ["SellingGeneralAndAdministrativeExpense", "SellingGeneralAndAdministrativeExpenses"]
_NI = ["NetIncomeLoss"]
_ASSETS = ["Assets"]
_EQUITY = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
_CASH = ["CashAndCashEquivalentsAtCarryingValue"]
_STI = ["ShortTermInvestments"]
_DEBT_LT = ["LongTermDebtNoncurrent", "LongTermDebt"]
_DEBT_CUR = ["LongTermDebtCurrent", "DebtCurrent"]
_SHARES = ["EntityCommonStockSharesOutstanding"]


def _pad(cik):
    return str(cik).lstrip("0").zfill(10)


def _get(sess, url):
    for i in range(3):
        try:
            r = sess.get(url, timeout=25)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(1.0 * (i + 1))
    return None


def _annual_rows(facts, tags):
    g = facts.get("facts", {}).get("us-gaap", {})
    for t in tags:
        node = g.get(t)
        if not node:
            continue
        usd = node.get("units", {}).get("USD")
        if not usd:
            continue
        rows = [(e["end"], e["val"]) for e in usd
                if str(e.get("form", "")).startswith("10-K") and e.get("fp") == "FY"
                and e.get("val") is not None and e.get("end")]
        if rows:
            rows.sort(key=lambda x: x[0], reverse=True)
            return rows
    return []


def _latest(facts, tags):
    rows = _annual_rows(facts, tags)
    return rows[0][1] if rows else None


def _latest_shares(facts):
    dei = facts.get("facts", {}).get("dei", {})
    for t in _SHARES:
        node = dei.get(t)
        if not node:
            continue
        units = node.get("units", {}).get("shares")
        if not units:
            continue
        rows = [(e["end"], e["val"]) for e in units
                if e.get("val") is not None and e.get("end")]
        if rows:
            rows.sort(key=lambda x: x[0], reverse=True)
            return rows[0][1]
    return None


def _rev_latest_prior(facts):
    rows = _annual_rows(facts, _REV)
    seen = {}
    for end, val in rows:
        seen.setdefault(end[:4], val)
    yrs = sorted(seen, reverse=True)
    return (seen[yrs[0]] if yrs else None), (seen[yrs[1]] if len(yrs) > 1 else None)


def _sum_latest(facts, tags):
    total = None
    for t in tags:
        v = _latest(facts, [t])
        if v is not None:
            total = (total or 0) + v
    return total


def _extract(facts):
    rev, rev_prior = _rev_latest_prior(facts)
    opinc = _latest(facts, _OPINC); sga = _latest(facts, _SGA)
    ni = _latest(facts, _NI); assets = _latest(facts, _ASSETS)
    equity = _latest(facts, _EQUITY); shares = _latest_shares(facts)
    cash = _sum_latest(facts, _CASH[:1] + _STI)
    debt = _sum_latest(facts, _DEBT_LT[:1] + _DEBT_CUR[:1])

    def r(n, d):
        return (n / d) if (n is not None and d and d > 0) else None

    return {
        "revenue": rev,
        "revenue_growth": ((rev - rev_prior) / rev_prior)
                          if (rev is not None and rev_prior and rev_prior > 0) else None,
        "operating_margin": r(opinc, rev), "sga_pct": r(sga, rev),
        "roa": r(ni, assets), "cash_to_assets": r(cash, assets),
        "debt_to_assets": r(debt, assets),
        "shares": shares, "book_equity": equity,
    }


def _sic2(cik10):
    r = _get(_sec, _SUB_URL.format(cik10=cik10))
    if not r:
        return None
    try:
        sic = str(r.json().get("sic") or "")
    except ValueError:
        return None
    return sic[:2] if len(sic) >= 2 else None


# ---- universe + jobs --------------------------------------------------------
def get_universe():
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = universe.load_universe()
        for c in _UNIVERSE:
            if c["cik"]:
                database.upsert_company(c["cik"], c["ticker"], c["name"])
    return _UNIVERSE


def refresh_data(max_companies=None):
    uni = get_universe()
    try:
        n_news = news.ingest(uni, limit=40)
    except Exception:
        traceback.print_exc(); n_news = 0
    try:
        n_filings = edgar.ingest(uni, days=config.SCORE_WINDOW_DAYS, max_companies=max_companies)
    except Exception:
        traceback.print_exc(); n_filings = 0
    try:
        flagged = scoring.recompute_all()
    except Exception:
        traceback.print_exc(); flagged = []
    print(f"[refresh] news={n_news} filings={n_filings} flagged={len(flagged)}")
    return {"news": n_news, "filings": n_filings, "flagged": len(flagged)}


def refresh_fundamentals(max_companies=None):
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    done = 0
    for i, c in enumerate(subset):
        cik = c.get("cik")
        if not cik:
            continue
        cik10 = _pad(cik)
        r = _get(_sec, _FACTS_URL.format(cik10=cik10)); time.sleep(0.12)
        if not r:
            continue
        try:
            facts = r.json()
        except ValueError:
            continue
        try:
            m = _extract(facts)
        finally:
            del facts
        sic2 = _sic2(cik10); time.sleep(0.12)
        database.upsert_fundamentals(cik10, c.get("ticker"), sic2, m)
        done += 1
        if i % 50 == 0:
            gc.collect()
    print(f"[fundamentals] refreshed {done}/{len(subset)} companies")
    try:
        flagged = scoring.recompute_all()
        print(f"[fundamentals] rescore complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()
    return done


def daily_rescore_and_digest():
    refresh_data()
    refresh_fundamentals()
    refresh_enrichment()
    return emailer.send_digest()


def startup_full_refresh():
    print("[boot] VERSION=xbrl-av-predictive  starting refresh")
    refresh_data()
    refresh_fundamentals()
    refresh_enrichment()


# ---- Alpha Vantage enrichment (shortlist only; free tier ~25 calls/day) ------
def _av_key():
    return os.getenv("ALPHAVANTAGE_API_KEY", "")


def _av_float(v):
    if v in (None, "", "None", "-", "NaN"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _unpad(cik):
    try:
        return str(int(cik))
    except (ValueError, TypeError):
        return cik


def refresh_enrichment():
    """Fetch market cap + P/B for the current shortlist via Alpha Vantage and
    rescore. Best-effort: missing/rate-limited names are simply skipped."""
    key = _av_key()
    if not key:
        print("[enrich] no ALPHAVANTAGE_API_KEY set; skipping")
        return 0
    shortlist = database.get_scores(limit=config.SHORTLIST_SIZE)
    done = 0
    for s in shortlist:
        tk = s.get("ticker"); cik = s.get("cik")
        if not tk or not cik:
            continue
        try:
            r = _web.get(_AV_URL, params={"function": "OVERVIEW", "symbol": tk,
                                          "apikey": key}, timeout=25)
            d = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            d = {}
        if d and "Symbol" in d:
            mcap = _av_float(d.get("MarketCapitalization"))
            pb = _av_float(d.get("PriceToBookRatio"))
            database.set_company_market(_unpad(cik), market_cap=mcap, pb_ratio=pb)
            done += 1
        time.sleep(13)  # respect the ~5 requests/minute free limit
    print(f"[enrich] Alpha Vantage enriched {done}/{len(shortlist)} shortlisted")
    try:
        flagged = scoring.recompute_all()
        print(f"[enrich] rescore complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()
    return done


if __name__ == "__main__":
    database.init_db()
    refresh_enrichment.__doc__  # no-op
    refresh_data()
