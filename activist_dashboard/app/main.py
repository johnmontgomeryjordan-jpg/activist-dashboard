"""
FastAPI app: serves the dashboard, JSON API (incl. per-company detail + CSV
export), the subscribe endpoint, and runs the background scheduler.
"""
import json
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database, pipeline, emailer, scoring

STATIC_DIR = Path(__file__).resolve().parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def _run_initial_refresh():
    try:
        pipeline.startup_full_refresh()
    except Exception as e:  # pragma: no cover
        print(f"[startup] initial refresh failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    pipeline.get_universe()
    scheduler.add_job(pipeline.refresh_data,
                      IntervalTrigger(minutes=config.REFRESH_MINUTES),
                      id="refresh", replace_existing=True, max_instances=1)
    scheduler.add_job(pipeline.daily_rescore_and_digest,
                      CronTrigger(hour=config.DIGEST_HOUR_ET, minute=0,
                                  timezone=config.TIMEZONE),
                      id="digest", replace_existing=True, max_instances=1)
    scheduler.start()
    threading.Thread(target=_run_initial_refresh, daemon=True).start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Activist Vulnerability Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/feed")
def api_feed():
    return {"news": database.recent_news(limit=25),
            "filings": database.recent_filings(limit=25)}


@app.get("/api/shortlist")
def api_shortlist():
    # Return a deep, ranked list so the dashboard table is explorable (searchable/sortable)
    # and the rotating "spotlight" + "new & rising" have a real pool to draw from.
    rows = database.get_scores(limit=75)
    notes = database.get_notes_set()
    for c in rows:
        prior = database.prior_score(c["cik"])
        c["week_change"] = (c["score"] - prior) if prior is not None else None
        c["has_note"] = c["cik"] in notes
    return {"companies": rows}


@app.get("/api/active-situations")
def api_active_situations():
    """Companies where an activist is already engaged, tagged by confidence tier:
    confirmed (SEC filing), reported (named in the press), or manual (partner-tagged)."""
    rows = database.get_active_situations(limit=60)
    notes = database.get_notes_set()
    manual = database.get_manual_situations()
    for c in rows:
        prior = database.prior_score(c["cik"])
        c["week_change"] = (c["score"] - prior) if prior is not None else None
        c["has_note"] = c["cik"] in notes
        c["tier"] = c.get("situation_tier") or ""
        try:
            c["meta"] = json.loads(c.get("situation_meta") or "{}")
        except (ValueError, TypeError):
            c["meta"] = {}
        c["manual"] = c["cik"] in manual
    return {"companies": rows}


@app.post("/api/situation")
async def api_situation(request: Request):
    """Manually add a company to (action=active) or remove it from (action=exclude)
    Active Situations, or drop the override (action=clear). Overrides always win over
    automated detection; every change is recorded in the audit trail."""
    data = await request.json()
    cik = (data.get("cik") or "").strip()
    action = (data.get("action") or "").strip()
    actor = (data.get("actor") or "").strip()
    note = (data.get("note") or "").strip()
    company = (data.get("company") or "").strip()
    if not cik or action not in ("active", "exclude", "clear"):
        return JSONResponse({"ok": False, "error": "need cik and action in active|exclude|clear"},
                            status_code=400)
    if action == "clear":
        database.clear_manual_situation(cik, actor, note, company)
    else:
        database.set_manual_situation(cik, action, actor, note, company)
    try:
        scoring.recompute_all()
    except Exception:
        import traceback
        traceback.print_exc()
    return {"ok": True, "status": "" if action == "clear" else action}


@app.get("/api/situation-audit")
def api_situation_audit(cik: str = None):
    """The manual-override audit trail (all, or for one company)."""
    return {"audit": database.get_situation_audit(cik, limit=200)}


@app.get("/api/watchlist")
def api_watchlist():
    """Shared watchlist, each item enriched with its current status."""
    out = []
    for w in database.get_watchlist():
        cik = w["cik"]
        sc = database.get_score_one(cik)
        try:
            fraw = json.loads(database.get_fundamentals_one(cik).get("raw") or "{}")
        except (ValueError, TypeError):
            fraw = {}
        if sc:
            status = "active" if sc.get("active_situation") else "flagged"
            prior = database.prior_score(cik)
            week_change = (sc.get("score") - prior) if prior is not None else None
            score, signals, mcap = sc.get("score"), sc.get("signals"), sc.get("market_cap")
            vuln = sc.get("vuln")
        else:
            status = "inactive" if fraw.get("inactive") else "dropped"
            score = signals = week_change = mcap = vuln = None
        note = database.get_note(cik)
        out.append({"cik": cik, "ticker": w.get("ticker"), "company": w.get("company"),
                    "note": note, "has_note": bool(note.strip()), "status": status,
                    "score": score, "vuln": vuln, "signals": signals,
                    "week_change": week_change, "market_cap": mcap,
                    "added_at": w.get("added_at")})
    return {"items": out}


@app.post("/api/watchlist/add")
async def api_watchlist_add(request: Request):
    data = await request.json()
    cik = (data.get("cik") or "").strip()
    if not cik:
        return JSONResponse({"ok": False, "error": "missing cik"}, status_code=400)
    database.add_watchlist(cik, (data.get("ticker") or "").strip(),
                           (data.get("company") or "").strip())
    return {"ok": True}


@app.post("/api/watchlist/remove")
async def api_watchlist_remove(request: Request):
    data = await request.json()
    cik = (data.get("cik") or "").strip()
    database.remove_watchlist(cik)
    return {"ok": True}


@app.post("/api/note")
async def api_note(request: Request):
    """Save a company-level pitch note (shared; the single source of truth, shown on
    the company profile regardless of whether it's on the shortlist or watchlist)."""
    data = await request.json()
    cik = (data.get("cik") or "").strip()
    if not cik:
        return JSONResponse({"ok": False, "error": "missing cik"}, status_code=400)
    database.set_note(cik, data.get("note") or "")
    return {"ok": True}


@app.post("/api/watchlist/note")
async def api_watchlist_note(request: Request):
    data = await request.json()
    cik = (data.get("cik") or "").strip()
    database.set_note(cik, data.get("note") or "")
    return {"ok": True}


@app.get("/api/shortlist.csv")
def api_shortlist_csv():
    import csv, io
    rows = database.get_scores(limit=config.SHORTLIST_SIZE)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rank", "Company", "Ticker", "Market cap", "Score",
                "Weekly change", "Key signals", "First flagged"])
    for i, c in enumerate(rows, 1):
        prior = database.prior_score(c["cik"])
        chg = "" if prior is None else f"{c['score'] - prior:+d}"
        w.writerow([i, c.get("company"), c.get("ticker"), c.get("market_cap"),
                    c.get("score"), chg, c.get("signals"), c.get("first_flagged")])
    fname = "companies_to_pitch.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/company")
