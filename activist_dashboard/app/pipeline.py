"""
Orchestration. Market valuation now comes from Finnhub (free, 60/min), so it refreshes
EVERY cycle rather than once a day -- Alpha Vantage is kept only for the (static, cached)
company description.

Fast jobs -- every 30 minutes (refresh_data):
  news + per-company news + 1-yr TSR (Finnhub) + EDGAR filings + rescore, then
  refresh_enrichment(fetch_desc=False): Finnhub market cap + P/B + P/E + 52-wk for the
  shortlist/watchlist/active names. So valuation is continuously updated, not daily.

Heavier jobs -- on boot and in the 6 AM ET daily run (new build with no-repeat lead):
  refresh_fundamentals()  -- SEC XBRL fundamentals + sector + shares + equity (universe).
  refresh_governance()    -- DEF 14A entrenchment flags (tracked names).
  refresh_insider()       -- Form 4 open-market buys/sells (tracked names).
  refresh_votes()         -- say-on-pay support from 8-K Item 5.07 (tracked names).
  refresh_activist()      -- 13D / contested-proxy sweep -> Active Situations
                             (full universe daily; tracked-only on boot).
  refresh_earnings()      -- next/last earnings dates (Finnhub).
  refresh_enrichment()    -- also fills any missing descriptions from Alpha Vantage (cached).
  daily_rescore_and_digest() -- runs all of the above, then emails the 4pm ET digest.
  startup_full_refresh()  -- runs all of the above once after boot.

P/B is computed as market cap / SEC book equity (most reliable), falling back to
Finnhub's reported P/B. Fundamentals also capture the RAW XBRL line items (operating
income, revenue, net income, assets, equity, cash, debt) plus the source 10-K (fiscal
year + period end + accession), so the detail view can show the exact math + a filing link.
"""
import os
import time
import gc
import io
import json
import zipfile
import traceback
from datetime import datetime, timedelta

import requests

from datetime import datetime

from . import (config, database, universe, edgar, news, scoring, emailer,
               governance, insider, activist, earnings, votes, fmp, twelvedata,
               contacts, advisors, reaction, longtsr, aithesis, spotlight)

_UNIVERSE = None

# How many of the top-ranked leads to enrich (contacts, prices, sentiment, news, etc.).
# Covers the visible shortlist + the spotlight pool so their profiles are complete, while
# staying within the free API tiers. Active situations + the watchlist are always added on
# top of this.
ENRICH_TOP = 60

# Twelve Data is the one hard daily cap (800 credits/day free; 1 credit per symbol per
# pull). So the Twelve-Data-backed passes (price charts, multi-year TSR, exec reactions)
# cover only the TOP-RANKED leads (plus active situations + watchlist, always included) —
# a much smaller set than the full ENRICH_TOP enrichment, which uses uncapped/free sources.
TD_TOP = 30

_HEADERS = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
_SUB_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_AV_URL = "https://www.alphavantage.co/query"
_FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
_FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
_FINNHUB_INSIDER_SENT_URL = "https://finnhub.io/api/v1/stock/insider-sentiment"
_FINNHUB_RECO_URL = "https://finnhub.io/api/v1/stock/recommendation"
_sec = requests.Session(); _sec.headers.update(_HEADERS)
_web = requests.Session(); _web.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ActivistDashboard/1.0)"})

_REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
_OPINC = ["OperatingIncomeLoss"]
_SGA = ["SellingGeneralAndAdministrativeExpense", "SellingGeneralAndAdministrativeExpenses"]
_NI = ["NetIncomeLoss"]
_ASSETS = ["Assets"]
_EQUITY = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
# Cash. Was a SINGLE tag with no fallback and no staleness guard -- the one gap in this file's
# otherwise-universal "companies abandon XBRL tags over time" defense (revenue/debt/goodwill all
# have multi-tag fallbacks; debt additionally has _DEBT_STALE_DAYS). Root-caused auditing AHCO:
# its CashAndCashEquivalentsAtCarryingValue series stopped at 2019-03-31 ($744,766) while its
# current balance sheet (2026-03-31, $47.964M) reports cash under
# CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents instead -- a 7-year-stale value
# was surfacing as "current" because nothing else was ever checked and nothing flagged the age.
# _usd()/_instant() already pick the MOST RECENT tag among candidates (see _usd's docstring), so
# listing every common current-cash tag here — plus the _CASH_STALE_DAYS guard below — closes
# both halves of the gap: the missing fallback AND the missing staleness check.
_CASH = ["CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
        "Cash"]
# If even the freshest available cash tag trails the company's current balance-sheet date (the
# Assets tag) by more than this, every candidate has been abandoned -> treat cash as unknown
# rather than show a frozen figure as current. Tighter than _DEBT_STALE_DAYS (550): cash is a
# required, universally-reported line every single quarter, unlike debt structure.
_CASH_STALE_DAYS = 400
# Short-term investments added to cash for the liquidity ("cash-rich") read. Precision-first so it
# can NEVER inflate liquidity into a false cash-rich flag:
#   _STI_CURRENT  — tags that are UNAMBIGUOUSLY current. Safe to count as-is.
#   _STI_AMBIG    — un-suffixed base tags some issuers use for CURRENT securities (Vital Farms files
#                   its $64.5M current AFS securities as us-gaap:AvailableForSaleSecuritiesDebtSecurities,
#                   which the old single-tag list missed -> VITL read 7% cash vs a true ~22%). Counted
#                   ONLY under the guards in _short_term_investments().
#   _LT_INVEST    — any long-term/noncurrent investment tag. Its presence means the base tag can't be
#                   assumed current, so we don't count it (blocks miscounting long-term holdings as cash).
_STI = ["ShortTermInvestments"]   # kept for reference; _STI_CURRENT supersedes it in _extract
# HELD-TO-MATURITY note (#33): some cash-rich names park liquidity in current HTM Treasuries, which
# the old list missed entirely — e.g. Copart holds ~$2.0B of HTM securities in current assets, so its
# liquidity read was understated (~$2.1B vs a true ~$4.8B). We add the STANDARD current-HTM tags below.
# CAVEAT: Copart itself files HTM under a *custom* (non-us-gaap) element, so this general fix helps
# other issuers but does NOT recover Copart's specific figure — a known limitation for custom taggers.
_STI_CURRENT = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                "AvailableForSaleSecuritiesCurrent", "OtherShortTermInvestments",
                "HeldToMaturitySecuritiesCurrent", "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent"]
_STI_AMBIG = ["AvailableForSaleSecuritiesDebtSecurities", "MarketableSecurities",
              "AvailableForSaleSecurities", "HeldToMaturitySecurities"]
_LT_INVEST = ["AvailableForSaleSecuritiesDebtSecuritiesNoncurrent", "MarketableSecuritiesNoncurrent",
              "AvailableForSaleSecuritiesNoncurrent", "LongTermInvestments",
              "OtherLongTermInvestments", "HeldToMaturitySecuritiesNoncurrent"]
_ASSETS_CURRENT = ["AssetsCurrent"]
# Funded debt. Prefer a single TOTAL tag — per US-GAAP, "LongTermDebt" already INCLUDES the
# current maturities — otherwise sum the noncurrent + current components (first tag available in
# each list). The old code read only _DEBT_LT[:1] ("LongTermDebtNoncurrent") + "LongTermDebtCurrent",
# so a company filing its debt under "LongTermDebt" (BLDR — the $408.9M was just the current
# portion) or a capital-lease tag was badly under-captured → a false "under-levered" signal AND an
# understated EV/EBITDA. See _total_debt().
# Funded-debt tags, grouped so _total_debt can prefer a single TOTAL, then a noncurrent TOTAL,
# and finally SUM the instrument-level component tags (senior notes / convertible notes / notes
# payable) — which is how issuers like ServiceNow report their debt ("Long-term debt, net" tagged
# ConvertibleDebtNoncurrent / SeniorNotesNoncurrent) rather than under LongTermDebt(Noncurrent).
# Missing those component tags left ServiceNow reading ~$0 funded debt -> a FALSE "under-levered
# opportunity" (#3). Parts are summed only when no total/noncurrent-total tag reports at the same
# date, so a company that files a proper total is never double-counted.
# NotesAndLoansPayable is an all-in (current + noncurrent) funded-debt total — "carrying value of
# all notes and loans payable." Homebuilders in particular file their debt ONLY under this tag and
# none of the LongTermDebt/SeniorNotes family: KB Home tags its entire $1.69B senior-note balance
# as us-gaap:NotesAndLoansPayable, so the app read $0 funded debt -> a FALSE "under-levered / Lever"
# signal and a badly understated EV/EBITDA (verified against KBH's 10-K + FactSet: 25% debt/assets,
# not 0%). Listed AFTER the more specific funded-debt totals so a company filing both is unaffected.
_DEBT_TOTAL = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
               "DebtInstrumentCarryingAmount", "NotesAndLoansPayable"]
_DEBT_NC_TOTAL = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]
# COMBINED instrument tags: each already includes BOTH current and noncurrent portions, so they
# are NOT part of the noncurrent/current split above. Many issuers — especially REITs (Digital
# Realty tags its ~$16B under plain "SeniorNotes", $432M under "UnsecuredDebt") — file only these
# and none of the split tags, which left them reading $0 funded debt -> a FALSE "under-levered"
# signal + understated EV. Summed ONLY as a fallback when the split tags yield nothing at the
# latest balance-sheet date, so a proper total is never double-counted with its own components.
_DEBT_COMBINED_PARTS = ["SeniorNotes", "UnsecuredDebt", "SecuredDebt", "NotesPayable",
                        "LineOfCreditFacilityAmountOutstanding", "ConvertibleDebt",
                        "SubordinatedDebt", "MediumTermNotes", "LongTermLineOfCredit",
                        "OtherLongTermDebt", "MortgageLoansOnRealEstate"]
