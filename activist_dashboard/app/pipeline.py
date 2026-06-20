"""
Orchestration + SEC XBRL fundamentals + Alpha Vantage enrichment.

Jobs:
  refresh_data()             -- EDGAR filings + news + rescore (fast, every 30m).
  refresh_fundamentals()     -- SEC XBRL fundamentals + sector + shares + equity.
  refresh_enrichment()       -- Alpha Vantage: market cap + P/B + overview (shortlist).
  daily_rescore_and_digest() -- 4pm ET: data + fundamentals + enrich, rescore, email.
  startup_full_refresh()     -- once after boot: data, fundamentals, enrich, score.

Fundamentals now also capture the RAW XBRL line items (operating income, revenue,
net income, assets, equity, cash, debt) plus the source 10-K (fiscal year + period
end + accession), so the detail view can show the exact math + a link to the filing.
"""
import os
import time
import gc
import traceback
from datetime import datetime, timedelta

import requests

from . import (config, database, universe, edgar, news, scoring, emailer,
               governance, insider, activist, earnings, votes)

_UNIVERSE = None

_HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
_SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_AV_URL = "https://www.alphavantage.co/query"
_FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
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


def _usd(facts, tags):
    g = facts.get("facts", {}).get("us-gaap", {})
    for t in tags:
        node = g.get(t)
        if node:
            u = node.get("units", {}).get("USD")
            if u:
                return u
    return []


def _pd(s):
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _ddays(s, e):
    a, b = _pd(s), _pd(e)
    return (b - a).days if (a and b) else None


def _minus_year(e):
    d = _pd(e)
    if not d:
        return None
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return d.replace(year=d.year - 1, day=28).isoformat()


def _flows(facts, tags):
    """Duration (income-statement) entries: dicts of end, val, days, accn."""
    out = []
    for e in _usd(facts, tags):
        s, en, v = e.get("start"), e.get("end"), e.get("val")
        if s and en and v is not None:
            d = _ddays(s, en)
            if d and d > 0:
                out.append({"end": en, "val": v, "days": d, "accn": e.get("accn")})
    return out


def _instant(facts, tags):
    """Latest point-in-time (balance-sheet) value, from 10-K or 10-Q, whichever is newest."""
    rows = [(e["end"], e["val"]) for e in _usd(facts, tags)
            if e.get("val") is not None and e.get("end")
            and (not e.get("start") or e.get("start") == e.get("end"))]
    if not rows:
        return None
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[0][1]


def _latest_period(flows):
    if not flows:
        return None
    return sorted(flows, key=lambda e: (e["end"], e["days"]), reverse=True)[0]


def _annual_period(flows):
    ann = [e for e in flows if 350 <= e["days"] <= 380]
    return _latest_period(ann)


def _at(flows, end, days, tol=25):
    same = [e for e in flows if e["end"] == end]
    for e in same:
        if abs(e["days"] - days) <= tol:
            return e["val"]
    return same[0]["val"] if same else None


def _prior_year(flows, end, days, tol=25):
    tgt = _minus_year(end)
    if not tgt:
        return None
    cand = [e for e in flows if abs(e["days"] - days) <= 20
            and abs((_pd(e["end"]) - _pd(tgt)).days) <= tol]
    if not cand:
        return None
    cand.sort(key=lambda e: abs((_pd(e["end"]) - _pd(tgt)).days))
    return cand[0]["val"]


def _period_label(end, days):
    d = _pd(end)
    if not d:
        return ""
    if days and days >= 350:
        return f"FY{d.year}"
    months = max(1, round((days or 0) / 30.4))
    return f"{months}-mo to {d.strftime('%b %Y')}"


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


