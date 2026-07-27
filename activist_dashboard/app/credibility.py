"""
Credibility watch — a nightly self-audit that re-checks the live board and Active Situations
against a set of DETERMINISTIC rules encoding the accuracy audit's failure classes, so quality
can't silently drift as the data refreshes. It reads the database only (no external calls) and
returns a short digest: "clean", or a severity-ranked list of flags to eyeball.

Each check maps to something the July 2026 audit found and we fixed — the watch exists so that if
any of them ever regress (a stale tag, a bad tier, an AI over-claim), it surfaces the same day
instead of in front of a partner.

Wire-up: call credibility.run() from the daily rebuild; it prints a one-line summary to the deploy
log every run, and returns {'flags','digest','counts'} so a caller can email the digest.
"""
import json
from datetime import datetime

from . import database

STALE_CAMPAIGN_DAYS = 548          # 18-month agitation gate — a Confirmed situation older is stale
VULN_CAP = 92                      # scoring soft-cap ceiling; nothing should exceed it

# Words in a thesis that assert a STOCK-return claim (not a period label like "trailing 12 months").
_UNDERPERF = ("underperform", "has fallen", "stock is down", "stock has fallen",
              "trailing the s&p", "trailing the market", "lagged the market",
              "lagging the market", "multi-year underperform", "structural underperform",
              "has trailed the", "sharp de-rating")
# If the thesis claims underperformance, at least one of these return-lag signals must have fired.
_RETURN_SIGNALS = {"weak_tsr_1y", "weak_tsr_3y", "strategic_review", "lags_own_peers"}


def _loads(s, default):
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s) if s else default
    except (ValueError, TypeError):
        return default