def api_company(cik: str):
    """Full detail for one flagged company."""
    score = database.get_score_one(cik)
    if not score:
        return JSONResponse({"ok": False, "error": "Company not found."},
                            status_code=404)
    ticker = score.get("ticker")
    fund = database.get_fundamentals_one(cik)
    av = database.get_av_overview(cik)
    prior = database.prior_score(cik)
    gov = database.get_governance(cik) or {}
    ins = database.get_insider(cik) or {}
    vot = database.get_votes(cik) or {}
    ear = database.get_earnings(cik) or {}
    fx = database.get_finnhub_extra(cik) or {}
    prof = database.get_company_profile(cik) or {}
    con = database.get_company_contacts(cik) or {}
    px = database.get_prices(cik) or {}
    aflag = database.get_activist_flag(cik) or {}
    market = database.get_company_market(cik)

    def _f(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    tsr_1y = _f(market.get("tsr_1y"))
    spy_1y = _f(database.get_meta("spy_1y"))
    tsr_gap = (tsr_1y - spy_1y) if (tsr_1y is not None and spy_1y is not None) else None

    try:
        evidence = json.loads(score.get("evidence") or "[]")
    except (ValueError, TypeError):
        evidence = []

    try:
        situation_meta = json.loads(score.get("situation_meta") or "{}")
    except (ValueError, TypeError):
        situation_meta = {}
    try:
        pitch_obj = json.loads(score.get("pitch") or "{}")
    except (ValueError, TypeError):
        pitch_obj = {}
    try:
        fin_context = json.loads(score.get("fin_context") or "[]")
    except (ValueError, TypeError):
        fin_context = []
    try:
        peer_analysis = json.loads(score.get("peer_analysis") or "{}")
    except (ValueError, TypeError):
        peer_analysis = {}
    try:
        ai_pitch = json.loads((database.get_ai_pitch(cik) or {}).get("pitch") or "{}")
    except (ValueError, TypeError):
        ai_pitch = {}
    manual_sit = database.get_manual_situation(cik)

    try:
        fraw = json.loads(fund.get("raw") or "{}")
    except (ValueError, TypeError):
        fraw = {}

    def _sec_ratio(num_key, den_key):
        n, d = fraw.get(num_key), fraw.get(den_key)
        return (n / d) if (n is not None and d not in (None, 0)) else None

    def avf(key):
        v = av.get(key)
        if v in (None, "", "None", "-", "NaN"):
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return v

    return {
        "ok": True,
        "cik": cik,
        "ticker": ticker,
        "company": score.get("company"),
        "score": score.get("score"),
        "vuln": score.get("vuln"),
        "active_situation": score.get("active_situation"),
        "situation_tier": score.get("situation_tier") or "",
        "situation_meta": situation_meta,
        "manual_situation": manual_sit.get("status") or "",
        "situation_audit": database.get_situation_audit(cik, limit=20),
        "signals": score.get("signals"),
        "evidence": evidence,
        "pitch": pitch_obj,
        "ai_pitch": ai_pitch,
        "fin_context": fin_context,
        "peer_analysis": peer_analysis,
        "first_flagged": score.get("first_flagged"),
        "market_cap": score.get("market_cap"),
        "note": database.get_note(cik),
        "history": [r["score"] for r in database.get_score_history(cik)],
        "week_change": (score.get("score") - prior) if prior is not None else None,
        "tsr": {"tsr_1y": tsr_1y, "spy_1y": spy_1y, "gap": tsr_gap},
        "governance": {
            "classified_board": bool(gov.get("classified_board")),
            "poison_pill": bool(gov.get("poison_pill")),
            "dual_class": bool(gov.get("dual_class")),
            "proxy_url": gov.get("proxy_url"),
            "proxy_date": gov.get("proxy_date"),
        },
        "insider": {
            "buy_value": ins.get("buy_value"),
            "sell_value": ins.get("sell_value"),
            "net_value": ins.get("net_value"),
            "n_buyers": ins.get("n_buyers"),
            "n_sellers": ins.get("n_sellers"),
            "last_date": ins.get("last_date"),
            "window_days": ins.get("window_days"),
            "top_url": ins.get("top_url"),
        },
        "earnings": {
            "next_date": ear.get("next_date"),
            "last_date": ear.get("last_date"),
        },
        "sentiment": {
            "mspr": fx.get("mspr"),
            "month": fx.get("mspr_month"),
        },
        "analysts": {
            "strong_buy": fx.get("rec_strongbuy"),
            "buy": fx.get("rec_buy"),
            "hold": fx.get("rec_hold"),
            "sell": fx.get("rec_sell"),
            "strong_sell": fx.get("rec_strongsell"),
            "period": fx.get("rec_period"),
        },
        "votes": {
            "say_on_pay": vot.get("say_on_pay"),
            "meeting_date": vot.get("meeting_date"),
            "url": vot.get("url"),
        },
        "activist": {
            "kind": aflag.get("kind"),
            "form": aflag.get("form"),
            "label": aflag.get("label"),
            "filed": aflag.get("filed"),
            "url": aflag.get("url"),
        },
        "overview": {
            "description": prof.get("description") or av.get("Description"),
            "sector": prof.get("sector") or av.get("Sector"),
            "industry": prof.get("industry") or av.get("Industry"),
            "exchange": av.get("Exchange"),
            "website": prof.get("website") or av.get("OfficialSite") or av.get("Website"),
        },
        "contacts": {
            "ceo": prof.get("ceo"),
            "phone": prof.get("phone"),
            "address": prof.get("address"),
            "city": prof.get("city"),
            "state": prof.get("state"),
            "zip": prof.get("zip"),
            "country": prof.get("country"),
            "employees": prof.get("employees"),
            "ipo": prof.get("ipo"),
            "website": prof.get("website"),
            "ir_name": con.get("ir_name"),
            "ir_email": con.get("ir_email"),
            "ir_phone": con.get("ir_phone"),
            "comms_name": con.get("comms_name"),
            "comms_email": con.get("comms_email"),
            "comms_phone": con.get("comms_phone"),
            "contacts_source": con.get("source_url"),
        },
        "prices": {"series": px.get("series") or [], "last_close": px.get("last_close")},
        "financials": {
            "revenue": fund.get("revenue"),
            "revenue_growth": fund.get("revenue_growth"),
            "operating_margin": fund.get("operating_margin"),
            "sga_pct": fund.get("sga_pct"),
            "roa": fund.get("roa"),
            "cash_to_assets": fund.get("cash_to_assets"),
            "debt_to_assets": fund.get("debt_to_assets"),
            "pe_ratio": avf("PERatio"),
            "pb_ratio": avf("PriceToBookRatio"),
            "profit_margin": _sec_ratio("net_income", "revenue") if _sec_ratio("net_income", "revenue") is not None else avf("ProfitMargin"),
            "dividend_yield": avf("DividendYield"),
            "week52_high": avf("52WeekHigh"),
            "week52_low": avf("52WeekLow"),
            "analyst_target": avf("AnalystTargetPrice"),
            "return_on_equity": _sec_ratio("net_income", "book_equity") if _sec_ratio("net_income", "book_equity") is not None else avf("ReturnOnEquityTTM"),
        },
        "filings": database.get_filings_by_cik(cik, limit=12),
        "news": database.get_news_for_ticker(ticker, limit=10) if ticker else [],
    }


@app.get("/api/status")
def api_status():
    return {
        "threshold": config.SCORE_THRESHOLD,
        "window_days": config.SCORE_WINDOW_DAYS,
        "refresh_minutes": config.REFRESH_MINUTES,
        "digest_hour_et": config.DIGEST_HOUR_ET,
        "universe_size": len(pipeline.get_universe()),
        "subscribers": len(database.get_subscribers()),
        "email_enabled": bool(config.EMAIL_API_KEY),
        "news_enabled": bool(config.NEWS_API_KEY),
    }


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"ok": False, "error": "Invalid email address."},
                            status_code=400)
    database.add_subscriber(email)
    return {"ok": True, "message": f"Subscribed {email} to the daily digest."}


