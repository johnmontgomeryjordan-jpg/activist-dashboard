"""
FastAPI app: serves the dashboard, JSON API (incl. per-company detail + CSV
export), the subscribe endpoint, and runs the background scheduler.
"""
import base64
import json
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database, pipeline, emailer, scoring, spotlight, universe, thirteenf, credibility

STATIC_DIR = Path(__file__).resolve().parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def _run_initial_refresh():
    try:
        pipeline.startup_full_refresh()
    except Exception as e:  # pragma: no cover
        print(f"[startup] initial refresh failed: {e}")


def _run_initial_13f():
    """Populate the 13F activist-holder map on boot if it's empty. Runs in its OWN thread —
    NOT gated behind the ~20-min startup_full_refresh — because everything it reads (cusip_map,
    companies, fundamentals) is already on the persistent disk from prior runs. This guarantees
    it actually executes even when frequent redeploys keep restarting the long startup job. A
    short delay lets the app settle first; the per-fund guard in thirteenf keeps a bad fund from
    aborting the sweep."""
    import time
    try:
        time.sleep(20)
        if not config.THIRTEENF_ENABLED or database.activist_holdings_count() > 0:
            return
        if thirteenf.refresh_13f() > 0:
            scoring.recompute_all()
    except Exception as e:  # pragma: no cover
        print(f"[startup] 13F populate failed: {e}")


def _daily_rescore_then_audit():
    """Daily rebuild + digest, then the Credibility Watch self-audit. The audit reads the DB
    only and is fully isolated in its own try/except so it can NEVER affect the digest — it just
    prints a one-line summary (and any HIGH/MED flags) to the deploy log. Log-only for now; email
    to John (only) can be wired later. Runs after the rescore so it sees the fresh board."""
    pipeline.daily_rescore_and_digest()
    try:
        credibility.run()
    except Exception as e:  # pragma: no cover
        print(f"[credibility] self-audit failed (non-fatal): {e}", flush=True)


def _now_et():
    """Current time in the scheduler's timezone (ET), whether config.TIMEZONE is a tzinfo or str."""
    from datetime import datetime
    tz = config.TIMEZONE
    try:
        return datetime.now(tz)
    except TypeError:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(str(tz)))


def _is_report_cycle():
    """True when today (ET) is on the 14-day cadence anchored at REPORT_ANCHOR — so the first issue
    lands exactly on the anchor date and every two weeks after, independent of ISO-week parity."""
    from datetime import date
    try:
        anchor = date.fromisoformat(config.REPORT_ANCHOR)
    except Exception:
        return True                       # misconfigured anchor: don't silently skip forever
    d = _now_et().date()
    return d >= anchor and (d - anchor).days % 14 == 0


def _report_generate():
    """Tuesday 7 PM ET (biweekly cadence): generate the rotated no-repeat issue, auto-audit it,
    and freeze it for the morning send. Off-cycle Tuesdays are a no-op. Never sends."""
    if not _is_report_cycle():
        return
    try:
        emailer.generate_issue()
    except Exception as e:  # pragma: no cover
        print(f"[report] generate failed (non-fatal): {e}", flush=True)