def _days_ago(date_str):
    try:
        return (datetime.utcnow() - datetime.strptime(str(date_str)[:10], "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _all_scores():
    board = database.get_scores(limit=10000)               # active_situation = 0 (pitch leads)
    sit = database.get_active_situations(limit=10000)       # active_situation = 1
    return board, sit


def run_checks():
    """Return a list of {severity, check, ticker, detail} anomalies. Empty list = clean."""
    board, sit = _all_scores()
    try:
        flags_by_cik = database.get_all_activist_flags() or {}   # {cik: {kind,form,label,filed,url}}
    except Exception:
        flags_by_cik = {}
    # Raw fundamentals per CIK — used to tell a GENUINELY debt-free name (no interest expense on the
    # P&L) from a MISSED/stale debt tag (debt reads 0 but the income statement still shows interest).
    try:
        _raw_by_cik = {f.get("cik"): _loads(f.get("raw"), {})
                       for f in database.get_all_fundamentals()}
    except Exception:
        _raw_by_cik = {}
    cik_to_tk = {r.get("cik"): (r.get("ticker") or r.get("company") or r.get("cik"))
                 for r in board + sit}

    out = []

    def add(sev, check, ticker, detail):
        out.append({"severity": sev, "check": check, "ticker": ticker, "detail": detail})

    for r in board + sit:
        tk = r.get("ticker") or r.get("company") or r.get("cik")
        ev = _loads(r.get("evidence"), [])
        keys = {e.get("key") for e in ev if isinstance(e, dict)}
        sigstr = str(r.get("signals") or "")
        pitch = _loads(r.get("pitch"), {})
        thesis = ((pitch.get("thesis") or "") + " "
                  + " ".join(pitch.get("points") or [])).lower()
        fin = _loads(r.get("fin_context"), [])
        on_board = not r.get("active_situation")

        # #156 — the 13F activist-holder signal must never be back in the score.
        if "activist_holder" in keys or "activist_holder" in sigstr:
            add("HIGH", "activist_holder leak", tk,
                "13F activist-holder signal is back in the score (violates #156)")

        # Soft-cap sanity.
        if (r.get("vuln") or 0) > VULN_CAP:
            add("LOW", "vuln over cap", tk, f"vuln {r.get('vuln')} exceeds the {VULN_CAP} ceiling")

        # A pitch lead with no market cap is data-incomplete (its valuation signals can't be trusted).
        if on_board and not r.get("market_cap"):
            add("LOW", "missing market cap", tk, "pitch lead has no market cap")

        # Insider window must be the 12-month net (#2) — catch a 120-day straggler.
        for e in ev:
            if isinstance(e, dict) and e.get("key") in ("insider_selling", "insider_buying"):
                per = (e.get("period") or "").lower()
                if per and "12 month" not in per and "365" not in per:
                    add("LOW", "insider window stale", tk,
                        f"insider period '{e.get('period')}' — should be trailing 12 months")

        # ServiceNow class — flagged "under-levered" opportunity with $0 debt. Only a real problem
        # when it's a MISSED tag, not a genuinely debt-free balance sheet. The discriminator is
        # interest expense: a debt-free company reports ~none; a missed/stale debt tag still shows a
        # material borrowing cost on the income statement. Gate on that so debt-free names (TTD,
        # STAA, SONO, SAM, RHI, INSP …) stop tripping a false "missed tag" alarm every run.
        for c in fin:
            if isinstance(c, dict) and c.get("key") == "debt_to_assets" and c.get("verdict") == "opp":
                v = c.get("value")
                if v is not None and v <= 0:
                    _raw = _raw_by_cik.get(r.get("cik")) or {}
                    ie, ta = _raw.get("interest_expense"), _raw.get("total_assets")
                    # material = real borrowing cost the $0-debt reading can't explain
                    material = ie is not None and ie > 0 and (not ta or ie >= 0.003 * ta)
                    if material:
                        add("MED", "under-levered with $0 debt", tk,
                            f"debt/assets is 0 but the P&L shows interest expense of "
                            f"${ie / 1e6:.1f}M — likely a missed/stale debt tag")

        # Intel class — thesis asserts stock underperformance but no return-lag signal fired.
        if on_board and thesis and any(w in thesis for w in _UNDERPERF) \
                and not (keys & _RETURN_SIGNALS):
            add("HIGH", "thesis vs returns", tk,
                "thesis claims stock underperformance but no return-lag signal fired (AI over-claim)")

    # Tiering — a name being pitched should not also carry an activist filing (acquired / settled /
    # confirmed names belong in Active Situations, not on the pitch board).
    for r in board:
        cik = r.get("cik")
        if cik in flags_by_cik:
            f = flags_by_cik[cik]
            add("MED", "pitched name has activist flag", r.get("ticker") or cik,
                f"on the pitch board but has an activist filing ({f.get('kind')}) — should be an Active Situation")

    # Staleness — a Confirmed (13D / contested proxy) situation older than the 18-month gate.
    for cik, f in flags_by_cik.items():
        if f.get("kind") in ("13d", "proxy"):
            d = _days_ago(f.get("filed"))
            if d is not None and d > STALE_CAMPAIGN_DAYS:
                add("MED", "stale confirmed campaign", cik_to_tk.get(cik, cik),
                    f"Confirmed situation filed {d} days ago (> {STALE_CAMPAIGN_DAYS}) — should have aged off")

    sev_rank = {"HIGH": 0, "MED": 1, "LOW": 2}
    out.sort(key=lambda x: (sev_rank.get(x["severity"], 9), x["check"], str(x["ticker"])))
    return out


def render_digest(flags):
    """Short human digest — for the log line and the (optional) email body."""
    if not flags:
        return "Credibility watch — CLEAN. No anomalies across the board and Active Situations."
    counts = {s: sum(1 for f in flags if f["severity"] == s) for s in ("HIGH", "MED", "LOW")}
    lines = [f"Credibility watch — {len(flags)} flag(s): "
             f"{counts['HIGH']} HIGH, {counts['MED']} MED, {counts['LOW']} LOW.", ""]
    for f in flags:
        lines.append(f"[{f['severity']}] {f['ticker']} — {f['check']}: {f['detail']}")
    return "\n".join(lines)


def run(verbose=True):
    """Run the watch. Prints a one-line summary to the log (visible each rebuild) and returns
    {'flags', 'digest', 'counts'} so a caller (main cron) can email the digest."""
    flags = run_checks()
    digest = render_digest(flags)
    counts = {s: sum(1 for f in flags if f["severity"] == s) for s in ("HIGH", "MED", "LOW")}
    if verbose:
        summary = ("clean" if not flags
                   else f"{len(flags)} flags ({counts['HIGH']}H/{counts['MED']}M/{counts['LOW']}L)")
        print(f"[credibility] self-audit: {summary}", flush=True)
        for f in flags:
            if f["severity"] in ("HIGH", "MED"):
                print(f"[credibility]   [{f['severity']}] {f['ticker']} — {f['check']}: {f['detail']}",
                      flush=True)
    return {"flags": flags, "digest": digest, "counts": counts}
