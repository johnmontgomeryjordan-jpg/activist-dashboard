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
            "archetype": (pitch.get("archetype") or "").replace("_", " ").title(),
            "thesis": pitch.get("thesis") or r.get("signals") or "",
            "points": (pitch.get("points") or [])[:3],
            "metrics": _metrics_for(fin_context),
            "catalyst": cat,
            "caveat": caveat,
        })
    return cards


def assemble_headlines(board, *, get_news, is_relevant, rank_relevant, per_ticker=2, total=6):
    """Relevant, per-ticker headlines for the board names — reusing the news filters, not the
    GDELT firehose. Ranked by activist-relevance, de-duplicated, trimmed to `total`."""
    collected, seen = [], set()
    for card in board:
        tkr = card.get("ticker")
        if not tkr:
            continue
        rows = [n for n in (get_news(tkr) or []) if is_relevant(n.get("headline"))]
        for n in rank_relevant(rows, per_ticker):
            uid = n.get("url") or n.get("headline")
            if uid in seen:
                continue
            seen.add(uid)
            collected.append({"ticker": tkr, "company": card.get("company"),
                              "headline": n.get("headline"), "source": n.get("source"),
                              "date": (n.get("published_at") or "")[:10], "url": n.get("url")})
    # Already per-ticker relevance-ranked; keep insertion order and trim.
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
                          "why": "Next earnings — a soft print sharpens the thesis and the timing of any outreach."})
        gov = get_governance(cik) or {}
        md = _d(gov.get("meeting_date"))
        if md and today <= md <= horizon + timedelta(days=30):
            items.append({"date": gov["meeting_date"][:10], "company": f"{co} ({tkr})",
                          "why": "Annual meeting — the live governance pressure point (nomination window, say-on-pay)."})
    items.sort(key=lambda x: x["date"])
    return items


def assemble(database, catalyst, news, *, limit=5, today=None):
    """Pull the full report model from the DB. `catalyst` and `news` are the modules."""
    from datetime import datetime
    today = today or datetime.utcnow()
    rows = database.get_scores(limit=80)
    # Optional per-deploy exclusion list (structural disqualifiers beyond the built-in set).
    try:
        from . import config as _cfg
        exclude = set(getattr(_cfg, "REPORT_EXCLUDE_TICKERS", []) or [])
    except Exception:
        exclude = set()
    board = assemble_board(
        rows,
        get_catalyst=lambda cik: catalyst.for_company(cik, database, today=today),
        get_ai_pitch=lambda cik: _loads((database.get_ai_pitch(cik) or {}).get("pitch"), {}),
        get_governance=database.get_governance,
        exclude_tickers=exclude, limit=limit,
    )
    headlines = assemble_headlines(
        board,
        get_news=lambda tk: database.get_news_for_ticker(tk, limit=12),
        is_relevant=news.is_relevant, rank_relevant=news.rank_relevant,
    )
    radar = assemble_radar(board, get_earnings=database.get_earnings,
                           get_governance=database.get_governance, today=today)
    filings = [f for f in (database.recent_filings(limit=25) or []) if (f.get("signals") or "").strip()][:5]
    issue_date = f"{today.strftime('%B')} {today.day}, {today.year}" if hasattr(today, "strftime") else ""
    return {
        "issue_date": issue_date,
        "board": board, "headlines": headlines, "radar": radar, "filings": filings,
    }


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
.evsrc{color:var(--dim);font-size:10.5px;margin-top:14px;text-transform:uppercase;letter-spacing:.06em;}
.evsrc a{color:var(--accent);text-decoration:none;}
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
    return f"""
  <div class="pk{' lead' if lead else ''}">
    <div class="pk-top">
      <div>
        <div class="pk-title"><span class="tkr">{_esc(card.get('ticker'))}</span> &middot; {_esc(card.get('company'))}</div>
        <div class="pk-meta">mkt cap {_esc(card.get('market_cap'))}</div>
        {arch_html}
      </div>
      <div class="vwrap"><span class="vchip {card.get('band_cls')}">{_esc(card.get('vuln'))}</span>
        <span class="vband {card.get('band_cls')}">{_esc(card.get('band'))}</span><span class="vsub">Rating</span></div>
    </div>
    {cat_html}
    <div class="verdict">{_esc(card.get('thesis'))}</div>
    {caveat_html}
    {pts_html}
    {mets_html}
    {src}
  </div>"""


def render_html(model):
    board = model.get("board") or []
    cards = "".join(_render_card(c, lead=(i == 0)) for i, c in enumerate(board)) \
        or '<div class="empty">No qualifying names this issue.</div>'

    hl = "".join(
        f'<div class="item"><a href="{_esc(h.get("url"))}">{_esc(h.get("headline"))}</a>'
        f'<div class="meta"><span class="tag cov">{_esc(h.get("ticker"))} &middot; on board</span>'
        f'<span>{_esc(h.get("source"))}</span><span>{_esc(h.get("date"))}</span></div></div>'
        for h in (model.get("headlines") or [])) or '<div class="empty">No relevant headlines.</div>'

    fl = "".join(
        f'<div class="item"><a href="{_esc(f.get("url"))}">{_esc(f.get("company"))} — {_esc(f.get("title"))}</a>'
        f'<div class="meta"><span class="tag sig">{_esc((f.get("signals") or "").split(",")[0].replace("_"," ").title())}</span>'
        f'<span>{_esc(f.get("filed_at"))}</span></div></div>'
        for f in (model.get("filings") or [])) or '<div class="empty">No notable filings.</div>'

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
  A rating (0–92) of how strongly a company matches the profile activists target — <b>not</b> a probability
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
