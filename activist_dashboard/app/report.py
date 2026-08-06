"""
Biweekly report — generator + renderer.

Turns the live board into the fortnightly "Activist Vulnerability" issue the app serves at
/report and emails to the small distribution. Everything is assembled from data the app
already computes: the scored board (scoring), the deterministic pitch (pitch.py, or the
AI-polished version), the driving catalyst (catalyst.py), per-ticker relevant news
(news.py), earnings timing (earnings.py) and recent event filings (edgar.py).

Design:
  * assemble()   — pulls the model from the DB (thin; delegates selection to helpers).
  * render_html()— renders the model in the app's own "Brief" palette, so the report always
                   matches the pitch kit by construction (no bespoke styling).
Both the page and the email use render_html(); the assembly is decoupled from the DB (it
takes plain rows + callables) so the selection + rendering are unit-testable without a
running app.
"""
import html
import json

# ---- structural disqualifiers ---------------------------------------------------------
# Names that must NOT be presented as proactive activist targets because a campaign is
# structurally foreclosed — a government ownership stake, or a founder/family/PE holder who
# controls the vote. MVP is a static list + the dual-class governance flag; the automated
# beneficial-ownership-% parse (governance.py) supersedes this list as it lands.
# INTC: U.S. government holds a stake. (Extend via config.REPORT_EXCLUDE_TICKERS.)
_STRUCTURAL_EXCLUDE = {"INTC"}

# Founder / insider control we know of that the dual-class flag doesn't catch (single-class
# but a founder holds a blocking stake). Value = the caveat holder label. Superseded by the
# ownership-% parser. Keyed by ticker.
_CONTROLLED_CAVEAT = {
    "SSTK": "founder-chairman Jon Oringer controls ~31% of the vote",
    "ACI": "Cerberus Capital Management holds ~31% of the shares",
}

_BAND = [(75, "Severe", "v3"), (50, "High", "v2"), (25, "Elevated", "v1"), (0, "Moderate", "v1")]

_PCT_KEYS = {"operating_margin", "roa", "revenue_growth", "sga_pct",
             "cash_to_assets", "debt_to_assets", "goodwill_to_assets"}
_X_KEYS = {"pb_ratio", "ev_ebitda"}
# Verdict -> (chip css class, short label). Matches the app's Financials chips.
_VERDICT = {"bad": ("bad", "Weak"), "opp": ("opp", "Lever"), "mid": ("mid", "In line")}
# Which fin_context metrics are worth surfacing on a card, in priority order.
_METRIC_PRIORITY = ["cash_to_assets", "ev_ebitda", "pb_ratio", "operating_margin",
                    "revenue_growth", "goodwill_to_assets", "roa", "sga_pct", "debt_to_assets"]

# Timing-radar copy, keyed by the card's archetype, so five upcoming prints don't all carry the
# same sentence — each reads against the thesis that actually put the name on the board.
_EARNINGS_WHY = {
    "Cash Laggard": "Next earnings — another soft quarter strengthens the case that idle cash should be returned rather than sat on.",
    "Turnaround": "Next earnings — the margin trajectory is the whole thesis; a miss hands a dissident the operating argument.",
    "Value": "Next earnings — a weak print deepens the discount and the pressure for a strategic review.",
    "Governance": "Next earnings — a soft quarter adds fuel to the board-accountability case ahead of the annual meeting.",
}
_EARNINGS_WHY_DEFAULT = "Next earnings — a soft print sharpens the thesis and the timing of any outreach."

# Filing materiality for the "Filings of note" panel, most → least activist-relevant. A routine
# "results_update" is deliberately absent: padding the panel with generic quarterly-results 8-Ks
# from uncovered companies is noise (and makes the AI gloss collapse into boilerplate).
_FILING_RANK = {"restatement": 0, "ceo_departure": 1, "earnings_miss": 2,
                "impairment": 3, "layoffs": 4, "leadership_change": 5}


def _filing_rank(sig_csv):
    """Best (lowest) materiality rank among a filing's signals, or None if nothing material."""
    keys = [s.strip() for s in (sig_csv or "").split(",") if s.strip() in _FILING_RANK]
    return min((_FILING_RANK[k] for k in keys), default=None)


def _esc(s):
    return html.escape(str(s or ""))


def _band(v):
    try:
        v = int(v or 0)
    except (ValueError, TypeError):
        v = 0
    for cut, name, cls in _BAND:
        if v >= cut:
            return name, cls
    return "Moderate", "v1"


def _mcap(v):
    try:
        v = float(v)
    except (ValueError, TypeError):
        return ""
    if v >= 1e12:
        return f"${v / 1e12:.1f}T"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:.0f}"


def _fmt_metric(key, val):
    if val is None:
        return "—"
    try:
        val = float(val)
    except (ValueError, TypeError):
        return _esc(val)
    if key in _X_KEYS:
        return f"{val:.1f}×"
    if key in _PCT_KEYS:
        return f"{val * 100:.0f}%"
    return f"{val:.2f}"


def _loads(s, default):
    try:
        v = json.loads(s or "")
        return v if v is not None else default
    except (ValueError, TypeError):
        return default


def _eff_pitch(row, ai_pitch):
    """Templated pitch, upgraded to the AI-polished thesis/points when present (mirrors emailer)."""
    p = _loads(row.get("pitch"), {})
    a = ai_pitch or {}
    if a.get("thesis"):
        p = dict(p)
        p["thesis"] = a["thesis"]
        if a.get("points"):
            p["points"] = a["points"]
    return p


def _metrics_for(fin_context, n=4):
    """Pick up to n telling fin_context metrics (peer verdict != 'mid' preferred), formatted."""
    by_key = {m.get("key"): m for m in (fin_context or []) if m.get("key")}
    out, seen = [], set()
    # First pass: prioritized, non-'mid' verdicts (the actual levers/weaknesses).
    for key in _METRIC_PRIORITY:
        m = by_key.get(key)
        if not m or key in seen:
            continue
        if m.get("verdict") in ("bad", "opp"):
            out.append(m); seen.add(key)
        if len(out) >= n:
            break
    # Backfill from priority order regardless of verdict if we're short.
    for key in _METRIC_PRIORITY:
        if len(out) >= n:
            break
        m = by_key.get(key)
        if m and key not in seen:
            out.append(m); seen.add(key)
    cards = []
    for m in out[:n]:
        cls, lab = _VERDICT.get(m.get("verdict"), ("mid", "In line"))
        cards.append({"label": m.get("label") or m.get("key"),
                      "value": _fmt_metric(m.get("key"), m.get("value")),
                      "chip_class": cls, "chip_label": lab})
    return cards


