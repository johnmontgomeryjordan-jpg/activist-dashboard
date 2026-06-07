"""
Orchestration + SEC XBRL fundamentals + Stooq prices for the predictive screen.

Jobs:
  refresh_data()             -- EDGAR filings + news + rescore (fast, every 30m).
  refresh_fundamentals()     -- SEC XBRL fundamentals + sector + shares + equity.
  refresh_prices()           -- Stooq daily prices -> market cap, P/B, 1y/3y TSR.
  daily_rescore_and_digest() -- 4pm ET: data + fundamentals + prices, rescore, email.
  startup_full_refresh()     -- once after boot: data, fundamentals, prices, score.

Fundamentals come from SEC's free APIs; prices from Stooq (free, keyless). Both
degrade gracefully: if a source is unreachable, those signals are simply absent
and scoring continues on whatever data is available.
"""
import time
import gc
import csv
import io
import traceback
from datetime import datetime, timedelta

import requests

from . import config, database, universe, edgar, news, scoring, emailer

_UNIVERSE = None

_HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
_SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
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


def _stooq_history(ticker):
    sym = ticker.lower().replace(".", "-")
    r = _get(_web, _STOOQ_URL.format(sym=sym))
    if not r or not r.text or r.text.strip().lower().startswith("<") \
            or "no data" in r.text[:50].lower():
        return []
    out = []
    try:
        for row in csv.DictReader(io.StringIO(r.text)):
            d = row.get("Date"); c = row.get("Close")
            if d and c and c not in ("", "N/D"):
                out.append((d, float(c)))
    except (ValueError, KeyError):
        return []
    return out


def _close_near(hist, days_ago):
    if not hist:
        return None
    target = (datetime.utcnow() - timedelta(days=days_ago)).date().isoformat()
    prior = None
    for d, c in hist:
        if d <= target:
            prior = c
        else:
            break
    return prior


def refresh_prices(max_companies=None):
    funds = {f["cik"]: f for f in database.get_all_fundamentals()}
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    done = 0
    for i, c in enumerate(subset):
        tk = c.get("ticker"); cik10 = _pad(c.get("cik")) if c.get("cik") else None
        if not tk or not cik10:
            continue
        hist = _stooq_history(tk)
        time.sleep(0.15)
        if not hist:
            continue
        last = hist[-1][1]
        c1 = _close_near(hist, 365); c3 = _close_near(hist, 365 * 3)
        tsr_1y = (last - c1) / c1 if (c1 and c1 > 0) else None
        tsr_3y = (last - c3) / c3 if (c3 and c3 > 0) else None
        f = funds.get(cik10, {})
        shares = f.get("shares"); equity = f.get("book_equity")
        mcap = last * shares if (shares and shares > 0) else None
        pb = (mcap / equity) if (mcap and equity and equity > 0) else None
        database.set_company_market(cik10, market_cap=mcap, pb_ratio=pb,
                                    tsr_1y=tsr_1y, tsr_3y=tsr_3y)
        done += 1
        if i % 100 == 0:
            gc.collect()
    print(f"[prices] fetched {done}/{len(subset)} from Stooq")
    try:
        flagged = scoring.recompute_all()
        print(f"[prices] rescore complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()
    return done


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
    refresh_prices()
    return emailer.send_digest()


def startup_full_refresh():
    print("[boot] VERSION=xbrl-stooq-predictive  starting refresh")
    refresh_data()
    refresh_fundamentals()
    refresh_prices()


if __name__ == "__main__":
    database.init_db()
    refresh_data()