_DEBT_NC_PARTS = ["SeniorNotesNoncurrent", "ConvertibleDebtNoncurrent",
                  "ConvertibleNotesPayableNoncurrent", "ConvertibleLongTermNotesPayable",
                  "UnsecuredDebtNoncurrent", "SecuredDebtNoncurrent", "SecuredLongTermDebt",
                  "NotesPayableNoncurrent", "LongTermNotesPayable", "LongTermLoansPayable",
                  "OtherLongTermDebtNoncurrent", "MediumTermNotesNoncurrent",
                  "NotesAndLoansPayableNoncurrent"]
_DEBT_CUR_TOTAL = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                   "DebtCurrent"]
_DEBT_CUR_PARTS = ["SeniorNotesCurrent", "ConvertibleDebtCurrent",
                   "ConvertibleNotesPayableCurrent", "NotesPayableCurrent",
                   "SecuredDebtCurrent", "ShortTermBorrowings", "NotesAndLoansPayableCurrent"]
# Back-compat aliases (some code/tests reference the old flat names).
# If the newest funded-debt tag a company still files predates its current balance sheet by more
# than this, the issuer STOPPED reporting funded debt -> it has been repaid or converted, and the
# frozen figure must NOT be shown as current (ServiceNow abandoned LongTermDebtNoncurrent after its
# 2018 converts settled in 2022; the stale 2021 $1.48B was surfacing mislabeled as Mar-2026 debt).
_DEBT_STALE_DAYS = 550
_DEBT_NONCUR = _DEBT_NC_TOTAL + _DEBT_NC_PARTS
_DEBT_CUR = _DEBT_CUR_TOTAL + _DEBT_CUR_PARTS
_SHARES = ["EntityCommonStockSharesOutstanding"]
_DEP = ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization"]                       # cash-flow D&A -> EBITDA
# Interest expense (income statement). Used only as a credibility discriminator: a genuinely
# debt-free company reports ~no interest expense, whereas a MISSED/stale debt tag still shows a
# real borrowing cost on the P&L — so we can tell "correctly $0 debt" from "we missed the debt."
_INT_EXP = ["InterestExpense", "InterestExpenseDebt", "InterestAndDebtExpense",
            "InterestExpenseNonoperating", "InterestExpenseBorrowings"]
# Dividends paid (cash-flow statement) -> a LOCALLY-computed yield cross-check against Finnhub's
# dividendYieldIndicatedAnnual. Motivated auditing MNRO: FactSet showed a 9.36% yield, comfortably
# under the existing 15% sanity cap, so nothing there would flag it as unreliable -- the real gap
# is having no independent source to catch Finnhub's figure being wrong or stale in the first
# place (the same abandoned-tag/stale-source risk XBRL fields already get multi-tag + staleness
# defenses for). This doesn't change any signal or score; it only stores a second, independently
# sourced yield alongside Finnhub's so a material divergence between the two is visible.
_DIV_PAID = ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]
# Declared dividend PER SHARE, quarter by quarter. Needed because a trailing yield cannot tell a
# healthy payer from one that just suspended: WHR paid $5.30/sh through Feb 2026 and then went to
# zero, so a trailing-12mo yield still reads ~13% while the true forward yield is 0%. Comparing the
# most recent declared quarter against the prior ones is what distinguishes the two.
_DIV_PER_SHARE = ["CommonStockDividendsPerShareDeclared",
                 "CommonStockDividendsPerShareCashPaid"]
# Share repurchases (cash-flow statement). Cumulative treasury stock is the wrong measure -- it is
# decades of history and would light up any long-lived company -- so we sum the trailing few YEARS
# of actual buyback spend and compare it to what the company is worth today. PZZA: ~$1.1B of
# treasury against a $788M market cap, funded with debt, equity at -$445M, stock -81% over 5 years.
_BUYBACK = ["PaymentsForRepurchaseOfCommonStock",
           "PaymentsForRepurchaseOfEquity",
           "TreasuryStockValueAcquiredCostMethod"]
_GOODWILL = ["Goodwill"]                                     # balance-sheet goodwill -> M&A
_OP_LEASE_NC = ["OperatingLeaseLiabilityNoncurrent"]        # ASC 842 operating-lease liability:
_OP_LEASE_CUR = ["OperatingLeaseLiabilityCurrent"]          # a mall retailer's real leverage
# Finance (capital) leases — interest-bearing lease debt, reported separately from operating leases.
# Surfaced so the scoring lease-heavy guard can judge TOTAL lease load (op + finance): Vital Farms
# carries $42.7M operating + $10.8M finance leases = 10.3% of assets, but operating-only was 8.2% —
# just under the 10% threshold — so it slipped the guard and drew a questionable "under-levered" read.
_FIN_LEASE_NC = ["FinanceLeaseLiabilityNoncurrent", "CapitalLeaseObligationsNoncurrent"]
_FIN_LEASE_CUR = ["FinanceLeaseLiabilityCurrent", "CapitalLeaseObligationsCurrent"]
_FIN_LEASE_TOTAL = ["FinanceLeaseLiability", "CapitalLeaseObligations"]  # combined fallback


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
    """USD unit series for the first of `tags` that has data — but, when several tags carry
    data, prefer the one whose data is MOST RECENT. Companies switch XBRL concepts over time
    (e.g. 'Revenues' -> 'RevenueFromContractWithCustomerExcludingAssessedTax'); returning the
    first tag with *any* data froze names like TMDX on a stale 2022 series under an abandoned
    tag. Ties (same latest end-date) fall back to the given preference order."""
    g = facts.get("facts", {}).get("us-gaap", {})
    best, best_end = [], ""
    for t in tags:
        node = g.get(t)
        if not node:
            continue
        u = node.get("units", {}).get("USD")
        if not u:
            continue
        mx = max((e.get("end") or "") for e in u)
        if mx > best_end:                  # strictly newer wins; equal -> keep earlier (preferred) tag
            best, best_end = u, mx
    return best


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


def _instant_dated(facts, tag):
    """(latest_end, val) for a single balance-sheet tag, or (None, None)."""
    rows = [(e["end"], e["val"]) for e in _usd(facts, [tag])
            if e.get("val") is not None and e.get("end")
            and (not e.get("start") or e.get("start") == e.get("end"))]
    if not rows:
        return None, None
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[0]


def _latest_instant_end(facts, tags):
    """Freshest 'end' date across a candidate tag LIST, using the same tag-recency selection
    _usd()/_instant() already apply (whichever candidate tag's data is most recent wins) --
    lets a caller staleness-check the value _instant(facts, tags) returned without re-deriving
    which tag it came from."""
    rows = [e.get("end") for e in _usd(facts, tags) if e.get("end")
            and (not e.get("start") or e.get("start") == e.get("end"))]
    return max(rows) if rows else None


# *AndCapitalLeaseObligations tags already bundle finance (capital) leases into the funded-debt
# figure, so when the funded total comes from one of these we must NOT add finance leases again.
_CAPLEASE_INCLUSIVE = ("LongTermDebtAndCapitalLeaseObligations",
                       "LongTermDebtAndCapitalLeaseObligationsCurrent")


def _funded_debt(facts):
    """Funded debt, RECENCY-AWARE and robust to tag switches. Companies abandon XBRL tags over time:
    BLDR's "LongTermDebt" froze at 2015 ($408.9M) while it now reports debt under
    "LongTermDebtAndCapitalLeaseObligations" (2026 = $4.6B). So: read every candidate debt tag's
    latest (date, value), find the company's most recent balance-sheet date, and build debt ONLY from
    tags reporting at that date. Returns (funded_debt, via_caplease) — via_caplease True when the value
    came from a *AndCapitalLeaseObligations tag (which already bundles finance leases)."""
    dated = {}                                   # tag -> (end_date, value)
    for t in (_DEBT_TOTAL + _DEBT_NC_TOTAL + _DEBT_NC_PARTS
              + _DEBT_CUR_TOTAL + _DEBT_CUR_PARTS + _DEBT_COMBINED_PARTS):
        ed, v = _instant_dated(facts, t)
        if ed is not None and v is not None:
            dated[t] = (ed, v)
    if not dated:
        return None, False
    latest = max(ed for ed, _v in dated.values())
    # Staleness guard: anchor to the company's CURRENT balance sheet (the Assets date). If the
    # newest debt tag is far older than that, the debt is gone (tag abandoned) -> report none.
    _bs_ed, _bs_v = _instant_dated(facts, _ASSETS[0])
    if _bs_ed:
        try:
            _gap = (datetime.strptime(_bs_ed[:10], "%Y-%m-%d")
                    - datetime.strptime(str(latest)[:10], "%Y-%m-%d")).days
        except (ValueError, TypeError):
            _gap = 0
        if _gap > _DEBT_STALE_DAYS:
            return 0.0, False

    def src_at_latest(tags):                     # (value, tag) for first tag (pref order) at `latest`
        for t in tags:
            if t in dated and dated[t][0] == latest:
                return dated[t][1], t
        return None, None

    def sum_at_latest(tags):                     # sum of ALL component tags reporting at `latest`
        vals = [dated[t][1] for t in tags if t in dated and dated[t][0] == latest]
        return sum(vals) if vals else None

    # A single all-in total tag (per US-GAAP LongTermDebt already includes current maturities).
    total, _tag = src_at_latest(_DEBT_TOTAL)
    if total is not None:
        return total, False                      # _DEBT_TOTAL tags are debt-only (no leases)
    # Else build from noncurrent + current. Prefer a TOTAL tag on each side; only if none reports
    # at the latest date do we SUM the instrument-level parts (so a proper total is never
    # double-counted with its own components).
    nc, nctag = src_at_latest(_DEBT_NC_TOTAL)
    if nc is None:
        nc = sum_at_latest(_DEBT_NC_PARTS)
    cur, curtag = src_at_latest(_DEBT_CUR_TOTAL)
    if cur is None:
        cur = sum_at_latest(_DEBT_CUR_PARTS)
    via_caplease = (nctag in _CAPLEASE_INCLUSIVE) or (curtag in _CAPLEASE_INCLUSIVE)
    if nc is None and cur is None:
        # No current/noncurrent-split tags reported at the latest date. Many issuers (esp. REITs)
        # instead file only plain COMBINED instrument tags (SeniorNotes / UnsecuredDebt /
        # SecuredDebt — each already current+noncurrent). Sum those at the latest date BEFORE the
        # last-resort stale fallback so DLR/FR/JBGS et al. get real debt instead of a false $0.
        combined = sum_at_latest(_DEBT_COMBINED_PARTS)
        if combined is not None:
            return combined, False
        # nothing from our known tags at the latest date — use the most recent value we do have
        return max(dated.values(), key=lambda ev: ev[0])[1], False
    return (nc or 0) + (cur or 0), via_caplease


