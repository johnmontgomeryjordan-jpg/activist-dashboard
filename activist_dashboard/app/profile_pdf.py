"""
Company profile -> print/PDF HTML.

The live company-profile page (static/app.js) is a client-side, JavaScript-rendered, tabbed
single-page app: it fetches /api/company and builds each of its eight tabs (Overview, Evidence,
Financials, Peer analysis, Ownership & insiders, Advisors, Filings & news, Notes) in the browser.
WeasyPrint (the PDF engine, chosen over a headless browser to fit the free-tier Render deployment's
512MB RAM ceiling -- see pdf.py) converts static HTML+CSS to a PDF; it does not run JavaScript. So
this module is a SEPARATE, server-side renderer that takes the same /api/company data and lays out
every tab's content flat on one document, section by section, instead of behind clickable tabs.

This is deliberately NOT a pixel clone of the interactive page -- it's a clean, print-organized
document covering the same underlying data, in the same visual language as the biweekly report
(reuses report._CSS / report._esc) so the two PDF types look like one product.
"""
from . import report as _report

_esc = _report._esc

# Extra rules on top of report._CSS for structures the report page doesn't need: a two-column
# key/value grid (contacts, governance), a plain data table (peers, holders, filings/news), and a
# print-specific page setup (WeasyPrint reads @page for margins/size).
_EXTRA_CSS = """
@page { size: Letter; margin: 0.6in; }
.kv { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 24px; margin: 8px 0 4px; }
.kv .row { display: flex; justify-content: space-between; gap: 10px; padding: 5px 0;
          border-bottom: 1px solid var(--line); font-size: 13px; }
.kv .row .k { color: var(--muted); }
.kv .row .v { font-weight: 600; text-align: right; }
table.plain { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0 4px; }
table.plain th { text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase;
                 letter-spacing: .08em; font-weight: 700; padding: 7px 10px; border-bottom: 1px solid var(--line2); }
table.plain td { padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
table.plain tr:last-child td { border-bottom: none; }
.sechead { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
          border-bottom: 2px solid var(--text); padding-bottom: 8px; margin: 30px 0 4px; }
.sechead:first-of-type { margin-top: 0; }
.sechead h2 { margin: 0; }
.pagebreak { page-break-before: always; }
.small { font-size: 11.5px; color: var(--dim); }
.notebox { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          padding: 14px 18px; white-space: pre-wrap; font-size: 13.5px; }
"""