def _extract(facts):
    """Return (metrics, raw). Signals are computed from the company's MOST RECENT
    reporting period (latest 10-Q year-to-date), falling back to the latest annual
    10-K when the quarterly data isn't cleanly available -- so scores refresh
    quarterly while never doing worse than annual."""
    rev_f = _flows(facts, _REV); op_f = _flows(facts, _OPINC)
    sga_f = _flows(facts, _SGA); ni_f = _flows(facts, _NI)

    # Use the recent period only when operating income is reported for it; else annual.
    base = _latest_period(rev_f)
    if not base or _at(op_f, base["end"], base["days"]) is None:
        base = _annual_period(rev_f)

    if base:
        p_end, p_days, p_accn = base["end"], base["days"], base.get("accn")
        rev = base["val"]
        opinc = _at(op_f, p_end, p_days)
        sga = _at(sga_f, p_end, p_days)
        ni = _at(ni_f, p_end, p_days)
        rev_prior = _prior_year(rev_f, p_end, p_days)
    else:
        p_end = p_days = p_accn = None
        rev = opinc = sga = ni = rev_prior = None

    assets = _instant(facts, _ASSETS)
    equity = _instant(facts, _EQUITY)
    cash_c = _instant(facts, _CASH[:1]); sti = _instant(facts, _STI)
    cash = (cash_c or 0) + (sti or 0) if (cash_c is not None or sti is not None) else None
    dlt = _instant(facts, _DEBT_LT[:1]); dcur = _instant(facts, _DEBT_CUR[:1])
    debt = (dlt or 0) + (dcur or 0) if (dlt is not None or dcur is not None) else None
    shares = _latest_shares(facts)

    # annualize a partial-year net income so ROA is comparable across fiscal positions
    ni_ann = ni
    if ni is not None and p_days and p_days < 350:
        ni_ann = ni * 365.0 / p_days

    def ratio(n, d, lo=None, hi=None):
        if n is None or not d or d <= 0:
            return None
        r = n / d
        if (lo is not None and r < lo) or (hi is not None and r > hi):
            return None
        return r

    growth = None
    if rev is not None and rev_prior and rev_prior > 0:
        g = (rev - rev_prior) / rev_prior
        growth = g if -10 < g < 10 else None

    metrics = {
        "revenue": rev, "revenue_growth": growth,
        "operating_margin": ratio(opinc, rev, -5, 5), "sga_pct": ratio(sga, rev, 0, 5),
        "roa": ratio(ni_ann, assets, -5, 5),
        "cash_to_assets": ratio(cash, assets, 0, 1),
        "debt_to_assets": ratio(debt, assets, 0, 5),
        "shares": shares, "book_equity": equity,
    }
    raw = {
        "revenue": rev, "revenue_prior": rev_prior, "operating_income": opinc,
        "sga": sga, "net_income": ni, "net_income_ann": ni_ann,
        "total_assets": assets, "book_equity": equity, "cash": cash, "debt": debt,
        "period_end": p_end, "period_days": p_days,
        "period_label": _period_label(p_end, p_days) if p_end else None,
        "source_accn": p_accn,
    }
    return metrics, raw


# A recent Form 25 (delisting notice) or Form 15 (deregistration) means the company
# has stopped (or is stopping) trading; long filing silence implies the same.
_DELIST_RECENT_DAYS = 150
_STALE_DAYS = 300


def _is_delist_form(fm):
    fm = (fm or "").strip().upper()
    return fm.startswith("25") or fm.startswith("15-12") or fm.startswith("15F") or fm == "15-15D"


def _meta_from_submissions(j):
    sic = str(j.get("sic") or "")
    desc = j.get("sicDescription") or None
    recent = j.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    last_filing = max(dates) if dates else None
    inactive, reason = False, None
    recent_cut = (datetime.utcnow() - timedelta(days=_DELIST_RECENT_DAYS)).date().isoformat()
    for fm, dt in zip(forms, dates):
        if _is_delist_form(fm) and dt >= recent_cut:
            inactive, reason = True, f"Filed Form {fm} on {dt} (delisting / deregistration)"
            break
    if not inactive and last_filing:
        stale_cut = (datetime.utcnow() - timedelta(days=_STALE_DAYS)).date().isoformat()
        if last_filing < stale_cut:
            inactive, reason = True, f"no SEC filings since {last_filing}"
    return (sic[:2] if len(sic) >= 2 else None), desc, inactive, reason, last_filing


def _company_meta(cik10):
    """Return (sic2, sic_desc, inactive, reason, last_filing) from the submissions API.
    Flags companies that look delisted/deregistered (recent Form 25/15) or that have
    gone quiet (no SEC filing for many months)."""
    r = _get(_sec, _SUB_URL.format(cik10=cik10))
    if not r:
        return None, None, False, None, None
    try:
        j = r.json()
    except ValueError:
        return None, None, False, None, None
    return _meta_from_submissions(j)


# ---- universe + jobs --------------------------------------------------------
def get_universe():
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = universe.load_universe()
        for c in _UNIVERSE:
            if c["cik"]:
                database.upsert_company(c["cik"], c["ticker"], c["name"])
    return _UNIVERSE