def _total_debt(facts):
    """Total debt as FactSet / BoardroomAlpha report it: funded debt PLUS lease liabilities (finance +
    operating). Funded-debt tags alone read a false ~$0 for lease-heavy names (grocers, asset-light
    services), badly understating Debt/Assets and EV — INSP/VITL showed $0 total debt vs ~$30M / ~$53M
    at FactSet/BoardroomAlpha, and ACI read 34% vs a real ~59%. Operating leases are never in a
    funded-debt tag (no double-count); finance leases are added only when funded didn't already bundle
    them (a *AndCapitalLeaseObligations tag)."""
    funded, via_caplease = _funded_debt(facts)
    op = (_instant(facts, _OP_LEASE_NC) or 0) + (_instant(facts, _OP_LEASE_CUR) or 0)
    fin = _instant(facts, _FIN_LEASE_TOTAL)
    if fin is None:
        fin = (_instant(facts, _FIN_LEASE_NC) or 0) + (_instant(facts, _FIN_LEASE_CUR) or 0)
    fin = 0 if via_caplease else (fin or 0)      # don't double-count finance leases already in funded
    leases = (op or 0) + fin
    if funded is None:
        return leases if leases else None        # a lease-only balance sheet (no funded debt)
    return funded + leases


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


def _annual_growth(rev_f):
    """Year-over-year revenue growth from FULL-YEAR (10-K) periods (~365 days), keyed by
    fiscal-year-end. Stable for lumpy-revenue companies where a single quarter's YoY
    misleads (milestone-based biotech, licensing transitions) -- that quarterly comparison
    made real double-digit growers look flat or negative.

    Returns (growth, cur_val, prior_val, cur_end, prior_end) so the evidence card can show
    the SAME two figures the percentage was computed from (they were previously drawn from
    the latest QUARTERLY period, which contradicted this annual %). All-None if <2 annual
    periods."""
    by_year = {}
    for e in rev_f:
        if 350 <= e["days"] <= 380 and e.get("end"):
            yr = e["end"][:4]
            if yr not in by_year or e["end"] > by_year[yr]["end"]:
                by_year[yr] = e
    yrs = sorted(by_year, reverse=True)
    if len(yrs) >= 2:
        ce, pe = by_year[yrs[0]], by_year[yrs[1]]
        cur, prior = ce["val"], pe["val"]
        if prior and prior > 0:
            g = (cur - prior) / prior
            if -10 < g < 10:
                return g, cur, prior, ce.get("end"), pe.get("end")
    return None, None, None, None, None


def _annual_latest(flows):
    """Most recent FULL-YEAR (~365-day) value from a flow series, by fiscal-year-end.
    Used for a profitability sanity check — a single quarter can be distorted by a one-time
    charge or a discontinued-operations divestiture, but the full year tells the real story."""
    ann = [e for e in flows if 350 <= e["days"] <= 380 and e.get("end")]
    if not ann:
        return None
    ann.sort(key=lambda e: e["end"], reverse=True)
    return ann[0]["val"]


def _ttm_from(annual_v, interim_v, prior_v):
    """Trailing-twelve-month value = latest full year + this fiscal year's interim (YTD) − the
    prior-year same interim. None unless all three parts are present (caller then falls back to
    the reporting-period figure). Rolls a seasonal quarter into a full, comparable year."""
    if annual_v is None or interim_v is None or prior_v is None:
        return None
    return annual_v + interim_v - prior_v