def _money(v):
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _pct(v, digits=1):
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _num(v, digits=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _date(v):
    return _esc(str(v)[:10]) if v else "—"


def _kv(pairs):
    """pairs: [(label, value_html_already_escaped_or_safe), ...]. Skips (label, None/""/'—')."""
    rows = "".join(f'<div class="row"><span class="k">{_esc(k)}</span><span class="v">{v}</span></div>'
                   for k, v in pairs if v not in (None, "", "—"))
    return f'<div class="kv">{rows}</div>' if rows else '<p class="small">Nothing on record.</p>'


def _section(title, body_html, subnote=None):
    note = f'<p class="hint" style="margin-top:2px;">{_esc(subnote)}</p>' if subnote else ""
    return f'<div class="sechead"><h2 class="sec">{_esc(title)}</h2></div>{note}{body_html}'


# --- 1. Overview -----------------------------------------------------------------------------
def _overview(d):
    o = d.get("overview") or {}
    tsr = d.get("tsr") or {}
    ear = d.get("earnings") or {}
    gov = d.get("governance") or {}
    pitch = d.get("pitch") or {}
    facts = _kv([
        ("Ticker", _esc(d.get("ticker"))),
        ("Market cap", _money(d.get("market_cap"))),
        ("Sector / industry", _esc(" / ".join(x for x in [o.get("sector"), o.get("industry")] if x))),
        ("Vulnerability score", f'{d.get("vuln")} / 92' if d.get("vuln") is not None else "—"),
        ("1-yr return vs S&P", (f'{tsr.get("gap") * 100:+.0f} pts'
                                    if tsr.get("gap") is not None else "—")),
        ("3-yr return vs S&P", (f'{tsr.get("gap_3y") * 100:+.0f} pts'
                                    if tsr.get("gap_3y") is not None else "—")),
        ("Next earnings", _date(ear.get("next_date"))),
        ("Next annual meeting", _date(gov.get("annual_meeting_date"))),
        ("First flagged", _date(d.get("first_flagged"))),
    ])
    desc = f'<p class="hint">{_esc(o.get("description"))}</p>' if o.get("description") else ""
    sig = f'<p class="verdict" style="font-size:15px;">{_esc(d.get("signals"))}</p>' if d.get("signals") else ""

    situation = ""
    if d.get("active_situation"):
        sm = d.get("situation_meta") or {}
        sm_date = f" ({_date(sm.get('date'))})" if sm.get("date") else ""
        situation = (f'<div class="caveat" style="background:#f4e4e2;border-color:#e3c3bd;color:var(--hot);">'
                    f'&#9888; Activist already engaged — {_esc(sm.get("label") or "confirmed situation")}'
                    f'{sm_date}</div>')

    thesis = f'<div class="verdict">{_esc(pitch.get("thesis"))}</div>' if pitch.get("thesis") else ""
    pts = "".join(f'<li><span class="num">{i}</span><span>{_esc(p)}</span></li>'
                  for i, p in enumerate(pitch.get("points") or [], 1))
    pts_html = f'<ul class="pitch-points">{pts}</ul>' if pts else ""
    approaches_html = _prior_approaches_table(d.get("prior_approaches") or [])

    return _section("Overview", f"{desc}{facts}{situation}{sig}{thesis}{pts_html}{approaches_html}")


def _prior_approaches_table(rows):
    """Curated prior-M&A-approach table (item 8) -- see prior_approaches.py. Empty (not even a
    'none on record' placeholder) when this company isn't one of the handful covered, since a
    curated, sparse dataset saying "none" for 1,490 of 1,500 names would read as a finding it
    isn't -- absence here means "not yet sourced," never "checked, and there isn't one."""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda a: a.get("date") or "", reverse=True)
    trs = "".join(
        f'<tr><td>{_date(a.get("date"))}</td><td>{_esc(a.get("description"))}</td>'
        f'<td>{_money(a.get("amount"))}</td><td>{_esc((a.get("status") or "").title())}</td></tr>'
        for a in rows)
    table = (f'<table class="plain"><thead><tr><th>Date</th><th>Transaction</th><th>Amount</th>'
            f'<th>Status</th></tr></thead><tbody>{trs}</tbody></table>')
    src = next((a.get("source_url") for a in rows if a.get("source_url")), None)
    src_html = (f'<p class="evsrc">Source: <a href="{_esc(src)}">curated, manually sourced</a></p>'
               if src else '<p class="evsrc">Source: curated, manually sourced</p>')
    return f'<h3 class="sub">Prior M&amp;A approaches on record</h3>{table}{src_html}'


# --- 2. Evidence -------------------------------------------------------------------------------
def _ev_card(e):
    val = f'<span class="chip mid" style="margin-left:8px;">{_esc(e.get("value"))}</span>' if e.get("value") else ""
    ctx = f'<p style="margin:6px 0 0;">{_esc(e.get("context"))}</p>' if e.get("context") else ""
    bits = [_esc(x) for x in [e.get("inputs"), e.get("period")] if x]
    math_line = f'<p class="small" style="margin:4px 0 0;">{" &middot; ".join(bits)}</p>' if bits else ""
    src = ""
    if e.get("url"):
        src = f'<p class="evsrc" style="margin:6px 0 0;">Source: <a href="{_esc(e["url"])}">{_esc(e.get("source") or "source")}</a></p>'
    elif e.get("source"):
        src = f'<p class="evsrc" style="margin:6px 0 0;">Source: {_esc(e["source"])}</p>'
    return (f'<div class="item"><b>{_esc(e.get("label"))}</b>{val}{ctx}{math_line}{src}</div>')


def _evidence(d):
    ev = d.get("evidence") or []
    if not ev:
        return _section("Evidence", '<p class="hint">No evidence cards on record for this company.</p>')
    body = f'<div class="panel">{"".join(_ev_card(e) for e in ev)}</div>'
    return _section("Evidence", body,
                    subnote="Every triggered signal, with the underlying figures and source filing.")


# --- 3. Financials -----------------------------------------------------------------------------
_FIN_ROWS = [
    ("revenue", "Revenue", "money"), ("revenue_growth", "Revenue growth", "pct"),
    ("operating_margin", "Operating margin", "pct"), ("sga_pct", "SG&A % of revenue", "pct"),
    ("roa", "Return on assets", "pct"), ("return_on_equity", "Return on equity", "pct"),
    ("cash_to_assets", "Cash / assets", "pct"), ("debt_to_assets", "Debt / assets", "pct"),
    ("pe_ratio", "P/E", "num"), ("pb_ratio", "Price / book", "num"),
    ("dividend_yield", "Dividend yield", "pct"), ("dividend_status", "Dividend status", "raw"),
    ("analyst_target", "Analyst target price", "money"),
    ("week52_low", "52-week low", "money"), ("week52_high", "52-week high", "money"),
]
_FMT = {"money": _money, "pct": _pct, "num": _num, "raw": lambda v: _esc(v) if v else "—"}