def _refresh_company_news():
    """Per-company news (Finnhub) for the names that matter -- current shortlist,
    active situations, and the shared watchlist. Skipped if no FINNHUB_API_KEY."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return 0
    tickers = set()
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("ticker"):
            tickers.add(s["ticker"])
    for s in database.get_active_situations(limit=40):
        if s.get("ticker"):
            tickers.add(s["ticker"])
    for w in database.get_watchlist():
        if w.get("ticker"):
            tickers.add(w["ticker"])
    return news.refresh_company_news(sorted(tickers), key)


def _finnhub_metric_1y(symbol, key):
    """52-week price return (as a fraction) from Finnhub's free metric endpoint."""
    try:
        r = requests.get(_FINNHUB_METRIC_URL,
                         params={"symbol": symbol, "metric": "all", "token": key}, timeout=20)
        d = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return None
    m = (d or {}).get("metric") or {}
    v = m.get("52WeekPriceReturnDaily")
    try:
        return float(v) / 100.0 if v is not None else None
    except (ValueError, TypeError):
        return None


def refresh_tsr():
    """1-yr relative TSR for shortlist / active situations / watchlist via Finnhub's
    free metric endpoint. Stores each name's 1-yr return + the S&P 500 benchmark."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return 0
    spy = _finnhub_metric_1y("SPY", key); time.sleep(0.25)
    if spy is not None:
        database.set_meta("spy_1y", spy)
    pairs = {}
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("ticker"):
            pairs[s["cik"]] = s["ticker"]
    for s in database.get_active_situations(limit=40):
        if s.get("ticker"):
            pairs[s["cik"]] = s["ticker"]
    for w in database.get_watchlist():
        if w.get("ticker"):
            pairs.setdefault(w["cik"], w["ticker"])
    done = 0
    for cik, tk in pairs.items():
        ret = _finnhub_metric_1y(tk, key); time.sleep(0.25)
        if ret is not None:
            database.set_company_market(_unpad(cik), tsr_1y=ret)
            done += 1
    print(f"[tsr] updated {done}/{len(pairs)} names (S&P 1y={spy})")
    return done


def refresh_data(max_companies=None):
    uni = get_universe()
    try:
        n_news = news.ingest(uni, limit=40)
    except Exception:
        traceback.print_exc(); n_news = 0
    try:
        n_cnews = _refresh_company_news()
    except Exception:
        traceback.print_exc(); n_cnews = 0
    try:
        n_tsr = refresh_tsr()
    except Exception:
        traceback.print_exc(); n_tsr = 0
    try:
        n_filings = edgar.ingest(uni, days=config.SCORE_WINDOW_DAYS, max_companies=max_companies)
    except Exception:
        traceback.print_exc(); n_filings = 0
    try:
        flagged = scoring.recompute_all()
    except Exception:
        traceback.print_exc(); flagged = []
    print(f"[refresh] news={n_news} company_news={n_cnews} tsr={n_tsr} "
          f"filings={n_filings} flagged={len(flagged)}")
    return {"news": n_news, "company_news": n_cnews, "tsr": n_tsr,
            "filings": n_filings, "flagged": len(flagged)}


def refresh_fundamentals(max_companies=None):
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    done = 0
    inactive_n = 0
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
            m, raw = _extract(facts)
        finally:
            del facts
        sic2, sic_desc, inactive, reason, last_filing = _company_meta(cik10); time.sleep(0.12)
        raw["sector_desc"] = sic_desc
        raw["inactive"] = inactive
        raw["inactive_reason"] = reason
        raw["last_filing"] = last_filing
        if inactive:
            inactive_n += 1
        database.upsert_fundamentals(cik10, c.get("ticker"), sic2, m, raw)
        done += 1
        if i % 50 == 0:
            gc.collect()
    print(f"[fundamentals] refreshed {done}/{len(subset)} companies; "
          f"{inactive_n} marked inactive (delisted/stale)")
    try:
        flagged = scoring.recompute_all()
        print(f"[fundamentals] rescore complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()
    return done


def refresh_governance():
    """Parse DEF 14A governance red flags for shortlist / active / watchlist names
    (cached by filing accession), then rescore. Free SEC data."""
    ciks = set()
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("cik"):
            ciks.add(s["cik"])
    for s in database.get_active_situations(limit=40):
        if s.get("cik"):
            ciks.add(s["cik"])
    for w in database.get_watchlist():
        if w.get("cik"):
            ciks.add(w["cik"])
    n = governance.refresh_governance(sorted(ciks))
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return n


def refresh_insider():
    """Parse Form 4 insider buys/sells for shortlist / active / watchlist names
    (cached by accession), then rescore. Free SEC data."""
    ciks = set()
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("cik"):
            ciks.add(s["cik"])
    for s in database.get_active_situations(limit=40):
        if s.get("cik"):
            ciks.add(s["cik"])
    for w in database.get_watchlist():
        if w.get("cik"):
            ciks.add(w["cik"])
    n = insider.refresh_insider(sorted(ciks))
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return n


def _tracked_ciks():
    ciks = set()
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("cik"):
            ciks.add(s["cik"])
    for s in database.get_active_situations(limit=40):
        if s.get("cik"):
            ciks.add(s["cik"])
    for w in database.get_watchlist():
        if w.get("cik"):
            ciks.add(w["cik"])
    return ciks


def _tracked_pairs():
    pairs = {}
    for s in database.get_scores(limit=config.SHORTLIST_SIZE):
        if s.get("ticker"):
            pairs[s["cik"]] = s["ticker"]
    for s in database.get_active_situations(limit=40):
        if s.get("ticker"):
            pairs.setdefault(s["cik"], s["ticker"])
    for w in database.get_watchlist():
        if w.get("ticker"):
            pairs.setdefault(w["cik"], w["ticker"])
    return pairs


def refresh_activist(full=False):
    """Sweep EDGAR full-text search for activist filings (13D + contested proxy),
    routing any hit into Active Situations. `full` sweeps the whole universe (daily);
    otherwise just the names we're already tracking (cheaper, for boot). Free SEC data."""
    if full:
        ciks = [c["cik"] for c in get_universe() if c.get("cik")]
    else:
        ciks = sorted(_tracked_ciks())
    n = activist.refresh_activist(ciks)
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return n