@app.post("/api/unsubscribe")
async def api_unsubscribe(request: Request):
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    database.remove_subscriber(email)
    return {"ok": True, "message": f"Unsubscribed {email}."}


@app.post("/api/refresh")
def api_refresh():
    result = pipeline.refresh_data()
    return {"ok": True, **result}


@app.post("/api/run-enrichment")
def api_run_enrichment():
    """Kick the heavy data passes (insider, votes, activist, earnings) on demand,
    in a background thread so the request returns immediately. These otherwise only
    run on boot and in the 4 PM ET daily job."""
    def _job():
        import traceback
        for name, fn in (("insider", pipeline.refresh_insider),
                         ("votes", pipeline.refresh_votes),
                         ("governance", pipeline.refresh_governance),
                         ("activist", lambda: pipeline.refresh_activist(full=False)),
                         ("earnings", pipeline.refresh_earnings),
                         ("sentiment", pipeline.refresh_sentiment),
                         ("contacts", pipeline.refresh_contacts),
                         ("ir-contacts", pipeline.refresh_ir_contacts),
                         ("exec-reactions", pipeline.refresh_exec_reactions),
                         ("long-tsr", pipeline.refresh_long_tsr),
                         ("prices", pipeline.refresh_prices),
                         ("ai-thesis", pipeline.refresh_ai_thesis)):
            try:
                fn()
            except Exception:
                print(f"[run-enrichment] {name} failed")
                traceback.print_exc()
        print("[run-enrichment] done")
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "message": "Enrichment started — insider, votes, activist and "
            "earnings are refreshing in the background (about a minute or two). "
            "Reload the page shortly to see updates."}