def _dividend_state(facts):
    """(latest_dps, prior_dps_run_rate, status) from the per-share declared-dividend series.

    A trailing yield is blind to a board that has just stopped paying, which is exactly the case
    that matters most: a suspension is one of the strongest distress/capital-allocation catalysts
    there is, and it is also when a stale third-party 'indicated yield' is most wrong (WHR showed
    6.8% on the dashboard with a $0.00 indicated dividend at FactSet). So we read the declared
    per-share series directly and compare the most recent QUARTERLY declaration against the median
    of the preceding four.

    status is one of: 'paying', 'cut', 'suspended', or None when there is not enough history.
    Quarterly entries only (55-115 days) so an annual roll-up can't be mistaken for a quarter."""
    q = [e for e in _flows(facts, _DIV_PER_SHARE) if 55 <= e["days"] <= 115 and e.get("end")]
    if len(q) < 3:
        return None, None, None
    q.sort(key=lambda e: e["end"], reverse=True)
    latest = q[0]["val"]
    prior = [e["val"] for e in q[1:5] if e["val"] is not None]
    if not prior or latest is None:
        return latest, None, None
    prior.sort()
    run_rate = prior[len(prior) // 2]                # median of the preceding quarters
    if run_rate <= 0:
        return latest, run_rate, ("paying" if latest > 0 else None)
    if latest <= 0:
        return latest, run_rate, "suspended"
    if latest <= run_rate * 0.60:                    # a 40%+ cut is a deliberate policy change
        return latest, run_rate, "cut"
    return latest, run_rate, "paying"


def _short_term_investments(facts, cash_c):
    """Short-term investments to add to cash for the liquidity read — precision over recall, so it
    can never manufacture a false 'cash-rich' flag.

    1. Prefer an EXPLICITLY-current investment tag (unambiguous). Use it as-is.
    2. Else fall back to an un-suffixed base tag (some issuers, e.g. Vital Farms, file CURRENT
       securities there) — but ONLY when both guards hold:
         (a) the filer reports NO long-term/noncurrent investment tag, and
         (b) cash + the investment stays within total current assets (AssetsCurrent).
       Either guard failing means the base tag can't be assumed current, so we skip it. A company
       holding long-term securities under the same base tag is therefore never counted as cash.

    Returns the short-term-investment amount, or None if nothing safe to add."""
    cur = _instant(facts, _STI_CURRENT)
    if cur is not None:
        return cur
    base = _instant(facts, _STI_AMBIG)
    if base is None:
        return None
    if _instant(facts, _LT_INVEST) is not None:
        return None                                  # long-term securities present -> ambiguous, skip
    ac = _instant(facts, _ASSETS_CURRENT)
    if ac is not None and (cash_c or 0) + base > ac:
        return None                                  # would exceed current assets -> not all current
    return base


def _extract(facts):
    """Return (metrics, raw). Signals are computed from the company's MOST RECENT
    reporting period (latest 10-Q year-to-date), falling back to the latest annual
    10-K when the quarterly data isn't cleanly available -- so scores refresh
    quarterly while never doing worse than annual."""
    rev_f = _flows(facts, _REV); op_f = _flows(facts, _OPINC)
    sga_f = _flows(facts, _SGA); ni_f = _flows(facts, _NI); dep_f = _flows(facts, _DEP)
    int_f = _flows(facts, _INT_EXP); div_f = _flows(facts, _DIV_PAID)
    buyback_f = _flows(facts, _BUYBACK)

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
        dep = _at(dep_f, p_end, p_days)
        int_exp = _at(int_f, p_end, p_days)
        rev_prior = _prior_year(rev_f, p_end, p_days)
    else:
        p_end = p_days = p_accn = None
        rev = opinc = sga = ni = dep = int_exp = rev_prior = None

    assets = _instant(facts, _ASSETS)
    equity = _instant(facts, _EQUITY)
    cash_c = _instant(facts, _CASH)
    # Staleness guard: if even the freshest candidate cash tag trails the current balance sheet
    # (the Assets tag) by more than _CASH_STALE_DAYS, every candidate has been abandoned -- treat
    # cash as unknown rather than surface a frozen figure as current (see _CASH comment above).
    if cash_c is not None:
        _cash_end = _latest_instant_end(facts, _CASH)
        _assets_end, _ = _instant_dated(facts, _ASSETS[0])
        if _cash_end and _assets_end:
            _cash_gap = _ddays(_cash_end, _assets_end)
            if _cash_gap is not None and _cash_gap > _CASH_STALE_DAYS:
                cash_c = None
    sti = _short_term_investments(facts, cash_c)
    cash = (cash_c or 0) + (sti or 0) if (cash_c is not None or sti is not None) else None
    debt = _total_debt(facts)
    goodwill = _instant(facts, _GOODWILL)
    _ol_nc = _instant(facts, _OP_LEASE_NC); _ol_cur = _instant(facts, _OP_LEASE_CUR)
    op_lease = ((_ol_nc or 0) + (_ol_cur or 0)) if (_ol_nc is not None or _ol_cur is not None) else None
    _fl_nc = _instant(facts, _FIN_LEASE_NC); _fl_cur = _instant(facts, _FIN_LEASE_CUR)
    fin_lease = ((_fl_nc or 0) + (_fl_cur or 0)) if (_fl_nc is not None or _fl_cur is not None) else None
    if fin_lease is None:                        # some filers report only a combined finance-lease tag
        fin_lease = _instant(facts, _FIN_LEASE_TOTAL)
    shares = _latest_shares(facts)

    # EBITDA = operating income + D&A. For EV/EBITDA (EV is a point-in-time figure) this MUST
    # be an annual number — using a single quarter's EBITDA against full EV inflated the
    # multiple ~4x (the 200x+ readings). Prefer the latest FULL YEAR (10-K) op income + D&A;
    # fall back to annualizing the current period.
    fy_opinc = _annual_latest(op_f)
    fy_dep = _annual_latest(dep_f)
    if fy_opinc is not None and fy_dep is not None:
        ebitda = fy_opinc + fy_dep
    elif opinc is not None and dep is not None:
        ebitda = opinc + dep
        if p_days and p_days < 350:
            ebitda = ebitda * 365.0 / p_days
    else:
        ebitda = None

    # annualize a partial-year net income so ROA is comparable across fiscal positions
    ni_ann = ni
    if ni is not None and p_days and p_days < 350:
        ni_ann = ni * 365.0 / p_days

    # --- TTM (trailing-twelve-month) basis for the operating-performance signals -------------
    # A single fiscal quarter is seasonally distorted (a jeweler's post-holiday Q1 trough, a
    # builder's winter Q1), so margin / SG&A / ROA are rated on the trailing YEAR instead:
    #   TTM = latest full year (10-K) + THIS interim (YTD) − prior-year same interim.
    # Every part is pulled for the SAME reporting period, so numerator and denominator can't
    # drift apart. Revenue-growth (annual) and EV/EBITDA (annual) already sidestep the quarter;
    # this puts margin/ROA/SG&A on the same honest footing. Falls back to the reporting-period
    # figures (annualized ROA) when the base is already a full year or a clean TTM isn't available.
    _is_interim = p_days is not None and p_days < 350
    if _is_interim:
        t_rev = _ttm_from(_annual_latest(rev_f), rev, rev_prior)
        t_opinc = _ttm_from(_annual_latest(op_f), opinc, _prior_year(op_f, p_end, p_days))
        t_sga = _ttm_from(_annual_latest(sga_f), sga, _prior_year(sga_f, p_end, p_days))
        t_ni = _ttm_from(_annual_latest(ni_f), ni, _prior_year(ni_f, p_end, p_days))
    else:
        t_rev = t_opinc = t_sga = t_ni = None

    # Trailing-12mo dividends paid, same TTM machinery as above (falls back to the latest full
    # year when a clean TTM isn't available). Cash-flow tag reports the outflow as a positive
    # figure, matching PaymentsOfDividends* convention.
    if p_end:
        div_paid_ttm = (_ttm_from(_annual_latest(div_f), _at(div_f, p_end, p_days),
                                  _prior_year(div_f, p_end, p_days))
                        if _is_interim else None) or _annual_latest(div_f)
    else:
        div_paid_ttm = _annual_latest(div_f)
    if _is_interim and t_rev and t_rev > 0 and t_opinc is not None:
        m_rev, m_opinc, m_sga, m_ni = t_rev, t_opinc, t_sga, t_ni
        m_label = ("trailing 12 mo to " + _pd(p_end).strftime("%b %Y")) if _pd(p_end) else "trailing 12 mo"
        m_basis = "ttm"
    else:                                   # base already a full year, or no clean TTM available
        m_rev, m_opinc, m_sga, m_ni = rev, opinc, sga, ni_ann
        m_label = _period_label(p_end, p_days) if p_end else None
        m_basis = "period"

    def ratio(n, d, lo=None, hi=None):
        if n is None or not d or d <= 0:
            return None
        r = n / d
        if (lo is not None and r < lo) or (hi is not None and r > hi):
            return None
        return r

    def ratio_cap(n, d, lo, hi):
        # Like ratio(), but CAPS extreme values instead of nulling them. Used for margin and
        # ROA so a deep cash-burner (e.g. a clinical biotech at -700% margin) still registers
        # as deeply negative — and so still trips the low-margin signal AND the deep-burn
        # discount — instead of clamping to None and silently escaping both.
        if n is None or not d or d <= 0:
            return None
        return max(lo, min(hi, n / d))

    # Revenue growth: prefer full-year (10-K) YoY — a single quarter's YoY is misleading for
    # lumpy-revenue names and made real double-digit growers (e.g. SDGR) read as flat/negative.
    # Fall back to the period-over-prior-year figure only when two annual periods aren't there.
    # Also capture the exact (current, prior) pair the % was computed from + a period label, so
    # the evidence card shows numbers that MATCH the headline % (previously it showed the latest
    # quarter's revenue vs prior-year quarter — a different basis that contradicted an annual %).
    growth, g_cur, g_prior, g_cur_end, _g_prior_end = _annual_growth(rev_f)
    g_period = _period_label(g_cur_end, 365) if g_cur_end else None      # e.g. "FY2025"
    if growth is None and rev is not None and rev_prior and rev_prior > 0:
        g = (rev - rev_prior) / rev_prior
        growth = g if -10 < g < 10 else None
        if growth is not None:                          # fell back to the quarterly pair
            g_cur, g_prior = rev, rev_prior
            g_period = _period_label(p_end, p_days) if p_end else None

    # Dividend policy state (paying / cut / suspended) from the declared per-share series.
    _div_latest, _div_run, _div_status = _dividend_state(facts)

    # Trailing ~3 years of actual buyback spend. Compared downstream against market cap: a board
    # that spent more repurchasing stock than the company is now worth, while the stock fell, is
    # the most common capital-allocation attack an activist runs.
    _bb_dated = sorted([e for e in buyback_f if 350 <= e["days"] <= 380 and e.get("end")],
                       key=lambda e: e["end"], reverse=True)[:3]
    _buybacks_3y = sum(abs(e["val"]) for e in _bb_dated if e.get("val")) or None

    # EV debt = total debt less operating-lease liabilities (see the note in `raw` below).
    _ev_debt = (debt - op_lease) if (debt is not None and op_lease is not None) else None
    if _ev_debt is not None and _ev_debt < 0:
        _ev_debt = None                              # inconsistent inputs -> fall back to `debt`

    # Most recent FULL-YEAR net income — the profitability sanity check. A GAAP-profitable
    # year means a negative latest-period margin/ROA is almost certainly a one-time charge
    # (impairment, divestiture) distorting the quarter, not real distress (see scoring).
    annual_ni = _annual_latest(ni_f)

    # Operating-margin TRAJECTORY (#36). Same-period YoY delta: current-period operating margin
    # minus the prior-year same-period margin. A positive delta means margins are IMPROVING —
    # which scoring uses to STOP a "margin turnaround / cut costs" thesis from firing on a company
    # whose margins are already rising (e.g. AECOM). Same-period on both sides so the two are
    # directly comparable, independent of the TTM smoothing used for the level metric above.
    _op_prior_sp = _prior_year(op_f, p_end, p_days) if p_end else None
    _m_now_sp = (opinc / rev) if (opinc is not None and rev and rev > 0) else None
    _m_prior_sp = (_op_prior_sp / rev_prior) if (_op_prior_sp is not None and rev_prior and rev_prior > 0) else None
    margin_yoy_delta = (_m_now_sp - _m_prior_sp) if (_m_now_sp is not None and _m_prior_sp is not None) else None

    metrics = {
        "revenue": m_rev, "revenue_growth": growth,
        "operating_margin": ratio_cap(m_opinc, m_rev, -5, 5), "sga_pct": ratio(m_sga, m_rev, 0, 5),
        "roa": ratio_cap(m_ni, assets, -5, 5),
        "cash_to_assets": ratio(cash, assets, 0, 1),
        "debt_to_assets": ratio(debt, assets, 0, 5),
        "shares": shares, "book_equity": equity,
    }
    raw = {
        "revenue": m_rev, "revenue_prior": rev_prior, "operating_income": m_opinc,
        "revenue_growth_cur": g_cur, "revenue_growth_prior": g_prior,
        "revenue_growth_period": g_period,
        "sga": m_sga, "net_income": ni, "net_income_ann": m_ni,
        "margin_label": m_label, "margin_basis": m_basis,
        "annual_net_income": annual_ni, "margin_yoy_delta": margin_yoy_delta,
        "total_assets": assets, "book_equity": equity, "cash": cash, "debt": debt,
        "dep_amort": dep, "ebitda": ebitda, "goodwill": goodwill, "operating_lease": op_lease,
        "finance_lease": fin_lease, "interest_expense": int_exp,
        "dividends_paid_ttm": div_paid_ttm,
        "dividend_dps_latest": _div_latest, "dividend_dps_run_rate": _div_run,
        "dividend_status": _div_status,
        "buybacks_3y": _buybacks_3y,
        # EV-specific debt: funded debt + FINANCE leases, EXCLUDING operating-lease liabilities.
        # Under ASC 842 operating-lease rent stays inside operating income, so EBITDA is already
        # net of it -- capitalising the same leases into EV as well charges the company twice.
        # Verified on PZZA: stripping operating leases lands within 0.6% of FactSet's own EV.
        # Finance leases stay in: their cost sits in D&A/interest, so EBITDA is gross of them.
        # None when the split is unknown, and consumers fall back to `debt` so nothing regresses.
        "ev_debt": _ev_debt,
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
    for s in database.get_scores(limit=ENRICH_TOP):
        if s.get("ticker"):
            tickers.add(s["ticker"])
    for s in database.get_active_situations(limit=40):
        if s.get("ticker"):
            tickers.add(s["ticker"])
    for w in database.get_watchlist():
        if w.get("ticker"):
            tickers.add(w["ticker"])
    return news.refresh_company_news(sorted(tickers), key)


def _ff(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _finnhub_metrics(symbol, key):
    """Valuation + return metrics from Finnhub's free /stock/metric endpoint:
    1-yr price return, price-to-book, P/E, dividend yield, 52-wk high/low."""
    try:
        r = requests.get(_FINNHUB_METRIC_URL,
                         params={"symbol": symbol, "metric": "all", "token": key}, timeout=20)
        d = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return {}
    m = (d or {}).get("metric") or {}
    series = ((d or {}).get("series") or {}).get("quarterly") or {}

    def latest(name):
        best = None
        for e in series.get(name, []) or []:
            v = _ff(e.get("v"))
            p = e.get("period")
            if v is not None and (best is None or (p and p > best[0])):
                best = (p, v)
        return best[1] if best else None

    tsr = m.get("52WeekPriceReturnDaily")
    return {
        "tsr_1y": (_ff(tsr) / 100.0 if tsr is not None else None),
        "pb": latest("pb") or _ff(m.get("pbAnnual")) or _ff(m.get("pbQuarterly")),
        "pe": _ff(m.get("peTTM")) or _ff(m.get("peExclExtraTTM")),
        # dividendYieldIndicatedAnnual is a percent (6.0 = 6%). Finnhub keeps reporting a stale
        # indicated yield after a board suspends/cuts the dividend, which on a crashed price reads
        # absurd (SSTK showed 24.4% the week its board suspended the dividend). Drop yields above a
        # sane ceiling as unreliable rather than print a phantom yield.
        "dividend_yield": (_divy / 100.0
                           if (_divy := _ff(m.get("dividendYieldIndicatedAnnual"))) is not None
                           and _divy <= 15.0 else None),
        "wk_hi": _ff(m.get("52WeekHigh")),
        "wk_lo": _ff(m.get("52WeekLow")),
    }


def _finnhub_metric_1y(symbol, key):
    """1-yr price return only (thin wrapper kept for refresh_tsr)."""
    return _finnhub_metrics(symbol, key).get("tsr_1y")


def _finnhub_profile(symbol, key):
    """Company profile from Finnhub's free /stock/profile2: market cap (reported in
    millions), shares outstanding, website, industry, exchange."""
    try:
        r = requests.get(_FINNHUB_PROFILE_URL,
                         params={"symbol": symbol, "token": key}, timeout=20)
        return (r.json() or {}) if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return {}


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
    for s in database.get_scores(limit=ENRICH_TOP):
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


# Coverage / recall (Phase 1): the 1-yr return is the cheapest deep signal (one fast Finnhub
# call) AND often the biggest vulnerability driver. The 30-min refresh_tsr above only covers the
# top-ENRICH_TOP shortlist, which creates a chicken-and-egg: a return-driven target (down hard
# but not cheap-on-fundamentals) never ranks top-60, so it never gets a return, so it never
# surfaces. This nightly pass fetches the 1-yr return for a MUCH wider set (ordered by market
# cap — the most pitchable names first) so those names finally get their return signal and can
# clear the board bar on their own merits. Bounded + env-tunable so it can be ramped (60 -> 500
# -> wider) or killed instantly. recompute_all() reads tsr_1y for every fundamentals row, so no
# scoring change is needed — feeding more names their return is the entire mechanism.
BROAD_TSR_TOP = int(os.getenv("BROAD_TSR_TOP", "500"))   # 0 or negative disables the pass
BROAD_TSR_SLEEP = float(os.getenv("BROAD_TSR_SLEEP", "1.0"))  # 60/min Finnhub free tier


def refresh_tsr_broad():
    """Universe-wide 1-yr relative return pre-screen (Phase 1 coverage). Fetches the 1-yr return
    for up to BROAD_TSR_TOP names (by market cap desc) so return-driven targets surface. Runs in
    the nightly rebuild only — never the 30-min cycle — and degrades gracefully (rate-limited /
    missing names simply keep their prior value and retry next night)."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key or BROAD_TSR_TOP <= 0:
        print(f"[tsr-broad] skipped (key={'yes' if key else 'no'}, cap={BROAD_TSR_TOP})")
        return 0
    spy = _finnhub_metric_1y("SPY", key); time.sleep(BROAD_TSR_SLEEP)
    if spy is not None:
        database.set_meta("spy_1y", spy)
    # Order the universe by market cap desc (most pitchable first); names with no cap sort last.
    mcaps = {}
    try:
        for c in database.get_companies():
            mc = c.get("market_cap")
            if mc is not None:
                mcaps[_pad(c["cik"])] = mc
    except Exception:
        traceback.print_exc()
    cand = [c for c in get_universe() if c.get("ticker") and c.get("cik")]
    cand.sort(key=lambda c: mcaps.get(_pad(c["cik"]), -1.0), reverse=True)
    if BROAD_TSR_TOP > 0:
        cand = cand[:BROAD_TSR_TOP]
    done = 0
    for c in cand:
        ret = _finnhub_metric_1y(c["ticker"], key); time.sleep(BROAD_TSR_SLEEP)
        if ret is not None:
            database.set_company_market(_unpad(c["cik"]), tsr_1y=ret)
            done += 1
    print(f"[tsr-broad] updated {done}/{len(cand)} names (cap={BROAD_TSR_TOP}, S&P 1y={spy})")
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
    # Refresh market cap + P/B every cycle now that it's on Finnhub (not Alpha Vantage's
    # daily cap). Skip the one-time description fetch here to keep the 30-min cycle fast.
    try:
        n_enrich = refresh_enrichment(fetch_desc=False)
    except Exception:
        traceback.print_exc(); n_enrich = 0
    print(f"[refresh] news={n_news} company_news={n_cnews} tsr={n_tsr} "
          f"filings={n_filings} flagged={len(flagged)} enriched={n_enrich}")
    return {"news": n_news, "company_news": n_cnews, "tsr": n_tsr,
            "filings": n_filings, "flagged": len(flagged), "enriched": n_enrich}


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


ENTITY_STALE_DAYS = 6


def refresh_entity_master(max_companies=None):
    """Universe-wide entity master (U2a): one queryable row per company with sector
    (from SEC fundamentals), market cap + industry + exchange (Finnhub profile2), and
    index membership (iShares, via universe.ishares_membership). Freshness-gated to
    ~weekly so daily/boot re-runs are cheap. Fails safe per-name; degrades gracefully
    without a Finnhub key (still fills sector + name + index tags)."""
    key = os.getenv("FINNHUB_API_KEY", "")
    try:
        membership = universe.ishares_membership()
    except Exception:
        traceback.print_exc()
        membership = {}
    uni = get_universe()
    subset = uni[:max_companies] if max_companies else uni
    now = datetime.utcnow()
    done = fetched = 0
    for i, c in enumerate(subset):
        cik = c.get("cik")
        if not cik:
            continue
        tk = (c.get("ticker") or "")
        tag = membership.get(tk.upper().replace(".", "-"))
        prev = database.get_entity(cik) or {}
        # Freshness gate: if refreshed recently, only cheaply refresh the index tag.
        if prev.get("updated_at"):
            try:
                if (now - datetime.fromisoformat(prev["updated_at"])).days < ENTITY_STALE_DAYS:
                    if tag and tag != prev.get("index_tags"):
                        database.upsert_entity(cik, index_tags=tag)
                    continue
            except (ValueError, TypeError):
                pass
        # Sector from the (padded-cik) fundamentals row's raw blob.
        sector = None
        try:
            raw = json.loads((database.get_fundamentals_one(_pad(cik)) or {}).get("raw") or "{}")
            sector = raw.get("sector_desc")
        except (ValueError, TypeError):
            pass
        mcap = industry = exchange = None
        if key and tk:
            prof = _finnhub_profile(tk, key); time.sleep(0.2)
            m = _ff(prof.get("marketCapitalization"))
            mcap = m * 1e6 if m else None          # Finnhub reports market cap in millions
            industry = prof.get("finnhubIndustry") or None
            exchange = prof.get("exchange") or None
            fetched += 1
        database.upsert_entity(cik, ticker=tk, name=c.get("name"), sector=sector,
                               industry=industry, exchange=exchange, market_cap=mcap,
                               index_tags=tag)
        done += 1
        if i % 50 == 0:
            gc.collect()
    print(f"[entity] entity master refreshed: {done} rows updated "
          f"({fetched} Finnhub profile fetches); {len(subset)} in universe")
    return done


# ---- U2b: free CUSIP -> ticker map from SEC Fails-to-Deliver files ----------
def _ftd_candidate_urls():
    """Recent FTD file URLs, newest first: both halves ('b' then 'a') of the last
    FTD_MONTHS months. Current-month files may not exist yet; _get returns None on 404
    and we move on."""
    urls = []
    now = datetime.utcnow()
    y, mo = now.year, now.month
    for _ in range(max(1, config.FTD_MONTHS)):
        for half in ("b", "a"):
            urls.append(f"{config.FTD_BASE_URL}/cnsfails{y:04d}{mo:02d}{half}.zip")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
    return urls


def _parse_ftd_zip(content):
    """Yield (cusip, ticker, name) from an FTD zip's pipe-delimited text.
    Columns: SETTLEMENT DATE | CUSIP | SYMBOL | QUANTITY | DESCRIPTION | PRICE."""
    out = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError):
        return out
    for nm in zf.namelist():
        try:
            data = zf.read(nm).decode("latin-1", "ignore")
        except (OSError, RuntimeError):
            continue
        for line in data.splitlines():
            parts = line.split("|")
            if len(parts) < 5:
                continue
            cusip = parts[1].strip()
            sym = parts[2].strip().upper()
            name = parts[4].strip()
            if len(cusip) != 9 or not sym or sym == "SYMBOL":
                continue
            out.append((cusip, sym, name))
    return out


def refresh_cusip_map(max_files=None):
    """Build/refresh the cusip->ticker map from SEC Fails-to-Deliver files (free, public).
    Each recent file covers ~all actively traded names; we take the newest FTD_MAX_FILES
    that download, keeping the most-recent ticker per CUSIP, then backfill entity.cusip
    for names we track. Fails safe: keeps the existing map on any error."""
    cap = max_files if max_files is not None else config.FTD_MAX_FILES
    latest = {}                      # cusip -> (ticker, name); first (newest) file wins
    files_ok = rows_total = 0
    for url in _ftd_candidate_urls():
        if files_ok >= cap:
            break
        r = _get(_sec, url)
        if not r:
            continue
        rows = _parse_ftd_zip(r.content)
        if not rows:
            continue
        files_ok += 1
        rows_total += len(rows)
        for cusip, sym, name in rows:
            latest.setdefault(cusip, (sym, name))
        time.sleep(0.3)
    if not latest:
        print("[cusip] no FTD data fetched; keeping existing map")
        return 0
    database.upsert_cusips([(c, t, n, "ftd") for c, (t, n) in latest.items()])
    # Backfill entity.cusip for tracked tickers (so profiles / 13F matching have it).
    try:
        tick_to_cusip = {}
        for c, (t, _n) in latest.items():
            tick_to_cusip.setdefault(t, c)
        for e in database.get_all_entities():
            if e.get("cusip"):
                continue
            cu = tick_to_cusip.get((e.get("ticker") or "").upper())
            if cu:
                database.upsert_entity(e["cik"], cusip=cu)
    except Exception:
        traceback.print_exc()
    print(f"[cusip] map refreshed from {files_ok} FTD file(s): {len(latest)} unique cusips "
          f"({rows_total} rows scanned); map size={database.cusip_map_count()}")
    return len(latest)


def _openfigi_ticker(cusip):
    """Map a single CUSIP -> ticker via the free OpenFIGI API. None on any failure.
    (OpenFIGI accepts a CUSIP as input and returns the ticker -- the direction we need;
    it just won't hand out CUSIPs.)"""
    try:
        headers = {"Content-Type": "application/json"}
        if config.OPENFIGI_API_KEY:
            headers["X-OPENFIGI-APIKEY"] = config.OPENFIGI_API_KEY
        r = requests.post(config.OPENFIGI_URL,
                          json=[{"idType": "ID_CUSIP", "idValue": cusip}],
                          headers=headers, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        arr = (data[0] or {}).get("data") if data else None
        if arr:
            return ((arr[0].get("ticker") or "").upper()) or None
    except (requests.RequestException, ValueError, IndexError, KeyError, TypeError):
        return None
    return None


def resolve_cusip(cusip):
    """Resolve a CUSIP -> ticker: cached map (FTD / entity) first, then OpenFIGI gap-fill
    (caching the hit back into cusip_map). This is the single call 4b's 13F parser makes
    per holding."""
    if not cusip:
        return None
    tk = database.ticker_for_cusip(cusip)
    if tk:
        return tk
    tk = _openfigi_ticker(cusip)
    if tk:
        database.upsert_cusip(cusip, tk, source="openfigi")
    return tk


def refresh_governance():
    """Parse DEF 14A governance red flags for shortlist / active / watchlist names
    (cached by filing accession), then rescore. Free SEC data."""
    ciks = set()
    for s in database.get_scores(limit=ENRICH_TOP):
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
    for s in database.get_scores(limit=ENRICH_TOP):
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
    for s in database.get_scores(limit=ENRICH_TOP):
        if s.get("cik"):
            ciks.add(s["cik"])
    for s in database.get_active_situations(limit=40):
        if s.get("cik"):
            ciks.add(s["cik"])
    for w in database.get_watchlist():
        if w.get("cik"):
            ciks.add(w["cik"])
    return ciks


def _tracked_pairs(top=ENRICH_TOP):
    pairs = {}
    for s in database.get_scores(limit=top):
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
    # Only the full universe sweep is allowed to CLEAR flags (it checks everyone). The
    # tracked-only sweep just adds/refreshes, so it can never erase a Confirmed situation
    # that lives outside the small tracked subset.
    n = activist.refresh_activist(ciks, clear_missing=full)
    # Resolution sweep: disclosed-holder (13F) names that are actually being ACQUIRED or have SETTLED
    # should leave the passive tier -> flag them so the rescore marks them active_situation and
    # accumulating() drops them (Masimo/Danaher -> M&A pending acquisition; Teradata/Lynrock -> settled).
    try:
        disclosed = [(r["cik"], (r.get("holders") or [{}])[0].get("fund"))
                     for r in database.accumulating()
                     if r.get("disclosed") and r.get("cik")]
        if disclosed:
            activist.sweep_resolved(disclosed)
    except Exception:
        traceback.print_exc()
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


def _finnhub_sentiment(symbol, key):
    """Most recent monthly insider-sentiment (MSPR, -100..100) from Finnhub (free)."""
    frm = (datetime.utcnow() - timedelta(days=180)).date().isoformat()
    to = datetime.utcnow().date().isoformat()
    try:
        r = requests.get(_FINNHUB_INSIDER_SENT_URL,
                         params={"symbol": symbol, "from": frm, "to": to, "token": key}, timeout=20)
        d = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return None
    rows = (d or {}).get("data") or []
    if not rows:
        return None
    rows.sort(key=lambda x: (x.get("year", 0), x.get("month", 0)))
    last = rows[-1]
    return {"mspr": _ff(last.get("mspr")),
            "month": f"{last.get('year')}-{str(last.get('month')).zfill(2)}"}


def _finnhub_recommendation(symbol, key):
    """Latest analyst recommendation counts from Finnhub (free, display-only)."""
    try:
        r = requests.get(_FINNHUB_RECO_URL, params={"symbol": symbol, "token": key}, timeout=20)
        d = r.json() if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return None
    if not d:
        return None
    a = d[0]
    return {"strongBuy": a.get("strongBuy"), "buy": a.get("buy"), "hold": a.get("hold"),
            "sell": a.get("sell"), "strongSell": a.get("strongSell"), "period": a.get("period")}


def refresh_sentiment():
    """Insider sentiment (MSPR) + analyst recommendation trends for tracked names via
    Finnhub (free; existing key). Display-only context for the profile -- does NOT feed
    the rating (insider behavior is already scored from Form 4; analyst posture isn't an
    activist signal)."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        print("[sentiment] no FINNHUB_API_KEY set; skipping")
        return 0
    pairs = _tracked_pairs()
    done = 0
    for cik, tk in pairs.items():
        s = _finnhub_sentiment(tk, key); time.sleep(0.2)
        rec = _finnhub_recommendation(tk, key); time.sleep(0.2)
        if s or rec:
            database.upsert_finnhub_extra(cik, {
                "mspr": (s or {}).get("mspr"), "mspr_month": (s or {}).get("month"),
                "rec_strongbuy": (rec or {}).get("strongBuy"), "rec_buy": (rec or {}).get("buy"),
                "rec_hold": (rec or {}).get("hold"), "rec_sell": (rec or {}).get("sell"),
                "rec_strongsell": (rec or {}).get("strongSell"), "rec_period": (rec or {}).get("period")})
            done += 1
    print(f"[sentiment] updated {done}/{len(pairs)} names")
    return done


def refresh_contacts():
    """Company contacts + descriptions for tracked names via FMP (free; gated by
    FMP_API_KEY). Display-only — does not affect the rating."""
    return fmp.refresh_fmp(_tracked_pairs(), database)


# Charts only need a daily refresh; skip if we already pulled successfully within this window
# so repeated deploys / manual enrichment runs in one day don't re-burn Twelve Data credits.
PRICES_MAX_AGE_HOURS = 18


def refresh_prices(force=False):
    """Daily price history (for the profile chart) for tracked names via Twelve Data
    (free; gated by TWELVEDATA_API_KEY). Display-only. Capped to TD_TOP, and skipped if a
    successful pull happened within PRICES_MAX_AGE_HOURS (credit-budget protection)."""
    if not force:
        last = database.get_meta("prices_at")
        if last:
            try:
                if (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < PRICES_MAX_AGE_HOURS * 3600:
                    print("[prices] skipped (refreshed recently)")
                    return 0
            except (ValueError, TypeError):
                pass
    n = twelvedata.refresh_prices(_tracked_pairs(top=TD_TOP), database)
    if n:                                  # only stamp on success so a 429 still retries next run
        database.set_meta("prices_at", datetime.utcnow().isoformat())
    return n


# Multi-year (3/5-yr) TSR moves slowly, so refresh at most this often to conserve the
# free Twelve Data credit budget even if the daily job / manual runs fire more frequently.
LONG_TSR_MAX_AGE_DAYS = 6


def refresh_long_tsr(force=False):
    """3-yr / 5-yr total return vs the S&P for tracked names via monthly Twelve Data closes
    (1 credit/symbol). Cached: skipped if refreshed within LONG_TSR_MAX_AGE_DAYS. Then rescore."""
    key = twelvedata.key()
    if not key:
        print("[long-tsr] no TWELVEDATA_API_KEY set; skipping")
        return 0
    if not force:
        last = database.get_meta("long_tsr_at")
        if last:
            try:
                age = (datetime.utcnow() - datetime.fromisoformat(last)).days
                if age < LONG_TSR_MAX_AGE_DAYS:
                    print(f"[long-tsr] skipped (refreshed {age}d ago)")
                    return 0
            except (ValueError, TypeError):
                pass
    bench = longtsr.fetch([longtsr.BENCH], key).get(longtsr.BENCH) or {}
    if bench.get("3y") is not None:
        database.set_meta("spy_3y", bench["3y"])
    if bench.get("5y") is not None:
        database.set_meta("spy_5y", bench["5y"])
    pairs = _tracked_pairs(top=TD_TOP)
    sym_to_cik = {}
    for cik, tk in pairs.items():
        if tk:
            sym_to_cik.setdefault(tk, cik)
    syms = list(sym_to_cik)
    got = {}
    # Try batched first; this account's free tier may not support multi-symbol requests,
    # so fall back to one-per-symbol for anything the batch didn't return (rate-limited).
    for i in range(0, len(syms), 25):
        got.update(longtsr.fetch(syms[i:i + 25], key))
        time.sleep(1.0)
    missing = [s for s in syms if not got.get(s)]
    for s in missing:
        r = longtsr.fetch([s], key)
        if r.get(s):
            got[s] = r[s]
        time.sleep(8.0)                 # free tier = 8 requests/minute
    done = 0
    for sym, rr in got.items():
        if not rr or (rr.get("3y") is None and rr.get("5y") is None):
            continue
        database.set_company_market(_unpad(sym_to_cik[sym]),
                                    tsr_3y=rr.get("3y"), tsr_5y=rr.get("5y"))
        done += 1
    # Only stamp the 6-day cache if we actually got data — so a credit-exhausted run
    # (all fetches 429) retries next cycle instead of silently skipping for 6 days.
    if done:
        database.set_meta("long_tsr_at", datetime.utcnow().isoformat())
    print(f"[long-tsr] {done}/{len(syms)} names (S&P 3y={bench.get('3y')} 5y={bench.get('5y')})")
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return done


def refresh_lead_data():
    """Guarantee TODAY'S lead-of-the-day has complete market data — its price chart and
    multi-year TSR — even if the rotation landed on a name outside the TD_TOP set that the
    bulk pulls cover. Cheap (~2 Twelve Data calls), and force-fetched (bypasses the caches)
    so the centerpiece profile is never missing data."""
    key = twelvedata.key()
    if not key:
        return 0
    rows = database.get_scores(limit=80)
    lead = spotlight.todays_lead(rows, database)
    if not lead or not lead.get("ticker"):
        print("[lead-data] no lead resolved; skipping")
        return 0
    cik, tk = lead["cik"], lead["ticker"]
    try:
        twelvedata.refresh_prices({cik: tk}, database)        # price chart for the lead
    except Exception:
        traceback.print_exc()
    try:
        r = longtsr.fetch([tk], key).get(tk)                  # multi-year TSR for the lead
        if r and (r.get("3y") is not None or r.get("5y") is not None):
            database.set_company_market(_unpad(cik), tsr_3y=r.get("3y"), tsr_5y=r.get("5y"))
    except Exception:
        traceback.print_exc()
    print(f"[lead-data] ensured chart + multi-year TSR for lead {tk}")
    return 1


# Exec-change 8-K signals we measure a market reaction for (from edgar classification).
_EXEC_SIGNALS = ("ceo_departure", "leadership_change")
# Results/earnings signals. If a leadership 8-K shares its filing day with one of these, the 1-day
# stock move is driven by the print, not the transition, so we do NOT store it as a leadership
# reaction (Shake Shack: CFO appointment filed the same day as the quarter's results, stock -28% on
# the earnings). Suppressing it here keeps the reaction signal, the pitch, and the catalyst all from
# claiming a false "market lost confidence in the leadership change."
_RESULTS_SIGNALS = ("results_update", "earnings_miss")


def refresh_exec_reactions():
    """For tracked names with a recent CEO/leadership-change 8-K, compute the 1-day stock
    move vs the S&P on the announcement date and cache it (computed once per filing).
    Gated by TWELVEDATA_API_KEY. Free/non-commercial market data."""
    key = twelvedata.key()
    if not key:
        print("[exec-reaction] no TWELVEDATA_API_KEY set; skipping")
        return 0
    pairs = _tracked_pairs(top=TD_TOP)
    computed = cached = 0
    for cik, ticker in pairs.items():
        # most-recent exec-change filing in the scoring window, plus the set of days that carry a
        # results/earnings 8-K (to detect a same-day earnings confound). Scan the whole window —
        # don't break on the first exec filing — so results on the SAME day are seen too.
        latest = None
        results_days = set()
        for f in database.filings_in_window(cik, config.SCORE_WINDOW_DAYS):
            sigs = [s.strip() for s in (f.get("signals") or "").split(",")]
            if any(s in _RESULTS_SIGNALS for s in sigs):
                results_days.add((f.get("filed_at") or "")[:10])
            if latest is None and any(s in _EXEC_SIGNALS for s in sigs):
                latest = f                            # filings come newest-first -> keep the newest
        if not latest or not latest.get("filed_at"):
            continue
        # Same-day earnings confound: the leadership 8-K's 1-day move reflects the print, not the
        # transition. Store a NULL reaction (overwriting any stale move) so the signal can't fire and
        # the catalyst attributes nothing; skip the price fetch entirely.
        if (latest.get("filed_at") or "")[:10] in results_days:
            database.upsert_exec_reaction(cik, ticker, latest["filed_at"], None, None, None, None,
                                          latest.get("title"), latest.get("url"))
            continue
        prev = database.get_exec_reaction(cik)
        if prev and prev.get("filed_at") == latest["filed_at"]:
            cached += 1
            continue                                  # already measured this event
        try:
            res = reaction.fetch_reaction(ticker, latest["filed_at"], key)
        except Exception:
            traceback.print_exc(); res = None
        time.sleep(1.0)
        if not res:
            continue
        database.upsert_exec_reaction(
            cik, ticker, latest["filed_at"], res.get("event_date"),
            res.get("move"), res.get("bench_move"), res.get("abnormal"),
            latest.get("title"), latest.get("url"))
        computed += 1
    print(f"[exec-reaction] computed {computed} · cached {cached} (of {len(pairs)} tracked)")
    try:
        scoring.recompute_all()
    except Exception:
        traceback.print_exc()
    return computed


# IR / Comms contacts are static, so cache each company for ~45 days, and only fetch a
# handful of stale ones per run (each fetch is several SEC calls). Free SEC data.
CONTACTS_MAX_AGE_DAYS = 45        # re-check a company at most this often
CONTACTS_RETRY_DAYS = 20          # but re-try a "found nothing" company sooner (new 8-Ks)
CONTACTS_PER_RUN = 12

# Advisors (law firms / banks) change only on a deal, so cache each company a long time and scan
# just a few stale names per run. Free SEC data (reuses edgar's 8-K press-release text fetcher).
ADVISORS_MAX_AGE_DAYS = 60
ADVISORS_PER_RUN = 25
# Bump when the scanner's coverage changes so cached rows written under the old logic get wiped and
# re-scanned, instead of waiting out their 60-day freshness. (r1 = added offering/prospectus + proxy.)
ADVISORS_VERSION = "2026-07-18-underwriter-syndicate-r2"  # bumped: drop offering-only bank syndicates (#7)


def refresh_advisors():
    """Scan tracked names' recent deal 8-Ks, securities offerings, and merger proxies for allowlisted
    law firms and banks, cached. Feeds the Advisors tab. Free SEC data; bounded per run."""
    if database.get_meta("advisors_ver") != ADVISORS_VERSION:
        database.clear_advisors()
        database.set_meta("advisors_ver", ADVISORS_VERSION)
        print(f"[advisors] scanner bumped to {ADVISORS_VERSION} — cache cleared, full re-scan")
    now = datetime.utcnow()
    fresh = (now - timedelta(days=ADVISORS_MAX_AGE_DAYS)).isoformat()
    pairs = _tracked_pairs()
    budget = ADVISORS_PER_RUN
    done = cached = 0
    for cik, ticker in pairs.items():
        ex = database.get_advisors(cik)
        if ex and (ex.get("updated_at") or "") >= fresh:
            cached += 1
            continue
        if budget <= 0:
            continue
        budget -= 1
        try:
            res = advisors.advisors_for_cik(cik, ticker)
        except Exception:
            traceback.print_exc(); res = None
        time.sleep(0.3)
        if res is not None:
            database.upsert_advisors(cik, res.get("advisors") or [],
                                     res.get("source_url"), res.get("source_date"))
            done += 1
    print(f"[advisors] scanned {done} · cached {cached} (of {len(pairs)} tracked)")
    return done


def refresh_ir_contacts():
    """Parse IR + Media contacts from recent 8-K press-release exhibits for tracked names,
    cached. Feeds the pitch kit's 'who to reach'. Free SEC data.

    On a MISS we still write a timestamped 'tombstone' (empty contacts) so the per-run
    budget moves on to fresh names next time -- otherwise it spins on the same handful of
    no-press-release companies forever and never covers the rest of the universe. Misses
    are retried sooner than hits (a company may file an earnings 8-K with contacts soon)."""
    now = datetime.utcnow()
    fresh_hit = (now - timedelta(days=CONTACTS_MAX_AGE_DAYS)).isoformat()
    fresh_miss = (now - timedelta(days=CONTACTS_RETRY_DAYS)).isoformat()
    pairs = _tracked_pairs()
    budget = CONTACTS_PER_RUN
    done = tombstoned = cached = 0
    for cik in pairs:
        ex = database.get_company_contacts(cik)
        if ex:
            has_contact = ex.get("ir_email") or ex.get("comms_email") or ex.get("ir_name")
            cutoff = fresh_hit if has_contact else fresh_miss
            if (ex.get("updated_at") or "") >= cutoff:
                cached += 1
                continue
        if budget <= 0:
            continue
        budget -= 1
        try:
            res = contacts.fetch_contacts(cik, _sec)
        except Exception:
            res = None
        time.sleep(0.2)
        if res:
            database.upsert_company_contacts(cik, res)
            done += 1
        elif ex and (ex.get("ir_email") or ex.get("comms_email") or ex.get("ir_name")):
            # no fresh press release this time -> KEEP the known-good contact, just reset
            # its clock so we don't re-fetch it next run.
            database.upsert_company_contacts(cik, {
                "ir": {"name": ex.get("ir_name"), "email": ex.get("ir_email"), "phone": ex.get("ir_phone")},
                "comms": {"name": ex.get("comms_name"), "email": ex.get("comms_email"), "phone": ex.get("comms_phone")},
                "source_url": ex.get("source_url")})
            cached += 1
        else:
            database.upsert_company_contacts(cik, {})   # tombstone: tried, none found
            tombstoned += 1
    print(f"[ir-contacts] fetched {done} · none-found {tombstoned} · cached {cached} "
          f"(of {len(pairs)} tracked)")
    return done


def refresh_ai_thesis():
    """Re-voice the templated pitch thesis + talking points with Claude Haiku, for the top
    leads + active situations. Cached by a hash of the underlying facts (only re-calls when
    the grounded pitch changes). Gated by ANTHROPIC_API_KEY; degrades to the template."""
    if not aithesis.key():
        print("[ai-thesis] no ANTHROPIC_API_KEY set; skipping (templated pitch stays in use)")
        return 0
    # Same set we pull multi-year TSR for (top TD_TOP leads + active situations + watchlist),
    # so the polished pitch lines up with where we have the fullest data.
    allowed = set(_tracked_pairs(top=TD_TOP).keys())
    cand = list(database.get_scores(limit=ENRICH_TOP)) + list(database.get_active_situations(limit=40))
    rows = [s for s in cand if s.get("cik") in allowed]
    seen = set()
    done = cached = 0
    for s in rows:
        cik = s.get("cik")
        if not cik or cik in seen:
            continue
        seen.add(cik)
        try:
            pj = json.loads(s.get("pitch") or "{}")
        except (ValueError, TypeError):
            pj = {}
        if not pj.get("thesis"):
            continue
        try:
            ev = json.loads(s.get("evidence") or "[]")
        except (ValueError, TypeError):
            ev = []
        facts = [e.get("context") for e in ev if e.get("context")][:8]
        h = aithesis.facts_hash(s.get("company"), pj, facts)
        if (database.get_ai_pitch(cik) or {}).get("hash") == h:
            cached += 1
            continue
        out = aithesis.revoice(s.get("company"), pj, facts)
        if out:
            database.upsert_ai_pitch(cik, h, out)
            done += 1
        time.sleep(0.3)
    print(f"[ai-thesis] revoiced {done} · cached {cached}")
    return done


def daily_rescore_and_digest():
    refresh_data()
    refresh_tsr_broad()      # Phase 1 coverage: wide 1-yr return pre-screen before the nightly rescore
    refresh_fundamentals()
    refresh_governance()
    refresh_insider()
    refresh_votes()
    refresh_activist(full=True)
    refresh_earnings()
    refresh_sentiment()
    refresh_contacts()
    refresh_ir_contacts()
    refresh_advisors()
    # Exec-reactions + multi-year TSR before the bulk chart pull so these rarer/cached event
    # & valuation signals aren't starved of Twelve Data credits on a tight day (free=800/day).
    refresh_exec_reactions()
    refresh_long_tsr()
    refresh_prices()
    refresh_lead_data()
    refresh_enrichment()
    refresh_ai_thesis()
    # Daily email PAUSED (2026-07) — moving to a curated BI-WEEKLY report. The full daily rescore
    # above still runs so the site stays fresh; only the email send is disabled. Re-enable (or wire
    # the bi-weekly sender) here when the new report is finalized.
    return 0


def startup_full_refresh():
    print("[boot] VERSION=F3-exec-reaction+F2-payperf+F1-evebitda-goodwill  starting refresh")
    refresh_data()
    refresh_tsr_broad()      # Phase 1 coverage: wide 1-yr return pre-screen before the nightly rescore
    refresh_fundamentals()
    refresh_governance()
    refresh_insider()
    refresh_votes()
    # Full universe sweep on boot (not just tracked names) so the Confirmed tier is
    # comprehensive right after a deploy, instead of waiting for the 4pm ET daily job.
    refresh_activist(full=True)
    refresh_earnings()
    refresh_sentiment()
    refresh_contacts()
    refresh_ir_contacts()
    refresh_advisors()
    refresh_exec_reactions()       # before refresh_prices: protect event-signal credits
    refresh_long_tsr()
    refresh_prices()
    refresh_lead_data()
    refresh_enrichment()
    refresh_ai_thesis()
    # U2a: fill the entity master last (universe-wide, ~25 min) so nothing above waits on it.
    try:
        refresh_entity_master()
    except Exception:
        traceback.print_exc()
    # U2b: build the free CUSIP->ticker map from SEC Fails-to-Deliver files.
    try:
        refresh_cusip_map()
    except Exception:
        traceback.print_exc()


# ---- Market enrichment: Finnhub (60/min) for cap + P/B; AV cached for description --
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


# Cap on Alpha Vantage description fetches per enrichment run (free tier ~25/day).
# Descriptions are static, so we fetch each once and cache it forever -- a handful
# per run quickly covers the whole tracked list without ever hitting the cap.
_AV_DESC_PER_RUN = 6


def refresh_enrichment(fetch_desc=True):
    """Market cap + P/B + valuation for tracked names via Finnhub (free, 60/min),
    so valuation can refresh every cycle instead of once daily. The long company
    description is cached once from Alpha Vantage (static, so its daily cap never
    bites). P/B is computed as market cap / SEC book equity (most reliable), falling
    back to Finnhub's reported P/B. Rescores afterward."""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        print("[enrich] no FINNHUB_API_KEY set; skipping")
        return 0
    av_key = _av_key()
    pairs = _tracked_pairs()
    done = 0
    av_budget = _AV_DESC_PER_RUN
    for cik, tk in pairs.items():
        prof = _finnhub_profile(tk, key); time.sleep(0.2)
        met = _finnhub_metrics(tk, key); time.sleep(0.2)
        mcap = _ff(prof.get("marketCapitalization"))
        mcap = mcap * 1e6 if mcap else None        # Finnhub reports market cap in millions
        try:
            raw = json.loads((database.get_fundamentals_one(cik) or {}).get("raw") or "{}")
        except (ValueError, TypeError):
            raw = {}
        # P/B. A company with NEGATIVE book equity has no meaningful price-to-book, and the old
        # `book > 0` guard silently handed those to Finnhub instead -- which returned 303.03x for
        # Papa John's (book equity -$444.8M) and the Financials tab rendered it "richly valued".
        # Report None so the metric is absent rather than wrong; the leverage signal is where a
        # negative-equity balance sheet should surface, and it now does.
        book = raw.get("book_equity")
        pb = (mcap / book) if (mcap and book and book > 0) else None

        # P/E, computed locally from TTM net income for the same reason. Finnhub reported 45.19x
        # for Monro against $2.2M of net income (a true trailing multiple near 178x).
        _ni_ttm = raw.get("net_income_ann") or raw.get("annual_net_income")
        pe = (mcap / _ni_ttm) if (mcap and _ni_ttm and _ni_ttm > 0) else None
        if pe is not None and pe > 400:
            pe = None                                # beyond this the multiple is noise, not signal

        # Dividend yield. Third-party indicated yields were wrong on every name we audited --
        # Monro 3.8% vs 8.92%, Papa John's 2.7% vs 7.69%, Whirlpool 6.8% against a dividend the
        # board had SUSPENDED -- because the vendor divides a stale indicated rate by a stale
        # price. So the local, filing-traceable figure is primary and the vendor is the check.
        #
        # A trailing yield is itself wrong once a payer stops paying, so the dividend STATE gates
        # it: a suspended dividend reports 0.0, a cut reports the new run-rate annualised, and only
        # an intact payer reports the trailing figure.
        div_paid = raw.get("dividends_paid_ttm")
        div_yield_local = (abs(div_paid) / mcap) if (div_paid and mcap) else None
        div_status = raw.get("dividend_status")
        shares_out = raw.get("shares") or (raw.get("book_equity") and None)
        if div_status == "suspended":
            div_yield = 0.0
        elif div_status == "cut" and raw.get("dividend_dps_latest") is not None and shares_out and mcap:
            div_yield = (raw["dividend_dps_latest"] * 4.0 * shares_out) / mcap
        else:
            div_yield = div_yield_local
        div_yield_fh = met.get("dividend_yield")
        if div_yield is None:
            div_yield = div_yield_fh                 # no XBRL dividend data -> vendor as fallback
        if div_yield_fh is not None and div_yield_local is not None:
            _base = max(div_yield_fh, div_yield_local, 1e-6)
            if abs(div_yield_fh - div_yield_local) / _base > 0.35:
                print(f"[enrich] {tk}: dividend yield mismatch — Finnhub={div_yield_fh:.2%} "
                      f"local(XBRL)={div_yield_local:.2%} status={div_status or 'unknown'}")
        if mcap is not None and met.get("tsr_1y") is not None:
            database.set_company_market(_unpad(cik), market_cap=mcap, pb_ratio=pb,
                                        tsr_1y=met.get("tsr_1y"))
        elif mcap is not None or pb is not None:
            database.set_company_market(_unpad(cik), market_cap=mcap, pb_ratio=pb)

        prev = database.get_av_overview(cik) or {}
        desc = prev.get("Description")
        if not desc and fetch_desc and av_key and av_budget > 0:
            d = _av_overview(tk, av_key)
            if d and "Symbol" in d:
                desc = d.get("Description")
            av_budget -= 1
            time.sleep(13)                         # respect AV free pace (only until cached)
        overview = {
            "Description": desc or prev.get("Description"),
            "Sector": raw.get("sector_desc") or prev.get("Sector"),
            "Industry": prof.get("finnhubIndustry") or prev.get("Industry"),
            "Exchange": prof.get("exchange") or prev.get("Exchange"),
            "OfficialSite": prof.get("weburl") or prev.get("OfficialSite"),
            "MarketCapitalization": mcap,
            "PriceToBookRatio": pb,
            "PERatio": pe if pe is not None else met.get("pe"),
            "DividendYield": div_yield,
            "DividendStatus": raw.get("dividend_status"),
            "DividendYieldFinnhub": div_yield_fh,
            "DividendYieldLocal": div_yield_local,
            "52WeekHigh": met.get("wk_hi"),
            "52WeekLow": met.get("wk_lo"),
        }
        database.upsert_av_overview(cik, tk, overview)
        done += 1
    print(f"[enrich] Finnhub market data for {done}/{len(pairs)} names")
    try:
        scoring.recompute_all()
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