def refresh_earnings():
    """Next/last earnings dates (timing layer) for tracked names via Finnhub."""
    return earnings.refresh_earnings(_tracked_pairs())


def refresh_votes():
    """Say-on-pay support (8-K Item 5.07) for tracked names, then rescore. Free SEC data."""
    n = votes.refresh_votes(sorted(_tracked_ciks()))
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return n


def daily_rescore_and_digest():
    refresh_data()
    refresh_fundamentals()
    refresh_governance()
    refresh_insider()
    refresh_votes()
    refresh_activist(full=True)
    refresh_earnings()
    refresh_enrichment()
    return emailer.send_digest()


def startup_full_refresh():
    print("[boot] VERSION=quarterly-evidence-finnhub-tsr-governance-insider-activist-votes  starting refresh")
    refresh_data()
    refresh_fundamentals()
    refresh_governance()
    refresh_insider()
    refresh_votes()
    refresh_activist(full=False)
    refresh_earnings()
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
    """Fetch market cap + P/B + full company overview for the current shortlist
    via Alpha Vantage, store them, and rescore. Paces calls under the free
    ~5/min limit with one backoff; missing/rate-limited names are skipped."""
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
        d = _av_overview(tk, key)
        if d is None:                      # rate-limited -> wait a minute, retry once
            time.sleep(60)
            d = _av_overview(tk, key)
        if d and "Symbol" in d:
            mcap = _av_float(d.get("MarketCapitalization"))
            pb = _av_float(d.get("PriceToBookRatio"))
            database.set_company_market(_unpad(cik), market_cap=mcap, pb_ratio=pb)
            database.upsert_av_overview(cik, tk, d)
            done += 1
        time.sleep(20)                     # ~3/min, under the free 5/min limit
    print(f"[enrich] Alpha Vantage enriched {done}/{len(shortlist)} shortlisted")
    try:
        flagged = scoring.recompute_all()
        print(f"[enrich] rescore complete; flagged={len(flagged)}")
    except Exception:
        traceback.print_exc()
    return done


def _av_overview(ticker, key):
    """Return the OVERVIEW dict, {} on blank, or None if rate-limited (retry)."""
    try:
        r = _web.get(_AV_URL, params={"function": "OVERVIEW", "symbol": ticker,
                                      "apikey": key}, timeout=25)
        d = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return {}
    if isinstance(d, dict) and ("Note" in d or "Information" in d):
        return None
    return d


if __name__ == "__main__":
    database.init_db()
    refresh_data()