def _financials(d):
    fin = d.get("financials") or {}
    fctx = {c.get("key"): c for c in (d.get("fin_context") or [])}
    rows = []
    for key, label, kind in _FIN_ROWS:
        v = fin.get(key)
        if v is None and key not in fctx:
            continue
        val = _FMT[kind](v)
        if key == "dividend_yield" and fin.get("dividend_yield_uncertain"):
            val += ' <span class="chip opp">unconfirmed</span>'
        c = fctx.get(key)
        verdict_html = ""
        if c and c.get("verdict") in ("bad", "opp"):
            cls = "bad" if c["verdict"] == "bad" else "opp"
            lab = "vulnerability" if cls == "bad" else "opportunity"
            verdict_html = f'<span class="chip {cls}">{lab}</span>'
        rows.append(f'<tr><td>{_esc(label)}</td><td>{val}</td><td>{verdict_html}</td></tr>')
    if not rows:
        return _section("Financials", '<p class="hint">No financial data on record.</p>')
    table = (f'<table class="plain"><thead><tr><th>Metric</th><th>Value</th><th>vs. peers</th></tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table>')
    return _section("Financials", table,
                    subnote="Peer verdicts are relative to the company's own self-selected comp set.")


# --- 4. Peer analysis --------------------------------------------------------------------------
def _peers(d):
    pa = d.get("peer_analysis")
    if not pa or not pa.get("peers"):
        return _section("Peer analysis", '<p class="hint">No peer comparison available for this company.</p>')
    s = pa.get("self") or {}
    med = pa.get("median") or {}
    rank_line = ""
    if pa.get("rank") and pa.get("rank_of"):
        rank_line = f'<p class="hint">Ranks {pa["rank"]} of {pa["rank_of"]} by 1-yr return among its self-selected peers.</p>'
    head = ('<tr><th>Company</th><th>1-yr return</th><th>Op. margin</th><th>P/B</th><th>EV/EBITDA</th></tr>')

    def row(tk, name, r, bold=False):
        style = ' style="font-weight:700;"' if bold else ""
        return (f'<tr{style}><td>{_esc(tk)} &middot; {_esc(name)}</td>'
                f'<td>{_pct(r.get("tsr_1y"), 0)}</td><td>{_pct(r.get("operating_margin"), 0)}</td>'
                f'<td>{_num(r.get("pb_ratio"))}</td><td>{_num(r.get("ev_ebitda"), 1)}</td></tr>')

    rows = [row(s.get("ticker"), s.get("name") or "This company", s, bold=True)]
    rows += [row(p.get("ticker"), p.get("name"), p) for p in pa["peers"]]
    rows.append(row("—", f"Peer median ({pa.get('n')} names)", med))
    table = f'<table class="plain"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table>'
    return _section("Peer analysis", f"{rank_line}{table}")


# --- 5. Ownership & insiders --------------------------------------------------------------------
def _ownership(d):
    ins = d.get("insider") or {}
    votes = d.get("votes") or {}
    holders = d.get("holders") or []
    ins_kv = _kv([
        ("Insider buying", _money(ins.get("buy_value"))),
        ("Insider selling", _money(ins.get("sell_value"))),
        ("Net", _money(ins.get("net_value"))),
        ("Buyers / sellers", f'{ins.get("n_buyers") or 0} / {ins.get("n_sellers") or 0}'),
        ("Window", f'{ins.get("window_days")} days' if ins.get("window_days") else "—"),
        ("Say-on-pay support", _pct(votes.get("say_on_pay"), 0)),
    ])
    holders_html = ""
    if holders:
        rows = "".join(
            f'<tr><td>{_esc(h.get("fund"))}</td><td>{_pct(h.get("ownership_pct"), 1)}</td>'
            f'<td>{_pct(h.get("weight_in_fund"), 1)}</td><td>{_money(h.get("value"))}</td>'
            f'<td>{_date(h.get("filed"))}</td></tr>' for h in holders)
        holders_html = (f'<h3 class="sub">13F holders</h3>'
                        f'<table class="plain"><thead><tr><th>Fund</th><th>% owned</th>'
                        f'<th>% of fund</th><th>Value</th><th>Filed</th></tr></thead>'
                        f'<tbody>{rows}</tbody></table>')
    else:
        holders_html = '<h3 class="sub">13F holders</h3><p class="hint">No 13F holders on record.</p>'
    return _section("Ownership & insiders", f"{ins_kv}{holders_html}")