@app.post("/api/sweep-activists")
def api_sweep_activists():
    """Run the FULL universe activist sweep on demand. This is the AUTHORITATIVE pass:
    it checks all ~1,489 names, ADDS newly-found Confirmed situations, and CLEARS stale
    ones (e.g. a mis-attributed filer like IAC, or a campaign that has ended). It normally
    runs only on boot and at 4 PM ET; this lets a partner force it without a redeploy.
    Runs in the background (~10 minutes) so the request returns immediately."""
    def _job():
        import traceback
        try:
            pipeline.refresh_activist(full=True)
        except Exception:
            print("[sweep-activists] failed")
            traceback.print_exc()
        print("[sweep-activists] done")
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "message": "Full SEC activist sweep started across all ~1,489 "
            "companies. This takes about 10 minutes and runs in the background — watch the "
            "logs for '[activist] swept 1489 names (full)', then reload this page. Stale "
            "flags (like a mis-attributed filer) clear and any newly-found situations appear."}


@app.api_route("/api/send-test-digest", methods=["GET", "POST"])
def api_send_test_digest():
    subs = database.get_subscribers()
    if not subs:
        return {"ok": False, "message": "No subscribers yet. Add your email on "
                "the dashboard first, then try again."}
    sent = emailer.send_digest()
    return {"ok": True, "sent": sent,
            "message": f"Digest sent to {sent} of {len(subs)} subscriber(s). "
                       f"Check your inbox (and spam folder)."}
