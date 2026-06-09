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

from . import config, database, pipeline, emailer

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
    rows = database.get_scores(limit=config.SHORTLIST_SIZE)
    for c in rows:
        prior = database.prior_score(c["cik"])
        c["week_change"] = (c["score"] - prior) if prior is not None else None
    return {"companies": rows}


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

    try:
        evidence = json.loads(score.get("evidence") or "[]")
    except (ValueError, TypeError):
        evidence = []

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
        "ticker": ticker,
        "company": score.get("company"),
        "score": score.get("score"),
        "signals": score.get("signals"),
        "evidence": evidence,
        "first_flagged": score.get("first_flagged"),
        "market_cap": score.get("market_cap"),
        "week_change": (score.get("score") - prior) if prior is not None else None,
        "overview": {
            "description": av.get("Description"),
            "sector": av.get("Sector"),
            "industry": av.get("Industry"),
            "exchange": av.get("Exchange"),
            "website": av.get("OfficialSite") or av.get("Website"),
        },
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