# --- 6. Advisors ---------------------------------------------------------------------------------
def _advisors(d):
    adv = d.get("advisors") or {}
    firms = adv.get("firms") or []
    ct = d.get("contacts") or {}
    banks = [f for f in firms if f.get("type") == "bank"]
    laws = [f for f in firms if f.get("type") == "law"]
    comms = ct.get("comms_name")
    if not banks and not laws and not comms:
        return _section("Advisors", '<p class="hint">No advisors identified yet from this company\'s recent SEC filings.</p>')
    parts = []
    if banks:
        parts.append('<h3 class="sub">Financial advisors</h3><ul>' +
                     "".join(f"<li>{_esc(b.get('name'))}</li>" for b in banks) + "</ul>")
    if laws:
        parts.append('<h3 class="sub">Legal advisors</h3><ul>' +
                     "".join(f"<li>{_esc(l.get('name'))}</li>" for l in laws) + "</ul>")
    if comms:
        parts.append(f'<h3 class="sub">Incumbent comms advisor</h3><p>{_esc(comms)} '
                     f'<span class="small">(a competitor, not an outreach target)</span></p>')
    src = adv.get("source_url")
    if src:
        src_date_suffix = f" &middot; {_date(adv.get('source_date'))}" if adv.get("source_date") else ""
        parts.append(f'<p class="evsrc">Source: <a href="{_esc(src)}">SEC filing'
                     f'{src_date_suffix}</a></p>')
    return _section("Advisors", "".join(parts),
                    subnote="Who's already advising this company, from its own recent SEC filings — landscape intel, not an outreach list.")


# --- 7. Filings & news ----------------------------------------------------------------------------
def _filings_news(d):
    filings = d.get("filings") or []
    news = d.get("news") or []
    parts = []
    if filings:
        rows = "".join(
            f'<tr><td>{_date(f.get("filed_at"))}</td><td>{_esc(f.get("form"))}</td>'
            f'<td><a href="{_esc(f.get("url"))}">{_esc(f.get("title"))}</a></td>'
            f'<td class="small">{_esc(f.get("signals"))}</td></tr>' for f in filings)
        parts.append(f'<h3 class="sub">Recent SEC filings</h3>'
                    f'<table class="plain"><thead><tr><th>Date</th><th>Form</th><th>Title</th>'
                    f'<th>Signals</th></tr></thead><tbody>{rows}</tbody></table>')
    else:
        parts.append('<h3 class="sub">Recent SEC filings</h3><p class="hint">None on record.</p>')
    if news:
        rows = "".join(
            f'<div class="item"><a href="{_esc(n.get("url"))}">{_esc(n.get("headline"))}</a>'
            f'<div class="meta">{_esc(n.get("source"))} &middot; {_date(n.get("published_at"))}</div></div>'
            for n in news)
        parts.append(f'<h3 class="sub">Recent headlines</h3><div class="panel">{rows}</div>')
    else:
        parts.append('<h3 class="sub">Recent headlines</h3><p class="hint">None on record.</p>')
    return _section("Filings & news", "".join(parts))


# --- 8. Notes ------------------------------------------------------------------------------------
def _notes(d):
    note = d.get("note")
    if not note:
        return _section("Notes", '<p class="hint">No notes on file for this company.</p>')
    return _section("Notes", f'<div class="notebox">{_esc(note)}</div>')


def render_html(d):
    """d: the exact dict main.api_company()/`_company_payload()` returns. Pure string-building,
    no DB access -- same discipline as report.render_html()."""
    company = _esc(d.get("company") or d.get("ticker") or "Company profile")
    ticker = _esc(d.get("ticker") or "")
    sections = [
        _overview(d), _evidence(d), _financials(d), _peers(d),
        _ownership(d), _advisors(d), _filings_news(d), _notes(d),
    ]
    body = "".join(f'<div class="wrap">{s}</div>' for s in sections)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{company} — Activist Vulnerability profile</title>
<style>{_report._CSS}{_EXTRA_CSS}</style></head><body>
<header><span class="brand">Activist Vulnerability Dashboard</span>
<h1>{company}{f' <span style="color:var(--muted);font-size:20px;">&middot; {ticker}</span>' if ticker else ""}</h1>
<div class="sub">Company profile — generated for offline review</div></header>
{body}
</body></html>"""