def _report_send():
    """Wednesday 7 AM ET: send last night's frozen issue if the audit was clean, else hold + alert.
    send_pending_issue() only acts on a fresh pending issue, so off-cycle Wednesdays no-op."""
    try:
        emailer.send_pending_issue()
    except Exception as e:  # pragma: no cover
        print(f"[report] send failed (non-fatal): {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    pipeline.get_universe()
    scheduler.add_job(pipeline.refresh_data,
                      IntervalTrigger(minutes=config.REFRESH_MINUTES),
                      id="refresh", replace_existing=True, max_instances=1)
    scheduler.add_job(_daily_rescore_then_audit,
                      CronTrigger(hour=config.DIGEST_HOUR_ET, minute=0,
                                  timezone=config.TIMEZONE),
                      id="digest", replace_existing=True, max_instances=1)
    # Biweekly report — two phases, anchored to REPORT_ANCHOR (every 14 days):
    #   GENERATE + auto-audit  — Tue REPORT_GEN_HOUR_ET (7 PM ET): assemble the rotated no-repeat
    #                            issue, audit it, freeze it for the morning send.
    #   SEND to the list       — Wed REPORT_SEND_HOUR_ET (7 AM ET): ship it if the audit was clean,
    #                            else hold + alert the admin.
    # Both jobs run WEEKLY; generate self-gates to the 14-day cadence and send only ships a fresh
    # pending issue, so off-cycle weeks are harmless no-ops.
    if config.REPORT_ENABLED:
        scheduler.add_job(_report_generate,
                          CronTrigger(day_of_week=config.REPORT_GEN_DAY,
                                      hour=config.REPORT_GEN_HOUR_ET, minute=0,
                                      timezone=config.TIMEZONE),
                          id="report_generate", replace_existing=True, max_instances=1)
        scheduler.add_job(_report_send,
                          CronTrigger(day_of_week=config.REPORT_SEND_DAY,
                                      hour=config.REPORT_SEND_HOUR_ET, minute=0,
                                      timezone=config.TIMEZONE),
                          id="report_send", replace_existing=True, max_instances=1)
    # Weekly (Sun 05:00): refresh S&P 1500 membership from iShares. Fails safe --
    # keeps the committed universe.csv on any fetch failure or sanity-gate rejection.
    scheduler.add_job(universe.rebuild_universe_csv,
                      CronTrigger(day_of_week="sun", hour=5, minute=0,
                                  timezone=config.TIMEZONE),
                      id="universe_rebuild", replace_existing=True,
                      max_instances=1)
    # Weekly (Sun 05:30): entity master (sector/market cap/exchange/index tags).
    # Freshness-gated so it's a cheap no-op most weeks; also fills on the boot refresh.
    scheduler.add_job(pipeline.refresh_entity_master,
                      CronTrigger(day_of_week="sun", hour=5, minute=30,
                                  timezone=config.TIMEZONE),
                      id="entity_master", replace_existing=True,
                      max_instances=1)
    # Monthly (1st, 06:00): rebuild the free CUSIP->ticker map from SEC Fails-to-Deliver.
    scheduler.add_job(pipeline.refresh_cusip_map,
                      CronTrigger(day=1, hour=6, minute=0, timezone=config.TIMEZONE),
                      id="cusip_map", replace_existing=True, max_instances=1)
    # Weekly (Sun 06:00): refresh the 13F activist-holder map. 13F is quarterly + lagged ~45
    # days, so weekly is ample; runs after universe/entity so the cusip_map + universe are set.
    if config.THIRTEENF_ENABLED:
        scheduler.add_job(thirteenf.refresh_13f,
                          CronTrigger(day_of_week="sun", hour=6, minute=0,
                                      timezone=config.TIMEZONE),
                          id="thirteenf", replace_existing=True, max_instances=1)
    scheduler.start()
    threading.Thread(target=_run_initial_refresh, daemon=True).start()
    threading.Thread(target=_run_initial_13f, daemon=True).start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Activist Vulnerability Dashboard", lifespan=lifespan)


@app.middleware("http")
async def _gate_and_secure(request: Request, call_next):
    """Internal-tool access control: if SITE_PASSWORD is set, the whole site requires a
    shared firm login (HTTP Basic). /healthz stays open so the host can probe the port.
    Also stamps a few conservative security headers on every response."""
    if request.url.path == "/healthz":
        return JSONResponse({"ok": True})
    pw = config.SITE_PASSWORD
    if pw:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8", "ignore")
                u, _, p = raw.partition(":")
                ok = (secrets.compare_digest(u, config.SITE_USER)
                      and secrets.compare_digest(p, pw))
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="FGS Activist Dashboard"'})
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # SAMEORIGIN (not DENY) so the landing page can embed the /report page in an iframe; still
    # blocks external sites from framing the tool (clickjacking).
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"   # keep it out of search engines
    return resp


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# The report is regenerated from live data on every request, so it must NEVER be cached. Without
# these headers the browser happily reuses the landing-page iframe, which silently showed a stale
# issue after a deploy (fixes appeared to "not deploy" when they were actually live). A stale
# report in front of a partner is a real risk — always serve fresh.
_REPORT_NOCACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/report", response_class=HTMLResponse)
def report_page():
    """The biweekly Activist Vulnerability report as a standalone page — rendered in the app's
    Brief palette, embedded on the landing tab via an iframe, and emailed on the biweekly cron."""
    try:
        body = emailer.build_report_html()
    except Exception as e:  # pragma: no cover
        return HTMLResponse(f"<p style='font-family:sans-serif;padding:24px'>Report temporarily "
                            f"unavailable: {e}</p>", status_code=500, headers=_REPORT_NOCACHE)
    return HTMLResponse(body, headers=_REPORT_NOCACHE)


@app.get("/api/report/preview", response_class=HTMLResponse)
def report_preview():
    """Identical HTML to /report — a distinct URL for the night-before preview."""
    return report_page()


@app.get("/api/report/latest", response_class=HTMLResponse)
def report_latest():
    """Preview the most recently GENERATED biweekly issue — the frozen, audited email body the Wed
    7 AM job will actually send (the rotated no-repeat 5), not the live top-5 that /report shows.
    Read-only QA view: prepends a banner with the audit verdict + send status so the Tuesday-night
    review shows precisely what's going out (or why it's held)."""
    issue = database.get_latest_issue()
    if not issue or not issue.get("html"):
        return HTMLResponse("<p style='font-family:system-ui;padding:24px'>No issue generated yet "
                            "— the first one is built Tue 7 PM ET.</p>",
                            status_code=404, headers=_REPORT_NOCACHE)

    def _e(s):
        return (str(s if s is not None else "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    flags = []
    try:
        import json as _json
        flags = _json.loads(issue.get("audit_flags") or "[]")
    except Exception:
        flags = []
    status = (issue.get("audit_status") or "?").lower()
    sent = issue.get("sent_at")

    def _btn(href, label, bg):
        return (f"<a href='{href}' style='display:inline-block;margin-left:8px;padding:4px 12px;"
                f"border-radius:6px;text-decoration:none;background:{bg};color:#fff;font-weight:600'>"
                f"{label}</a>")

    actions, color = "", "#137333"
    if sent:
        state, color = f"already sent {_e(sent)}", "#5f6368"
    elif status == "approved":
        state = "APPROVED — will send at the next 7 AM ET window"
    elif status == "clean":                       # passed the audit, awaiting the owner's approval
        state = "audit CLEAN — awaiting your approval"
        actions = (_btn("/api/report/approve", "Approve &amp; send", "#137333")
                   + _btn("/api/report/hold", "Hold", "#b3261e"))
    elif status == "held":                         # audit HELD it
        state, color = "HELD by the audit — will NOT send (admin alerted)", "#b3261e"
        actions = _btn("/api/report/approve?override=1", "Approve anyway", "#b3261e")
    elif status == "held_user":                    # owner declined
        state, color = "held by you — will NOT send", "#b3261e"
        actions = _btn("/api/report/approve?override=1", "Re-approve &amp; send", "#137333")
    else:
        state, color = f"status {_e(status)}", "#b3261e"

    flag_html = "".join(
        f"<li><b>[{_e(f.get('severity'))}]</b> {_e(f.get('check'))} — {_e(f.get('subject'))}: "
        f"{_e(f.get('detail'))}</li>" for f in flags)
    banner = (
        f"<div style='font-family:system-ui;font-size:14px;padding:12px 20px;background:#f6f4ee;"
        f"border-bottom:3px solid {color};color:#1c1b18'>"
        f"<b>Issue {_e(issue.get('issue_id'))}</b> &middot; {_e(issue.get('tickers'))} &middot; "
        f"audit <b style='color:{color}'>{_e(status.upper())}</b> &middot; {_e(state)}{actions}"
        + (f"<ul style='margin:8px 0 0;padding-left:20px'>{flag_html}</ul>" if flags else "")
        + "</div>")
    return HTMLResponse(banner + issue["html"], headers=_REPORT_NOCACHE)


@app.api_route("/api/report/approve", methods=["GET", "POST"])
def report_approve(override: int = 0):
    """Owner approval gate. Releases the pending issue to the next 7 AM ET send window. A clean
    (audit-passed) issue approves directly; an audit-HELD issue requires ?override=1 after you've
    reviewed the flags. Nothing reaches the distribution list until this is called."""
    issue = database.get_pending_issue()
    if not issue:
        return {"ok": False, "message": "No pending issue to approve."}
    issue_id, tickers = issue.get("issue_id"), issue.get("tickers")
    st = (issue.get("audit_status") or "").lower()
    if st == "approved":
        return {"ok": True, "issue": issue_id,
                "message": "Already approved — it will send at the next 7 AM ET window."}
    if st == "held" and not override:
        import json as _json
        try:
            flags = _json.loads(issue.get("audit_flags") or "[]")
        except Exception:
            flags = []
        return {"ok": False, "held": True, "issue": issue_id, "flags": flags,
                "message": "This issue was HELD by the auto-audit. Review the flags, then approve "
                           "anyway with ?override=1 to send it."}
    database.set_issue_status(issue_id, "approved")
    return {"ok": True, "issue": issue_id, "tickers": tickers,
            "message": f"Approved. Issue {issue_id} ({tickers}) will send to the distribution list "
                       f"at the next 7 AM ET send window."}


@app.api_route("/api/report/hold", methods=["GET", "POST"])
def report_hold():
    """Owner hold: block the pending issue so the Wednesday send skips it. Reversible — re-approve
    with /api/report/approve?override=1."""
    issue = database.get_pending_issue()
    if not issue:
        return {"ok": False, "message": "No pending issue to hold."}
    issue_id = issue.get("issue_id")
    database.set_issue_status(issue_id, "held_user")
    return {"ok": True, "issue": issue_id,
            "message": f"Held. Issue {issue_id} will NOT be sent. Re-approve with "
                       f"/api/report/approve?override=1 if you change your mind."}


_REGEN = {"running": False, "started_at": None, "done_at": None, "result": None, "error": None}


@app.api_route("/api/report/generate-now", methods=["GET", "POST"])
def report_generate_now(confirm: int = 0, recompute: int = 1, tickers: str = ""):
    """Owner MANUAL regenerate. Recomputes the data behind the report so code/data fixes take effect,
    then re-renders the pending issue IN PLACE (same vetted board, corrected numbers/theses), re-audits
    and re-freezes it awaiting approval. Never sends.

        /api/report/generate-now                     -> shows what it will do (no action)
        /api/report/generate-now?confirm=1           -> recompute + regenerate the SAME board
        /api/report/generate-now?confirm=1&recompute=0   -> re-render only (skip the slow recompute)
        /api/report/generate-now?confirm=1&tickers=INSP,BLDR,SSTK,VITL,ACI  -> pin an explicit board

    Runs in the background (the recompute touches the full universe and takes several minutes). Poll
    /api/report/latest — when it reappears 'awaiting your approval', review it and /api/report/approve."""
    if _REGEN["running"]:
        return {"ok": False, "running": True, "started_at": _REGEN["started_at"],
                "message": "A regenerate is already in progress. Poll /api/report/latest."}
    pending = database.get_pending_issue()
    if not confirm:
        cur = (pending or {}).get("tickers") if pending else None
        return {"ok": False, "confirm_required": True, "current_board": cur,
                "recompute": bool(recompute),
                "message": ("Add ?confirm=1 to recompute the data and regenerate this issue in place "
                            "(same board, corrected numbers). Add &recompute=0 to re-render only. "
                            "The result waits for /api/report/approve — nothing is sent.")}
    explicit = [t.strip().upper() for t in tickers.split(",") if t.strip()] or None

    def _work():
        from datetime import datetime
        _REGEN.update(running=True, started_at=datetime.utcnow().isoformat(),
                      done_at=None, result=None, error=None)
        try:
            if recompute:
                print("[report] generate-now: recompute starting", flush=True)
                pipeline.refresh_fundamentals()   # re-extract debt (leases) + metrics
                try:
                    pipeline.refresh_votes()      # say-on-pay support (gates the overpaid-CEO signal)
                except Exception as _ve:
                    print(f"[report] generate-now: refresh_votes failed (non-fatal): {_ve}", flush=True)
                scoring.recompute_all()           # rescore with the corrected pay/insider signals
                pipeline.refresh_ai_thesis()      # re-voice pitches (drops stale theses, archetypes)
                print("[report] generate-now: recompute done", flush=True)
            cleared = database.clear_pending_issues()   # drop the stale draft; returns its board
            pin = explicit or cleared or None
            res = emailer.generate_issue(pin=pin)
            _REGEN.update(result={"status": res.get("status"), "summary": res.get("summary"),
                                  "pin": pin})
            print(f"[report] generate-now complete: pin={pin} · {res.get('summary')}", flush=True)
        except Exception as e:  # pragma: no cover
            _REGEN.update(error=str(e))
            print(f"[report] generate-now FAILED: {e}", flush=True)
        finally:
            _REGEN.update(running=False, done_at=datetime.utcnow().isoformat())

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "started": True, "recompute": bool(recompute),
            "pin": explicit or ((pending or {}).get("tickers") if pending else None),
            "message": ("Regeneration started in the background"
                        + (" (recompute + re-render)" if recompute else " (re-render only)")
                        + ". Poll /api/report/generate-now/status or /api/report/latest; the refreshed "
                          "issue will show 'awaiting your approval'.")}


@app.get("/api/report/generate-now/status")
def report_generate_now_status():
    """Progress of the last manual regenerate (see /api/report/generate-now)."""
    return {"ok": True, **_REGEN}


@app.api_route("/api/report/send-test", methods=["GET", "POST"])
def report_send_test(to: str = "", confirm: int = 0):
    """Send the biweekly report. GUARDED — a bare call is a genuine test and reaches exactly ONE
    inbox, never the distribution list:

        /api/report/send-test                  -> first recipient only, subject tagged [TEST]
        /api/report/send-test?to=you@fgs.com   -> that one address, subject tagged [TEST]
        /api/report/send-test?confirm=1        -> THE REAL SEND: every recipient, untagged

    This endpoint previously called send_report() directly, so a click meant as a dry run mailed
    the whole live list (an issue went to five people that way). The default is now the safe path
    and mailing everyone requires typing confirm=1, which is hard to do by accident."""
    recipients = config.REPORT_RECIPIENTS or database.get_subscribers()
    if not recipients and not to:
        return {"ok": False, "sent": 0, "test": True,
                "message": "No recipients configured. Add an email on the dashboard, set "
                           "REPORT_RECIPIENTS in Render, or call this with ?to=you@example.com."}
    if confirm:
        sent = emailer.send_report()
        return {"ok": bool(sent), "sent": sent, "test": False,
                "recipients": len(recipients),
                "message": f"REAL SEND — biweekly report sent to {sent} recipient(s)." if sent
                           else "Real send requested but nothing went out; check the email provider config."}
    target = (to or "").strip() or recipients[0]
    ok = emailer.send_test_report(target)
    return {"ok": ok, "sent": 1 if ok else 0, "test": True, "recipient": target,
            "held_back": max(len(recipients) - 1, 0),
            "message": (f"TEST send to {target} only (subject tagged [TEST]). "
                        f"{max(len(recipients) - 1, 0)} other recipient(s) were NOT mailed. "
                        f"Add ?confirm=1 to send the real issue to everyone.") if ok
                       else f"Test send to {target} failed — check the email provider config and logs."}


@app.get("/api/feed")
def api_feed():
    # `news` is the full feed (drives the Live feed page); `news_top` is the
    # relevance-curated subset (drives the pitch-kit + dashboard Top-headlines panels,
    # so routine per-company items don't leak into them).
    return {"news": database.recent_news(limit=25),
            "news_top": database.recent_news(limit=12, relevant_only=True),
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


def _gated_company_news(ticker, name, limit=10):
    """Per-company news, filtered to items that actually name the company (reusing the
    scoring name-matcher), so a ticker's Finnhub feed can't leak competitor/unrelated
    roundups — e.g. an 'American Electric Power' or 'WaterBridge' headline surfacing under
    Hawaiian Electric. Falls back to the ungated feed only when NOTHING name-matches, so a
    thinly-covered name still shows something rather than an empty panel."""
    if not ticker:
        return []
    rows = database.get_news_for_ticker(ticker, limit=limit * 3)
    _, keys = scoring._company_keys(name or "")
    gated = [n for n in rows if scoring._headline_about_company(n.get("headline"), keys)] if keys else []
    return (gated if gated else rows)[:limit]


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
    adv = database.get_advisors(cik) or {}
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
    tsr_3y = _f(market.get("tsr_3y"))
    spy_3y = _f(database.get_meta("spy_3y"))
    gap_3y = (tsr_3y - spy_3y) if (tsr_3y is not None and spy_3y is not None) else None
    tsr_5y = _f(market.get("tsr_5y"))
    spy_5y = _f(database.get_meta("spy_5y"))
    gap_5y = (tsr_5y - spy_5y) if (tsr_5y is not None and spy_5y is not None) else None

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
        "tsr": {"tsr_1y": tsr_1y, "spy_1y": spy_1y, "gap": tsr_gap,
                "tsr_3y": tsr_3y, "spy_3y": spy_3y, "gap_3y": gap_3y,
                "tsr_5y": tsr_5y, "spy_5y": spy_5y, "gap_5y": gap_5y},
        "governance": {
            "classified_board": bool(gov.get("classified_board")),
            "poison_pill": bool(gov.get("poison_pill")),
            "dual_class": bool(gov.get("dual_class")),
            "proxy_url": gov.get("proxy_url"),
            "proxy_date": gov.get("proxy_date"),
            "annual_meeting_date": gov.get("meeting_date"),
            "nom_min_days": gov.get("nom_min_days"),
            "nom_max_days": gov.get("nom_max_days"),
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
        "advisors": {
            "firms": json.loads(adv.get("advisors_json") or "[]"),
            "source_url": adv.get("source_url"),
            "source_date": adv.get("source_date"),
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
            "profit_margin": _sec_ratio("net_income_ann", "revenue") if _sec_ratio("net_income_ann", "revenue") is not None else avf("ProfitMargin"),
            "dividend_yield": avf("DividendYield"),
            "week52_high": avf("52WeekHigh"),
            "week52_low": avf("52WeekLow"),
            "analyst_target": avf("AnalystTargetPrice"),
            "return_on_equity": _sec_ratio("net_income_ann", "book_equity") if _sec_ratio("net_income_ann", "book_equity") is not None else avf("ReturnOnEquityTTM"),
        },
        "filings": database.get_filings_by_cik(cik, limit=12),
        "news": _gated_company_news(ticker, score.get("company")),
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


@app.get("/api/lead")
def api_lead():
    """Backend-chosen lead of the day (locked per day, no-repeat rotation). The site and
    the emailed pitch kit both read this, so they always feature the same name."""
    rows = database.get_scores(limit=80)
    lead = spotlight.todays_lead(rows, database)
    return {"cik": (lead.get("cik") if lead else None)}


@app.get("/api/feedback")
def api_feedback_list():
    return {"feedback": database.get_feedback(limit=50)}


@app.post("/api/feedback")
async def api_feedback_add(request: Request):
    data = await request.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return JSONResponse({"ok": False, "error": "Message is empty."}, status_code=400)
    database.add_feedback(data.get("name") or "", msg)
    return {"ok": True, "message": "Thanks — your note was saved."}


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
                         ("advisors", pipeline.refresh_advisors),
                         ("exec-reactions", pipeline.refresh_exec_reactions),
                         ("long-tsr", lambda: pipeline.refresh_long_tsr(force=True)),
                         ("prices", pipeline.refresh_prices),
                         ("lead-data", pipeline.refresh_lead_data),
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
def api_send_test_digest(to: str = "", confirm: int = 0):
    """Send the daily digest. GUARDED the same way as /api/report/send-test, because this is the
    same shape of hazard: send_digest() mails the entire subscriber list the moment the URL is
    opened. The daily digest cron is PAUSED (pipeline.daily_rescore_and_digest returns 0 without
    sending), so this endpoint is now the ONLY way a digest can go out — which makes an accidental
    click here mail everyone with no other safety net.

        /api/send-test-digest                 -> first subscriber only (a real test)
        /api/send-test-digest?to=you@x.com    -> that one address
        /api/send-test-digest?confirm=1       -> the whole subscriber list

    Note the digest has no per-recipient subject line to tag, so the guard is purely about WHO
    receives it, not about labelling; the default reaches exactly one inbox."""
    subs = database.get_subscribers()
    if not subs and not to:
        return {"ok": False, "sent": 0, "test": True,
                "message": "No subscribers yet. Add your email on the dashboard first, or call "
                           "this with ?to=you@example.com to preview."}
    if confirm:
        sent = emailer.send_digest()
        return {"ok": bool(sent), "sent": sent, "test": False, "recipients": len(subs),
                "message": f"REAL SEND — digest sent to {sent} of {len(subs)} subscriber(s)."}
    target = (to or "").strip() or subs[0]
    ok = emailer.send_test_digest(target)
    return {"ok": ok, "sent": 1 if ok else 0, "test": True, "recipient": target,
            "held_back": max(len(subs) - 1, 0),
            "message": (f"TEST digest to {target} only. {max(len(subs) - 1, 0)} other "
                        f"subscriber(s) were NOT mailed. Add ?confirm=1 to send to everyone.") if ok
                       else f"Test digest to {target} failed — check the email provider config."}


@app.get("/api/thirteenf/stats")
def api_thirteenf_stats(ticker: str = ""):
    """Debug view of the 13F activist-holder map: how many funds resolved, how many universe
    holdings mapped, when it last ran, and (optionally) the holders for one ticker."""
    out = {
        "filers_resolved": len(database.get_activist_filers()),
        "holdings_mapped": database.activist_holdings_count(),
        "last_run": database.get_meta("thirteenf_last_run"),
    }
    if ticker:
        out["ticker"] = ticker.upper()
        out["holders"] = database.holders_for_ticker(ticker)
    return out


@app.get("/api/accumulating")
def api_accumulating():
    """STEALTH accumulators: a known activist holds a material stake (<5%, so no 13D yet) but
    hasn't agitated. The true early-warning tier. (>=5% disclosed holders move to the Active
    Situations 'Disclosed holder' tier via /api/disclosed-holders.)"""
    rows = [r for r in database.accumulating() if not r.get("disclosed")]
    return {"companies": rows, "count": len(rows)}


@app.get("/api/disclosed-holders")
def api_disclosed_holders():
    """DISCLOSED holders: a known activist owns >=5% (a Schedule 13D/13G is on file — already
    public) but the name isn't a CURRENT active situation (no 13D/proxy in the last 18 months).
    Shown on the Active Situations page as its own tier — a known holder, campaign not active."""
    rows = [r for r in database.accumulating() if r.get("disclosed")]
    return {"companies": rows, "count": len(rows)}


@app.post("/api/refresh-13f")
def api_refresh_13f():
    """Force a full 13F refresh (resolve funds -> pull latest info tables -> map holdings),
    then recompute scores so any activist-holder signal appears. Runs in the background
    (~1-2 minutes) so the request returns immediately."""
    def _job():
        import traceback
        try:
            n = thirteenf.refresh_13f()
            scoring.recompute_all()
            print(f"[refresh-13f] done; {n} holdings mapped")
        except Exception:
            print("[refresh-13f] failed")
            traceback.print_exc()
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "message": "13F activist-holder refresh started across the activist "
            "fund list. Takes ~1-2 minutes; watch the logs for '[13f] resolved … funds', then "
            "reload. Any name where an activist is already a holder gets a boost + evidence card."}