# ---- assembly -------------------------------------------------------------------------
def assemble_board(rows, *, get_catalyst, get_ai_pitch, get_governance,
                   exclude_tickers=frozenset(), limit=5):
    """Build the board card models from scored rows. Skips already-engaged names and
    structurally-excluded tickers; caveats controlled companies. `get_*` are callables
    (cik -> data) so this is testable without a DB."""
    excl = set(t.upper() for t in exclude_tickers) | _STRUCTURAL_EXCLUDE
    # Defensive: lead by rating regardless of input order (get_scores is already sorted).
    rows = sorted(rows, key=lambda r: -(r.get("vuln") or 0))
    cards = []
    for r in rows:
        if len(cards) >= limit:
            break
        tkr = (r.get("ticker") or "").upper()
        if r.get("active_situation"):
            continue                      # proactive report — skip names already in a campaign
        if tkr in excl:
            continue                      # structural disqualifier (e.g. government stake)
        cik = r.get("cik")
        pitch = _eff_pitch(r, get_ai_pitch(cik))
        fin_context = _loads(r.get("fin_context"), [])
        band_name, band_cls = _band(r.get("vuln"))
        gov = get_governance(cik) or {}
        caveat = None
        if tkr in _CONTROLLED_CAVEAT:
            caveat = (f"Control overhang: {_CONTROLLED_CAVEAT[tkr]} — frame as a sale / strategic "
                      f"review, not a proxy fight.")
        elif gov.get("dual_class"):
            caveat = ("Control overhang: insiders control the vote through super-voting stock — "
                      "frame as a sale / strategic review, not a board contest.")
        cat = get_catalyst(cik)
        cards.append({
            "cik": cik, "ticker": tkr, "company": r.get("company"),
            "market_cap": _mcap(r.get("market_cap")),
            "vuln": r.get("vuln"), "band": band_name, "band_cls": band_cls,
            # Suppress the placeholder "default" archetype so no card shows a "Default" pill.
            "archetype": ("" if (pitch.get("archetype") or "").strip().lower() in ("", "default")
                          else (pitch.get("archetype") or "").replace("_", " ").title()),
            "thesis": pitch.get("thesis") or r.get("signals") or "",
            "points": (pitch.get("points") or [])[:3],
            "metrics": _metrics_for(fin_context),
            "catalyst": cat,
            "caveat": caveat,
        })
    return cards


def assemble_headlines(board, *, get_news, get_broad, is_relevant, rank_relevant,
                       per_ticker=1, total=5):
    """Relevant headlines for the issue. Lead with the board names' own headlines (reusing the
    news filters, not the GDELT firehose), then BACKFILL from the broad relevance-curated feed so
    the section always fills to `total` even when a fortnight is quiet on the board — off-profile
    items are welcome. Ranked by activist-relevance, de-duplicated."""
    collected, seen = [], set()

    def _add(n, ticker, company, on_board):
        uid = n.get("url") or n.get("headline")
        if not uid or uid in seen:
            return
        seen.add(uid)
        collected.append({"ticker": ticker, "company": company, "on_board": on_board,
                          "headline": n.get("headline"), "source": n.get("source"),
                          "date": (n.get("published_at") or "")[:10], "url": n.get("url")})

    for card in board:
        tkr = card.get("ticker")
        if not tkr:
            continue
        rows = [n for n in (get_news(tkr) or []) if is_relevant(n.get("headline"))]
        for n in rank_relevant(rows, per_ticker):
            _add(n, tkr, card.get("company"), True)
            if len(collected) >= total:
                return collected[:total]
    # Backfill from the broad relevance-curated feed to reach `total`. The item must match a
    # company in OUR universe (matched_tickers), otherwise the international GDELT feed fills the
    # section with unactionable foreign names — a UK pub chain, a Singapore retailer, a French
    # locker business. Off-BOARD is welcome and useful; off-UNIVERSE is not.
    for n in rank_relevant(get_broad() or [], total * 6):
        if len(collected) >= total:
            break
        tk = (n.get("matched_tickers") or "").split(",")[0].strip()
        if not tk:
            continue
        _add(n, tk, None, False)
    return collected[:total]


