"""
FastAPI application: serves the dashboard, the JSON API, the subscribe endpoint,
and runs the background scheduler (30-min refresh + 4pm-ET digest).

Run locally:   uvicorn app.main:app --reload
In production:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database, pipeline, emailer

STATIC_DIR = Path(__file__).resolve().parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def _run_initial_refresh():
    """Kick off a first full pull (filings, news, market data, scores) in the
    background so the page populates within minutes of a deploy."""
    try:
        pipeline.startup_full_refresh()
    except Exception as e:  # pragma: no cover
        print(f"[startup] initial refresh failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    pipeline.get_universe()

    # 30-minute data refresh (EDGAR + news + rescore)
    scheduler.add_job(
        pipeline.refresh_data,
        IntervalTrigger(minutes=config.REFRESH_MINUTES),
        id="refresh", replace_existing=True, max_instances=1,
    )
    # Daily 4pm ET: refresh market data, rescore, send digest
    scheduler.add_job(
        pipeline.daily_rescore_and_digest,
        CronTrigger(hour=config.DIGEST_HOUR_ET, minute=0,
                    timezone=config.TIMEZONE),
        id="digest", replace_existing=True, max_instances=1,
    )
    scheduler.start()
    # Non-blocking first refresh so the server starts immediately.
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
    """Live intelligence feed: recent news (left) + recent filings (right)."""
    return {
        "news": database.recent_news(limit=25),
        "filings": database.recent_filings(limit=25),
    }


@app.get("/api/shortlist")
def api_shortlist():
    """Ranked 'Companies to Pitch'."""
    return {"companies": database.get_scores(limit=config.SHORTLIST_SIZE)}


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
    """Manual trigger (button on the dashboard) for an immediate data pull."""
    result = pipeline.refresh_data()
    return {"ok": True, **result}


@app.api_route("/api/send-test-digest", methods=["GET", "POST"])
def api_send_test_digest():
    """Send the digest right now to all subscribers (for testing the email).
    Accepts GET too so it can be triggered from a browser address bar."""
    subs = database.get_subscribers()
    if not subs:
        return {"ok": False,
                "message": "No subscribers yet. Add your email on the "
                           "dashboard first, then try again."}
    sent = emailer.send_digest()
    return {"ok": True, "sent": sent,
            "message": f"Digest sent to {sent} of {len(subs)} subscriber(s). "
                       f"Check your inbox (and spam folder)."}