def assemble_radar(board, *, get_earnings, get_governance, today, horizon_days=16):
    """Upcoming events over the fortnight: board names' next earnings + annual-meeting dates."""
    from datetime import datetime, timedelta
    def _d(s):
        try:
            return datetime.strptime((s or "")[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    horizon = today + timedelta(days=horizon_days)
    items = []
    for card in board:
        cik, tkr, co = card.get("cik"), card.get("ticker"), card.get("company")
        ear = get_earnings(cik) or {}
        nd = _d(ear.get("next_date"))
        if nd and today <= nd <= horizon:
            items.append({"date": ear["next_date"][:10], "company": f"{co} ({tkr})",
                          "why": _EARNINGS_WHY.get(card.get("archetype"), _EARNINGS_WHY_DEFAULT)})
        gov = get_governance(cik) or {}
        md = _d(gov.get("meeting_date"))
        if md and today <= md <= horizon + timedelta(days=30):
            items.append({"date": gov["meeting_date"][:10], "company": f"{co} ({tkr})",
                          "why": "Annual meeting — the live governance pressure point (nomination window, say-on-pay)."})
    items.sort(key=lambda x: x["date"])
    return items


def _blend_select(eligible, *, get_catalyst, prior_score, limit, blend):
    """From vuln-sorted `eligible` rows, pick a blend so each issue mixes standing strength with
    freshness: `n_rating` by current rating, `n_riser` by biggest rating RISE since ~2 weeks ago,
    `n_fresh` by newest catalyst. De-duplicates, then tops up from the next-highest-rated names so
    we still return `limit` whenever the pool is deep enough. (Callers exclude the no-repeat set
    before this, so everything here is already a fresh, eligible name.)"""
    n_rating, n_riser, n_fresh = blend
    chosen, seen = [], set()

    def take(r):
        c = r.get("cik")
        if c in seen:
            return
        seen.add(c); chosen.append(r)

    for r in eligible:                                   # 1) standing strength (already vuln-sorted)
        if len(chosen) >= n_rating:
            break
        take(r)

    if n_riser and prior_score:                          # 2) biggest movers since last issue
        risers = []
        for r in eligible:
            if r.get("cik") in seen:
                continue
            prev = prior_score(r.get("cik"), days=14)
            if prev is not None:
                risers.append(((r.get("vuln") or 0) - prev, r))
        risers.sort(key=lambda x: -x[0])
        for _, r in risers[:n_riser]:
            take(r)

    if n_fresh and get_catalyst:                         # 3) freshest catalyst (a live hook)
        cats = []
        for r in eligible:
            if r.get("cik") in seen:
                continue
            cat = get_catalyst(r.get("cik"))
            if cat and cat.get("date"):
                cats.append((cat["date"], r))
        cats.sort(key=lambda x: x[0], reverse=True)
        for _, r in cats[:n_fresh]:
            take(r)

    for r in eligible:                                   # 4) top up from next-highest-rated
        if len(chosen) >= limit:
            break
        take(r)
    return chosen[:limit]


def assemble(database, catalyst, news, *, limit=5, today=None, summarize=None, rotate=False, pin=None):
    """Pull the full report model from the DB. `catalyst` and `news` are the modules.
    `summarize(text, kind)->str` optionally glosses each headline/filing (the Haiku layer).
    rotate=True  → the biweekly no-repeat build: draw from the full scored universe, drop anything
    featured in the last REPORT_NOREPEAT_ISSUES issues, and pick 5 by the rating/riser/catalyst
    blend. rotate=False → the live top-5 view (unchanged) for the /report page and previews.
    pin=[tickers] → re-render exactly those names (in that order) from the current scored data,
    bypassing rotation. Used by the manual regenerate so a vetted board is refreshed in place with
    corrected fundamentals/theses rather than reshuffled to new companies."""
    from datetime import datetime
    today = today or datetime.utcnow()
    try:
        from . import config as _cfg
        exclude = set(getattr(_cfg, "REPORT_EXCLUDE_TICKERS", []) or [])
        pool_n = int(getattr(_cfg, "REPORT_POOL", 500))
        norepeat_n = int(getattr(_cfg, "REPORT_NOREPEAT_ISSUES", 13))
        blend = tuple(getattr(_cfg, "REPORT_BLEND", (2, 2, 1)))
    except Exception:
        exclude, pool_n, norepeat_n, blend = set(), 500, 13, (2, 2, 1)

    if pin:
        # Re-render a vetted board in place: take exactly the named tickers from current scores,
        # preserving the given order. Score/fundamental fixes flow through; membership does not change.
        want = [t.strip().upper() for t in pin if t and t.strip()]
        by_tk = {(r.get("ticker") or "").upper(): r
                 for r in database.get_scores(limit=max(pool_n, 2000))}
        chosen = [by_tk[t] for t in want if t in by_tk]
    elif rotate:
        rows = database.get_scores(limit=pool_n)          # full scored universe (no floor)
        recent = set()
        try:
            recent = database.get_recent_issue_tickers(norepeat_n)   # 6-month no-repeat memory
        except Exception:
            recent = set()
        excl = {t.upper() for t in exclude} | recent | _STRUCTURAL_EXCLUDE
        eligible = [r for r in rows
                    if not r.get("active_situation")
                    and (r.get("ticker") or "").upper() not in excl]
        eligible.sort(key=lambda r: -(r.get("vuln") or 0))
        chosen = _blend_select(
            eligible, limit=limit, blend=blend,
            get_catalyst=lambda cik: catalyst.for_company(cik, database, today=today),
            prior_score=database.prior_score)
    else:
        chosen = database.get_scores(limit=80)

    board = assemble_board(
        chosen,
        get_catalyst=lambda cik: catalyst.for_company(cik, database, today=today),
        get_ai_pitch=lambda cik: _loads((database.get_ai_pitch(cik) or {}).get("pitch"), {}),
        get_governance=database.get_governance,
        exclude_tickers=exclude, limit=limit,
    )
    headlines = assemble_headlines(
        board,
        get_news=lambda tk: database.get_news_for_ticker(tk, limit=12),
        get_broad=lambda: database.recent_news(limit=40, relevant_only=True),
        is_relevant=news.is_relevant, rank_relevant=news.rank_relevant,
    )
    radar = assemble_radar(board, get_earnings=database.get_earnings,
                           get_governance=database.get_governance, today=today)
    # Filings of note: MATERIAL events first (restatement > CEO exit > miss > impairment > ...),
    # ranked by materiality then recency. If that yields fewer than 5 we pad ONLY with filings from
    # companies on this issue's board — never with routine quarterly-results 8-Ks from uncovered
    # names, which read as filler and make the AI gloss generic.
    recent = database.recent_filings(limit=60) or []
    board_tickers = {(c.get("ticker") or "").upper() for c in board if c.get("ticker")}
    board_names = {(c.get("company") or "").strip().lower() for c in board if c.get("company")}
    material = [f for f in recent if _filing_rank(f.get("signals")) is not None]
    material.sort(key=lambda f: (f.get("filed_at") or ""), reverse=True)   # recency
    material.sort(key=lambda f: _filing_rank(f.get("signals")))            # then materiality (stable)
    filings = material[:5]
    if len(filings) < 5:
        have = {f.get("url") for f in filings}
        for f in recent:
            if len(filings) >= 5:
                break
            if f.get("url") in have:
                continue
            if ((f.get("ticker") or "").upper() in board_tickers
                    or (f.get("company") or "").strip().lower() in board_names):
                filings.append(f)

    # Optional one-line AI summaries under each headline + filing (the Haiku layer). Best-effort:
    # if aithesis isn't available or has no summariser, items simply render without a gloss.
    _summ = summarize or _load_summarizer()
    if _summ:
        def _h_ctx(h):
            who = h.get("ticker") or "not identified"
            where = ("This company IS on our activist-vulnerability board this issue."
                     if h.get("on_board") else "This company is NOT on our board this issue.")
            return f"Company/ticker: {who}. {where}"

        def _f_ctx(f):
            sig = (f.get("signals") or "none").replace("_", " ")
            on = ((f.get("ticker") or "").upper() in board_tickers
                  or (f.get("company") or "").strip().lower() in board_names)
            return (f"Company: {f.get('company') or 'unknown'}. Filing signal: {sig}. "
                    f"{'On our board this issue.' if on else 'Not on our board.'}")

        _attach_summaries(headlines, "headline", _summ, kind="headline", ctx_fn=_h_ctx)
        _attach_summaries(filings, "title", _summ, kind="filing", ctx_fn=_f_ctx)

    issue_date = f"{today.strftime('%B')} {today.day}, {today.year}" if hasattr(today, "strftime") else ""
    return {
        "issue_date": issue_date,
        "board": board, "headlines": headlines, "radar": radar, "filings": filings,
    }


def _load_summarizer():
    """Best-effort handle to the Haiku layer's one-line summariser. Expected interface:
    aithesis.summarize_line(text, kind) -> short str (or None). Returns None if unavailable."""
    try:
        from . import aithesis
    except Exception:
        return None
    fn = getattr(aithesis, "summarize_line", None)
    return fn if callable(fn) else None


def _attach_summaries(items, textkey, summarize, kind, ctx_fn=None):
    """Attach a one-line AI gloss to each item. `ctx_fn(item) -> str` supplies the factual
    context (company, signal, on/off board) that keeps summaries specific rather than boilerplate.
    Degrades safely: falls back to a context-free call, then to no summary at all."""
    for it in items or []:
        s = None
        try:
            ctx = ctx_fn(it) if ctx_fn else None
            s = summarize(it.get(textkey) or "", kind, context=ctx)
        except TypeError:
            try:                                  # summariser without a context parameter
                s = summarize(it.get(textkey) or "", kind)
            except Exception:
                s = None
        except Exception:
            s = None
        if s:
            it["summary"] = s.strip()


# ---- rendering (Brief palette, mirrors the pitch kit) ---------------------------------
_CSS = """
:root{--bg:#f6f4ee;--panel:#fffdf8;--panel2:#f1eee4;--text:#1c1b18;--muted:#79766b;--dim:#9a978b;
--line:#e3ded2;--line2:#d4cdbd;--accent:#2f5fa6;--hot:#b23b32;--warn:#9a6a18;--ok:#15724e;--brand:#15724e;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);
color:var(--text);font-size:14.5px;line-height:1.55;-webkit-font-smoothing:antialiased;}
a{color:inherit;}
.wrap{padding:24px 30px 60px;max-width:940px;margin:0 auto;}
header{padding:26px 30px 16px;border-bottom:2px solid var(--text);max-width:940px;margin:0 auto;}
.brand{color:var(--brand);font-weight:700;font-size:11.5px;letter-spacing:.26em;text-transform:uppercase;}
header h1{font-family:var(--serif);font-size:32px;margin:6px 0 0;font-weight:500;}
header .sub{color:var(--dim);font-size:12.5px;margin-top:5px;}
.issue{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-top:14px;font-size:12px;color:var(--muted);}
.issue b{color:var(--text);font-weight:600;}
h2.sec{font-family:var(--serif);font-size:22px;font-weight:500;margin:34px 0 4px;}
h3.sub{font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted);margin:26px 0 12px;font-weight:700;}
.hint{color:var(--muted);font-size:13px;margin:0 0 18px;line-height:1.6;max-width:820px;}
.pk{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:22px 26px;margin-bottom:18px;}
.pk.lead{border-left:3px solid var(--brand);}
.pk-top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;border-bottom:1px solid var(--line);padding-bottom:15px;}
.pk-title{font-family:var(--serif);font-size:24px;font-weight:500;line-height:1.15;}
.pk-title .tkr{color:var(--brand);}
.pk-meta{color:var(--muted);font-size:12.5px;margin-top:5px;}
.pk-arch{display:inline-block;margin-top:9px;font-size:10.5px;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;background:var(--panel2);color:var(--muted);border-radius:4px;padding:3px 10px;}
.vwrap{flex:none;text-align:center;}
.vchip{display:inline-block;min-width:46px;text-align:center;padding:6px 12px;border-radius:6px;
font-weight:700;font-size:20px;font-family:var(--serif);}
.vchip.v3{background:#f4e4e2;color:var(--hot);}.vchip.v2{background:#f1e7d4;color:var(--warn);}.vchip.v1{background:#e6eef7;color:var(--accent);}
.vband{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;margin-top:5px;display:block;}
.vband.v3{color:var(--hot);}.vband.v2{color:var(--warn);}.vband.v1{color:var(--accent);}
.vsub{font-size:10px;color:var(--dim);margin-top:2px;}
.catalyst{background:#f7f1e3;border:1px solid #e6d9b6;border-left:3px solid var(--warn);border-radius:0 8px 8px 0;
padding:12px 15px;margin:16px 0 4px;font-size:13.5px;line-height:1.55;}
.catalyst .ct-h{color:var(--warn);text-transform:uppercase;font-size:10px;letter-spacing:.12em;font-weight:700;display:block;margin-bottom:4px;}
.catalyst a{color:var(--accent);text-decoration:none;}
.verdict{font-family:var(--serif);font-size:18px;line-height:1.6;margin:16px 0 4px;max-width:800px;}
.caveat{background:#eef3f8;border:1px solid #d8e2ee;border-radius:8px;padding:9px 13px;margin:10px 0 0;font-size:12.5px;color:var(--accent);}
.pitch-points{list-style:none;padding:0;margin:10px 0 0;}
.pitch-points li{display:flex;gap:14px;font-size:14px;line-height:1.55;padding:12px 0;border-top:1px solid var(--line);}
.pitch-points li:first-child{border-top:none;}
.pitch-points .num{font-family:var(--serif);font-size:17px;font-weight:600;color:var(--brand);flex-shrink:0;width:16px;}
.fin{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-top:18px;}
@media (max-width:640px){.fin{grid-template-columns:repeat(2,1fr);}}
.metric{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px 14px;}
.metric-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px;}
.metric .mk{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:600;}
.metric .mv{font-family:var(--serif);font-size:20px;font-weight:500;margin-top:4px;}
.chip{font-size:9px;letter-spacing:.06em;text-transform:uppercase;font-weight:700;padding:2px 7px;border-radius:4px;white-space:nowrap;}
.chip.bad{background:#f4e4e2;color:var(--hot);}.chip.opp{background:#f1e7d4;color:var(--warn);}.chip.mid{background:var(--panel2);color:var(--muted);}
.pk-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px;flex-wrap:wrap;}
.evsrc{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;}
.evsrc a{color:var(--accent);text-decoration:none;}
.pk-profile{display:inline-block;background:var(--brand);color:#fff;text-decoration:none;padding:8px 15px;
border-radius:6px;font-size:12.5px;font-weight:600;white-space:nowrap;}
.pk-profile:hover{background:#0f5c3c;}
.isum{color:var(--muted);font-size:12px;line-height:1.5;margin-top:5px;font-style:italic;}
.isum b{color:var(--brand);font-style:normal;font-weight:700;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;margin-right:6px;}
.row{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
@media (max-width:820px){.row{grid-template-columns:1fr;}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;}
.panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.16em;margin:0;padding:14px 18px;border-bottom:1px solid var(--line);color:var(--muted);font-weight:700;}
.item{padding:13px 18px;border-bottom:1px solid var(--line);}
.item:last-child{border-bottom:none;}
.item a{color:var(--text);text-decoration:none;font-weight:500;}
.item a:hover{color:var(--brand);}
.meta{color:var(--dim);font-size:11.5px;margin-top:5px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.tag{display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;background:var(--panel2);color:var(--muted);}
.tag.cov{background:#e7f0ea;color:var(--brand);}
.tag.sig{background:#f1e7d4;color:var(--warn);}
table.radar{width:100%;border-collapse:collapse;table-layout:fixed;}
.radar th{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;font-weight:700;text-align:left;padding:12px 18px;border-bottom:1px solid var(--line);}
.radar td{padding:13px 18px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13.5px;}
.radar tr:last-child td{border-bottom:none;}
.radar .when{color:var(--accent);font-weight:700;white-space:nowrap;}
.foot{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);font-size:11.5px;color:var(--muted);line-height:1.6;}
.foot b{color:var(--text);}
.empty{padding:20px;color:var(--dim);}
"""


def _render_card(card, lead=False):
    cat = card.get("catalyst")
    cat_html = ""
    if cat and cat.get("sentence"):
        link = f' <a href="{_esc(cat.get("url"))}">filing &rarr;</a>' if cat.get("url") else ""
        cat_html = (f'<div class="catalyst"><span class="ct-h">Catalyst &middot; {_esc(cat.get("date_pretty"))}</span>'
                    f'{_esc(cat.get("sentence"))}{link}</div>')
    pts = "".join(f'<li><span class="num">{i}</span><span>{_esc(p)}</span></li>'
                  for i, p in enumerate(card.get("points") or [], 1))
    pts_html = f'<ul class="pitch-points">{pts}</ul>' if pts else ""
    mets = "".join(
        f'<div class="metric"><div class="metric-top"><span class="mk">{_esc(m["label"])}</span>'
        f'<span class="chip {m["chip_class"]}">{_esc(m["chip_label"])}</span></div>'
        f'<div class="mv">{_esc(m["value"])}</div></div>'
        for m in (card.get("metrics") or []))
    mets_html = f'<div class="fin">{mets}</div>' if mets else ""
    caveat_html = f'<div class="caveat">{_esc(card["caveat"])}</div>' if card.get("caveat") else ""
    arch_html = f'<span class="pk-arch">{_esc(card["archetype"])}</span>' if card.get("archetype") else ""
    src = ""
    if cat and cat.get("url"):
        src = f'<div class="evsrc">Catalyst: <a href="{_esc(cat["url"])}">SEC 8-K, {_esc(cat.get("date_pretty"))}</a> &middot; Financials: SEC filings</div>'
    else:
        src = '<div class="evsrc">Financials: SEC filings</div>'
    # Profile button: deep-links into the app's company profile. target=_top so it works both
    # standalone and inside the landing-page iframe; app.js reads ?company= on load.
    profile = f'<a class="pk-profile" target="_top" href="/?company={_esc(card.get("cik"))}">View full profile &rarr;</a>' if card.get("cik") else ""
    return f"""
  <div class="pk{' lead' if lead else ''}">
    <div class="pk-top">
      <div>
        <div class="pk-title"><span class="tkr">{_esc(card.get('ticker'))}</span> &middot; {_esc(card.get('company'))}</div>
        <div class="pk-meta">mkt cap {_esc(card.get('market_cap'))}</div>
        {arch_html}
      </div>
      <div class="vwrap"><span class="vband {card.get('band_cls')}">{_esc(card.get('band'))}</span></div>
    </div>
    {cat_html}
    <div class="verdict">{_esc(card.get('thesis'))}</div>
    {caveat_html}
    {pts_html}
    {mets_html}
    <div class="pk-actions">{src}{profile}</div>
  </div>"""


def render_html(model, email=False, site_url=None):
    """Render the report model to HTML.

    email=False (default) -> the /report web page: a <style> block driving CSS grid/flexbox.
      This is the path the site and /api/report/preview use and it is deliberately unchanged.
    email=True            -> the inbox-safe render (inline styles, <table> layout, absolute
      URLs resolved against SITE_URL). Gmail strips <style> blocks outright, which is why the
      web markup cannot simply be reused in an email.

    The flag is a thin switch, not a fork in the rendering logic: both modes consume the exact
    same model, so the emailed issue and the web page cannot drift apart in substance.
    """
    if email:
        return render_email_html(model, site_url=site_url)
    board = model.get("board") or []
    cards = "".join(_render_card(c, lead=(i == 0)) for i, c in enumerate(board)) \
        or '<div class="empty">No qualifying names this issue.</div>'

    def _isum(it):
        return f'<div class="isum"><b>AI</b>{_esc(it["summary"])}</div>' if it.get("summary") else ""

    def _hl_item(h):
        if h.get("on_board") and h.get("ticker"):
            tag = f'<span class="tag cov">{_esc(h.get("ticker"))} &middot; on board</span>'
        elif h.get("ticker"):
            tag = f'<span class="tag">{_esc(h.get("ticker"))}</span>'
        else:
            tag = ""
        return (f'<div class="item"><a href="{_esc(h.get("url"))}">{_esc(h.get("headline"))}</a>'
                f'{_isum(h)}<div class="meta">{tag}<span>{_esc(h.get("source"))}</span>'
                f'<span>{_esc(h.get("date"))}</span></div></div>')

    def _fl_item(f):
        sig = (f.get("signals") or "").split(",")[0].replace("_", " ").title() or "Filing"
        return (f'<div class="item"><a href="{_esc(f.get("url"))}">{_esc(f.get("company"))} — {_esc(f.get("title"))}</a>'
                f'{_isum(f)}<div class="meta"><span class="tag sig">{_esc(sig)}</span>'
                f'<span>{_esc(f.get("filed_at"))}</span></div></div>')

    hl = "".join(_hl_item(h) for h in (model.get("headlines") or [])) or '<div class="empty">No relevant headlines.</div>'
    fl = "".join(_fl_item(f) for f in (model.get("filings") or [])) or '<div class="empty">No notable filings.</div>'

    radar_rows = "".join(
        f'<tr><td class="when">{_esc(r.get("date"))}</td><td><b>{_esc(r.get("company"))}</b></td>'
        f'<td>{_esc(r.get("why"))}</td></tr>'
        for r in (model.get("radar") or []))
    radar_html = (f'<div class="panel"><table class="radar"><colgroup><col style="width:18%"><col style="width:30%">'
                  f'<col style="width:52%"></colgroup><thead><tr><th>When</th><th>Company</th><th>Why it matters</th>'
                  f'</tr></thead><tbody>{radar_rows}</tbody></table></div>'
                  if radar_rows else '<div class="panel"><div class="empty">No dated events over the fortnight.</div></div>')

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FGS — Activist Vulnerability · Biweekly</title><style>{_CSS}</style></head><body>
<header>
  <div class="brand">FGS Global &middot; Situations &amp; Shareholder Advisory</div>
  <h1>Activist Vulnerability — Biweekly</h1>
  <div class="sub">Predictive activist-defense intelligence &middot; free public data &middot; fortnightly</div>
  <div class="issue"><span>Fortnight of <b>{_esc(model.get('issue_date'))}</b></span>
    <span>Screen: <b>S&amp;P 1500</b> &middot; SEC / market / news</span></div>
</header>
<div class="wrap">
  <h2 class="sec">This fortnight's board</h2>
  <p class="hint">The names screening highest for activist exposure, each paired with the driving catalyst.
  A read of how strongly a company matches the profile activists target — <b>not</b> a probability
  of a campaign. Every figure traces to a filing.</p>
  {cards}
  <h3 class="sub">Relevant headlines</h3>
  <div class="row"><div class="panel"><h2>On the board</h2>{hl}</div>
    <div class="panel"><h2>Filings of note</h2>{fl}</div></div>
  <h3 class="sub">Timing radar</h3>
  {radar_html}
  <div class="foot">
    <p><b>How to read this.</b> The rating blends valuation, relative underperformance, balance-sheet slack,
    governance friction and fresh catalysts across the S&amp;P 1500, measured against each company's industry
    peers, on free public data. It flags <b>exposure, not an active campaign</b>. Every catalyst links its filing.</p>
    <p><b>FGS Global</b> &middot; Situations &amp; Shareholder Advisory &middot; Internal — not for external distribution.</p>
  </div>
</div></body></html>"""


# ---- email rendering (inline styles, table layout) -------------------------------------
# render_html() above targets the /report web page: a <style> block driving CSS grid/flexbox,
# which most mail clients (Outlook/Gmail app strip <style>, ignore grid/flex) will mangle. This
# is the email-safe twin: same model, same content and section order, but every rule is inlined
# on the element and layout runs on <table> instead of div/grid. Internal links (the profile deep
# link) are resolved to absolute URLs via SITE_URL since a relative href is meaningless once the
# HTML is lifted out of the browser and into an inbox.
_EBAND_CSS = {"v3": ("#f4e4e2", "#b23b32"), "v2": ("#f1e7d4", "#9a6a18"), "v1": ("#e6eef7", "#2f5fa6")}
_ECHIP_CSS = {"bad": ("#f4e4e2", "#b23b32"), "opp": ("#f1e7d4", "#9a6a18"), "mid": ("#f1eee4", "#79766b")}


def _abs_url(url, site_url):
    """Resolve a possibly-relative URL against SITE_URL. Filing/news links from SEC/press
    sources already arrive absolute; this only matters for in-app links like the profile deep
    link, which are relative by design on the web page."""
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{(site_url or '').rstrip('/')}/{str(url).lstrip('/')}"


def _render_metrics_email(metrics, per_row=2):
    """Metric cards as a fixed-width table grid. The web page lays these out 4-across via CSS
    grid and collapses to 2-across under 640px; an email IS that narrow, so 2-across is the
    faithful equivalent rather than a compromise. Odd counts are padded with an empty cell —
    without it a lone metric stretches to full width and reads like a rendering fault."""
    if not metrics:
        return ""
    cells = []
    for m in metrics:
        bg, fg = _ECHIP_CSS.get(m.get("chip_class"), _ECHIP_CSS["mid"])
        cells.append(f"""
        <td width="{int(100 / per_row)}%" valign="top" style="padding:5px;">
          <table role="presentation" width="100%" style="background:#f6f4ee;border:1px solid #e3ded2;border-radius:8px;border-collapse:separate;">
            <tr><td style="padding:10px 12px;">
              <table role="presentation" width="100%" style="border-collapse:collapse;"><tr>
                <td valign="top" style="font-size:9.5px;line-height:1.3;letter-spacing:.06em;text-transform:uppercase;color:#79766b;font-weight:600;">{_esc(m["label"])}</td>
                <td valign="top" align="right" style="white-space:nowrap;padding-left:6px;"><span style="font-size:9px;letter-spacing:.06em;text-transform:uppercase;font-weight:700;padding:2px 7px;border-radius:4px;background:{bg};color:{fg};">{_esc(m["chip_label"])}</span></td>
              </tr></table>
              <div style="font-family:Georgia,'Times New Roman',serif;font-size:19px;font-weight:500;margin-top:3px;color:#1c1b18;">{_esc(m["value"])}</div>
            </td></tr>
          </table>
        </td>""")
    filler = f'<td width="{int(100 / per_row)}%" style="padding:5px;">&nbsp;</td>'
    rows = ""
    for i in range(0, len(cells), per_row):
        chunk = cells[i:i + per_row]
        rows += f'<tr>{"".join(chunk)}{filler * (per_row - len(chunk))}</tr>'
    return (f'<table role="presentation" width="100%" style="border-collapse:collapse;'
            f'margin-top:12px;table-layout:fixed;">{rows}</table>')


def _render_points_email(points):
    if not points:
        return ""
    rows = "".join(
        f'''<tr>
          <td width="24" valign="top" style="padding:11px 0;{'border-top:1px solid #e3ded2;' if i > 1 else ''}">
            <span style="font-family:Georgia,'Times New Roman',serif;font-size:16px;font-weight:600;color:#15724e;">{i}</span>
          </td>
          <td valign="top" style="padding:11px 0 11px 12px;font-size:14px;line-height:1.55;color:#1c1b18;{'border-top:1px solid #e3ded2;' if i > 1 else ''}">{_esc(p)}</td>
        </tr>'''
        for i, p in enumerate(points, 1)
    )
    return f'<table role="presentation" width="100%" style="border-collapse:collapse;margin-top:8px;">{rows}</table>'


def _render_card_email(card, site_url, lead=False):
    band_bg, band_fg = _EBAND_CSS.get(card.get("band_cls"), _EBAND_CSS["v1"])
    border_left = "border-left:3px solid #15724e;" if lead else ""

    cat = card.get("catalyst")
    cat_html = ""
    if cat and cat.get("sentence"):
        link = (f' <a href="{_esc(_abs_url(cat.get("url"), site_url))}" style="color:#2f5fa6;text-decoration:none;">filing &rarr;</a>'
                if cat.get("url") else "")
        cat_html = f"""
      <tr><td style="padding:12px 15px;background:#f7f1e3;border:1px solid #e6d9b6;border-left:3px solid #9a6a18;border-radius:0 8px 8px 0;font-size:13.5px;line-height:1.55;color:#1c1b18;">
        <span style="display:block;color:#9a6a18;text-transform:uppercase;font-size:10px;letter-spacing:.12em;font-weight:700;margin-bottom:4px;">Catalyst &middot; {_esc(cat.get("date_pretty"))}</span>
        {_esc(cat.get("sentence"))}{link}
      </td></tr>
      <tr><td style="height:12px;line-height:12px;font-size:0;">&nbsp;</td></tr>"""

    caveat_html = ""
    if card.get("caveat"):
        caveat_html = f"""
      <tr><td style="padding:9px 13px;background:#eef3f8;border:1px solid #d8e2ee;border-radius:8px;font-size:12.5px;color:#2f5fa6;">{_esc(card["caveat"])}</td></tr>
      <tr><td style="height:6px;line-height:6px;font-size:0;">&nbsp;</td></tr>"""

    arch_html = (f'<span style="display:inline-block;margin-top:9px;font-size:10.5px;font-weight:700;letter-spacing:.08em;'
                f'text-transform:uppercase;background:#f1eee4;color:#79766b;border-radius:4px;padding:3px 10px;">{_esc(card["archetype"])}</span>'
                if card.get("archetype") else "")

    pts_html = _render_points_email(card.get("points") or [])
    mets_html = _render_metrics_email(card.get("metrics") or [])

    src = (f'Catalyst: <a href="{_esc(_abs_url(cat["url"], site_url))}" style="color:#2f5fa6;text-decoration:none;">SEC 8-K, {_esc(cat.get("date_pretty"))}</a> &middot; Financials: SEC filings'
           if cat and cat.get("url") else "Financials: SEC filings")
    profile = ""
    if card.get("cik"):
        profile_url = _abs_url(f"/?company={card.get('cik')}", site_url)
        profile = (f'<a href="{_esc(profile_url)}" style="background:#15724e;color:#ffffff;text-decoration:none;'
                  f'padding:8px 15px;border-radius:6px;font-size:12.5px;font-weight:600;white-space:nowrap;">View full profile &rarr;</a>')

    return f"""
  <table role="presentation" width="100%" style="background:#fffdf8;border:1px solid #e3ded2;{border_left}border-radius:10px;border-collapse:separate;margin:0 0 16px;">
    <tr><td style="padding:22px 24px;">
      <table role="presentation" width="100%" style="border-collapse:collapse;">
        <tr>
          <td valign="top">
            <div style="font-family:Georgia,'Times New Roman',serif;font-size:22px;color:#1c1b18;">
              <span style="color:#15724e;">{_esc(card.get('ticker'))}</span> &middot; {_esc(card.get('company'))}
            </div>
            <div style="color:#79766b;font-size:12.5px;margin-top:5px;">mkt cap {_esc(card.get('market_cap'))}</div>
            {arch_html}
          </td>
          <td valign="top" align="center" width="90">
            <table role="presentation" style="margin-left:auto;"><tr><td align="center" style="background:{band_bg};color:{band_fg};border-radius:6px;padding:6px 12px;font-family:Georgia,'Times New Roman',serif;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.1em;">{_esc(card.get('band'))}</td></tr></table>
          </td>
        </tr>
      </table>
      <table role="presentation" width="100%" style="border-collapse:collapse;margin-top:14px;">
        {cat_html}
        <tr><td style="font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.6;color:#1c1b18;">{_esc(card.get('thesis'))}</td></tr>
        {caveat_html}
      </table>
      {pts_html}
      {mets_html}
      <table role="presentation" width="100%" style="border-collapse:collapse;margin-top:16px;"><tr>
        <td style="color:#9a978b;font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;">{src}</td>
        <td align="right">{profile}</td>
      </tr></table>
    </td></tr>
  </table>"""


def _hl_item_email(h, site_url):
    if h.get("on_board") and h.get("ticker"):
        tag = f'<span style="font-size:11px;padding:1px 8px;border-radius:4px;background:#e7f0ea;color:#15724e;">{_esc(h.get("ticker"))} &middot; on board</span>'
    elif h.get("ticker"):
        tag = f'<span style="font-size:11px;padding:1px 8px;border-radius:4px;background:#f1eee4;color:#79766b;">{_esc(h.get("ticker"))}</span>'
    else:
        tag = ""
    isum = (f'<div style="color:#79766b;font-size:12px;line-height:1.5;margin-top:5px;font-style:italic;">'
           f'<b style="color:#15724e;font-style:normal;font-weight:700;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;margin-right:6px;">AI</b>{_esc(h["summary"])}</div>'
           if h.get("summary") else "")
    return f"""
      <tr><td style="padding:13px 18px;border-bottom:1px solid #e3ded2;">
        <a href="{_esc(_abs_url(h.get('url'), site_url))}" style="color:#1c1b18;text-decoration:none;font-weight:500;">{_esc(h.get('headline'))}</a>
        {isum}
        <div style="color:#9a978b;font-size:11.5px;margin-top:5px;">{tag}&nbsp;&nbsp;{_esc(h.get('source'))} &middot; {_esc(h.get('date'))}</div>
      </td></tr>"""


def _fl_item_email(f, site_url):
    sig = (f.get("signals") or "").split(",")[0].replace("_", " ").title() or "Filing"
    isum = (f'<div style="color:#79766b;font-size:12px;line-height:1.5;margin-top:5px;font-style:italic;">'
           f'<b style="color:#15724e;font-style:normal;font-weight:700;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;margin-right:6px;">AI</b>{_esc(f["summary"])}</div>'
           if f.get("summary") else "")
    return f"""
      <tr><td style="padding:13px 18px;border-bottom:1px solid #e3ded2;">
        <a href="{_esc(_abs_url(f.get('url'), site_url))}" style="color:#1c1b18;text-decoration:none;font-weight:500;">{_esc(f.get('company'))} — {_esc(f.get('title'))}</a>
        {isum}
        <div style="color:#9a978b;font-size:11.5px;margin-top:5px;">
          <span style="font-size:11px;padding:1px 8px;border-radius:4px;background:#f1e7d4;color:#9a6a18;">{_esc(sig)}</span>
          &nbsp;&nbsp;{_esc(f.get('filed_at'))}
        </div>
      </td></tr>"""


def render_email_html(model, site_url=None):
    """Email-safe render of the biweekly report model: inline styles + <table> layout only (no
    <style> block, no grid/flex), so it survives Gmail/Outlook/Apple Mail sanitization intact.
    Same content and section order as render_html() (the /report web page), so the emailed issue
    and the site page never drift apart in substance -- only in markup. All internal links
    (currently just the profile deep link) are resolved to absolute via `site_url`; when the
    caller doesn't pass one we fall back to config.SITE_URL, so a relative href can never reach
    an inbox as the invalid `http:///?company=...` a browser-less client produces."""
    if site_url is None:
        try:
            from . import config as _cfg
            site_url = getattr(_cfg, "SITE_URL", "") or ""
        except Exception:
            site_url = ""
    board = model.get("board") or []
    cards = "".join(_render_card_email(c, site_url, lead=(i == 0)) for i, c in enumerate(board)) \
        or '<p style="color:#9a978b;padding:20px 0;">No qualifying names this issue.</p>'

    hl_rows = "".join(_hl_item_email(h, site_url) for h in (model.get("headlines") or []))
    hl = (f'<table role="presentation" width="100%" style="border-collapse:collapse;">{hl_rows}</table>'
          if hl_rows else '<p style="color:#9a978b;padding:14px 18px;margin:0;">No relevant headlines.</p>')

    fl_rows = "".join(_fl_item_email(f, site_url) for f in (model.get("filings") or []))
    fl = (f'<table role="presentation" width="100%" style="border-collapse:collapse;">{fl_rows}</table>'
          if fl_rows else '<p style="color:#9a978b;padding:14px 18px;margin:0;">No notable filings.</p>')

    # Column widths mirror the web page's <colgroup> (18/30/52). Without them the browser
    # auto-sizes and a name like "Builders FirstSource (BLDR)" wraps onto three lines.
    _rth = ('padding:11px 16px;border-bottom:1px solid #e3ded2;color:#79766b;font-size:10.5px;'
            'text-transform:uppercase;letter-spacing:.1em;font-weight:700;')
    _rtd = 'padding:12px 16px;border-bottom:1px solid #e3ded2;vertical-align:top;'
    radar_rows = "".join(
        f'<tr><td style="{_rtd}color:#2f5fa6;font-weight:700;white-space:nowrap;font-size:13px;">{_esc(r.get("date"))}</td>'
        f'<td style="{_rtd}font-size:13.5px;"><b>{_esc(r.get("company"))}</b></td>'
        f'<td style="{_rtd}font-size:13.5px;line-height:1.5;">{_esc(r.get("why"))}</td></tr>'
        for r in (model.get("radar") or []))
    radar_html = (f'<table role="presentation" width="100%" style="border-collapse:collapse;table-layout:fixed;">'
                  f'<colgroup><col style="width:22%"><col style="width:30%"><col style="width:48%"></colgroup>'
                  f'<tr><th align="left" style="{_rth}">When</th>'
                  f'<th align="left" style="{_rth}">Company</th>'
                  f'<th align="left" style="{_rth}">Why it matters</th></tr>'
                  f'{radar_rows}</table>'
                  if radar_rows else '<p style="color:#9a978b;padding:16px 18px;margin:0;">No dated events over the fortnight.</p>')

    def panel(title, inner):
        """Bordered cream panel. `title=None` renders the panel with no header strip — the
        radar table carries its own When/Company/Why header row, so titling it as well would
        stack two headers, which the web page doesn't do."""
        head = (f'<tr><td style="padding:14px 18px;border-bottom:1px solid #e3ded2;color:#79766b;'
                f'font-size:11px;text-transform:uppercase;letter-spacing:.16em;font-weight:700;">{_esc(title)}</td></tr>'
                if title else "")
        return (f'<table role="presentation" width="100%" style="background:#fffdf8;border:1px solid #e3ded2;border-radius:10px;border-collapse:separate;">'
                f'{head}<tr><td>{inner}</td></tr></table>')

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>FGS — Activist Vulnerability · Biweekly</title></head>
<body style="margin:0;padding:0;background:#f6f4ee;">
<table role="presentation" width="100%" style="background:#f6f4ee;border-collapse:collapse;"><tr><td align="center">
<table role="presentation" width="640" style="max-width:640px;width:100%;border-collapse:collapse;">
  <tr><td style="padding:26px 26px 16px;border-bottom:2px solid #1c1b18;">
    <div style="color:#15724e;font-weight:700;font-size:11.5px;letter-spacing:.26em;text-transform:uppercase;">FGS Global &middot; Situations &amp; Shareholder Advisory</div>
    <div style="font-family:Georgia,'Times New Roman',serif;font-size:28px;margin:8px 0 0;font-weight:500;color:#1c1b18;">Activist Vulnerability — Biweekly</div>
    <div style="color:#9a978b;font-size:12.5px;margin-top:6px;">Predictive activist-defense intelligence &middot; free public data &middot; fortnightly</div>
    <table role="presentation" width="100%" style="margin-top:14px;"><tr>
      <td style="font-size:12px;color:#79766b;">Fortnight of <b style="color:#1c1b18;">{_esc(model.get('issue_date'))}</b></td>
      <td align="right" style="font-size:12px;color:#79766b;">Screen: <b style="color:#1c1b18;">S&amp;P 1500</b> &middot; SEC / market / news</td>
    </tr></table>
  </td></tr>
  <tr><td style="padding:24px 26px 0;">

    <div style="font-family:Georgia,'Times New Roman',serif;font-size:20px;font-weight:500;color:#1c1b18;margin:0 0 4px;">This fortnight's board</div>
    <p style="color:#79766b;font-size:13px;line-height:1.6;margin:0 0 18px;">The names screening highest for activist exposure, each paired with the driving catalyst.
    A read of how strongly a company matches the profile activists target — <b>not</b> a probability
    of a campaign. Every figure traces to a filing.</p>
    {cards}

    <div style="font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:#79766b;font-weight:700;margin:26px 0 12px;">Relevant headlines</div>
    {panel("On the board", hl)}
    <div style="height:16px;line-height:16px;font-size:0;">&nbsp;</div>
    {panel("Filings of note", fl)}

    <div style="font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:#79766b;font-weight:700;margin:26px 0 12px;">Timing radar</div>
    {panel(None, radar_html)}

    <div style="margin-top:30px;padding-top:16px;border-top:1px solid #e3ded2;font-size:11.5px;color:#79766b;line-height:1.6;">
      <p style="margin:0 0 8px;"><b style="color:#1c1b18;">How to read this.</b> The rating blends valuation, relative underperformance, balance-sheet slack,
      governance friction and fresh catalysts across the S&amp;P 1500, measured against each company's industry
      peers, on free public data. It flags <b style="color:#1c1b18;">exposure, not an active campaign</b>. Every catalyst links its filing.</p>
      <p style="margin:0;"><b style="color:#1c1b18;">FGS Global</b> &middot; Situations &amp; Shareholder Advisory &middot; Internal — not for external distribution.</p>
    </div>
  </td></tr>
  <tr><td style="height:26px;line-height:26px;font-size:0;">&nbsp;</td></tr>
</table>
</td></tr></table>
</body></html>"""
