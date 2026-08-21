"""
Pitch prose generator — turns a company's firing signals + values into the activist
THESIS and the TALKING POINTS shown on the pitch kit and the company profile.

Deterministic and template-based on purpose: every sentence is assembled from facts we
already computed (the same numbers behind the Evidence tab), so nothing is invented and
every claim traces to a filing or metric. An optional LLM polish layer can be layered on
top later (it would only re-voice this grounded text, never add facts).

build_pitch() picks an ARCHETYPE from the signal mix -- the campaign an activist would
most plausibly run -- and fills a full-sentence template for it, so grammar stays clean.
Talking points are the strongest individual signals, each reframed as a "so what."
"""

# ---- formatting helpers -----------------------------------------------------
def _money(v):
    if v is None:
        return None
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.1f}T"
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.0f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _money_precise(v):
    """Like _money but keeps one decimal for M/B — used for exact figures like CEO pay."""
    if v is None:
        return None
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _pct(v, dp=0):
    return None if v is None else f"{v * 100:.{dp}f}%"


def _drop(tsr):
    """A negative 1-yr return as a clean 'fallen N%' magnitude."""
    if tsr is None or tsr >= 0:
        return None
    return f"{abs(tsr) * 100:.0f}%"


def _gap_pts(tsr, spy):
    if tsr is None or spy is None:
        return None
    return f"{round((spy - tsr) * 100)}"


def _has(trig, *keys):
    return any(k in trig for k in keys)


def _gov_phrase(trig):
    if "gov_classified" in trig:
        return " behind a staggered board"
    if "gov_poison" in trig:
        return " shielded by a poison pill"
    if "gov_dual" in trig:
        return " with insiders controlling the vote through super-voting stock"
    return ""


def _gov_list(trig):
    parts = []
    if "gov_classified" in trig:
        parts.append("a staggered board")
    if "gov_poison" in trig:
        parts.append("a poison pill")
    if "gov_dual" in trig:
        parts.append("super-voting insider stock")
    if not parts:
        return "entrenchment provisions"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _catalyst_sentence(trig):
    """An optional extra sentence about a fresh, time-sensitive opening."""
    if "restatement" in trig:
        return "A recent financial restatement puts management's credibility — and the audit committee — squarely in play."
    if "exec_reaction_drop" in trig:
        return "The stock sold off on a recent leadership-change filing — the market has already lost confidence."
    if "ceo_departure" in trig:
        return "A recent C-suite departure — a leadership transition worth engaging on."
    if "weak_vote_support" in trig:
        return "Shareholders are already revolting on executive pay."
    if "overpaid_ceo" in trig:
        return "CEO pay has climbed even as the stock lagged — a ready-made pay-for-performance attack."
    if "insider_selling" in trig:
        return "Insiders have been selling into the weakness."
    if "earnings_miss" in trig:
        return "A recent earnings miss has the shareholder base frustrated."
    return ""


def _acq_rerate(r, trig):
    """Recent transformative acquisition the market has re-rated lower: a goodwill-heavy
    balance sheet + very high revenue growth (the tell of a big DEAL, not organic growth —
    organic growers carry little goodwill) + a sharp 1-yr de-rate. The AVAV/BlueHalo pattern,
    where the deal compressed margins, ballooned goodwill, and the stock fell hard."""
    g = r.get("revenue_growth")
    t1 = r.get("tsr_1y")
    return ("high_goodwill" in trig
            and g is not None and g > 0.50
            and ((t1 is not None and t1 <= -0.20) or "weak_tsr_1y" in trig))


def _acq_rerate_point(r):
    pct = _pct(r.get("goodwill_to_assets"))
    d = _drop(r.get("tsr_1y"))
    lead = f"De-rated {d} over the past year" if d else "Sharply de-rated"
    gw = f" (goodwill now {pct} of assets)" if pct else ""
    return (f"{lead} since a large, goodwill-heavy acquisition{gw} — the market re-rating a "
            f"deal it views as value-destructive, a ready-made 'reverse the M&A' angle.")


def _perf_phrase(r, trig):
    """The headline weakness, picked by what's most striking."""
    drop = _drop(r.get("tsr_1y"))
    gap = _gap_pts(r.get("tsr_1y"), r.get("_spy_1y"))
    if "weak_tsr_1y" in trig and drop and gap:
        return f"has fallen {drop} over the past year, trailing the market by {gap} points"
    if _has(trig, "cheap_abs", "cheap_pb") and r.get("pb_ratio"):
        return f"trades at just {r['pb_ratio']:.1f}x book"
    if "low_margin" in trig:
        return "runs bottom-quartile operating margins"
    if "low_roa" in trig:
        return "earns negative returns on its asset base"
    if "weak_growth" in trig:
        return "has a stalling top line"
    return "screens poorly against its sector peers"


# ---- archetype selection ----------------------------------------------------
def _archetype(trig):
    cash = "cash_hoard" in trig
    weakperf = _has(trig, "weak_tsr_1y", "low_margin", "low_roa", "weak_growth")
    cheap = _has(trig, "cheap_abs", "cheap_pb")
    gov = _has(trig, "gov_classified", "gov_poison", "gov_dual")
    if cash and (weakperf or cheap):
        return "cash_laggard"
    # A sharp de-rating (>=30% down on a still-viable business) is a sale / strategic-review story,
    # NOT an operational "cut costs" turnaround — even when thin margins also trip the turnaround
    # test. AECOM (down ~44% while operating income actually GREW) was mis-framed as a margin
    # turnaround; the de-rating is the real story, so strategic_review outranks turnaround. (#5)
    if "strategic_review" in trig:
        return "strategic_review"
    if "low_margin" in trig and _has(trig, "high_sga", "low_roa"):
        return "turnaround"
    if cheap:
        return "value"
    if gov and weakperf:
        return "governance"
    return "default"


def _holder_hook(r):
    """13F early-warning hook: a known activist already holds a material stake but hasn't gone
    active. The strongest possible opener — the diligence is done and the capital is committed,
    so it's a warm introduction, not a cold pitch."""
    hs = r.get("_holders") or []
    if not hs:
        return ""
    h = hs[0]
    fund = (h.get("fund") or "A known activist").title()
    if h.get("ownership_pct") is not None:
        stake = f"~{h['ownership_pct'] * 100:.1f}% of the shares"
    elif h.get("weight_in_fund") is not None:
        stake = f"a position worth {h['weight_in_fund'] * 100:.1f}% of its own 13F book"
    else:
        stake = "a disclosed position"
    others = sorted({(x.get("fund") or "").title() for x in hs if x.get("fund")} - {fund})
    extra = ""
    if others:
        verb = "is" if len(others) == 1 else "are"
        extra = f" ({', '.join(others)} {verb} also on the register.)"
    return (f"{fund} already owns {stake} here per its latest 13F but has not yet gone active — "
            f"the capital is committed and the diligence is done. This is a warm introduction, "
            f"not a cold call: bring the campaign thesis to an investor already in the stock.{extra}")


def _derate_caveat(r, trig):
    """4e — a diligence flag appended to a strategic-review thesis so the pitch never sells a
    sharp de-rating as a pure valuation gift. Two cases: (1) the top line is shrinking with
    the stock — a possible structural decline / falling knife, not a clean turnaround; (2) a
    50%+ collapse on a still-growing business — the market is pricing a forward risk the
    trailing numbers don't show (the Intuit pattern), so the moat/thesis must be tested."""
    if "strategic_review" not in trig:
        return ""
    raw = r.get("raw") or {}
    ani = raw.get("annual_net_income")
    rg = r.get("revenue_growth")
    t1 = r.get("tsr_1y")
    if rg is not None and rg < 0:
        return ("One caveat to diligence first: the top line is shrinking alongside the stock, "
                "so pressure-test whether this is a fixable de-rating or a structural decline "
                "before pitching a sale.")
    if ani is not None and ani < 0:
        # Full-year GAAP loss (usually a big impairment/write-down): the fundamentals are impaired,
        # not just the multiple — this is the case that must NOT read as a clean valuation gift.
        return ("One caveat to diligence first: the company posted a full-year GAAP loss — often a "
                "large impairment or write-down — so the fundamentals are impaired, not just the "
                "multiple. Diligence what drove the loss before pitching this as a clean valuation "
                "opportunity.")
    if t1 is not None and t1 <= -0.50:
        return ("One caveat to diligence first: a de-rating this sharp on a still-growing "
                "business means the market is pricing a forward risk the trailing numbers "
                "don't show — test the moat and guidance rather than assuming a valuation gift.")
    return ""


# ---- thesis -----------------------------------------------------------------
def _thesis(r, trig):
    name = r.get("name") or "The company"
    raw = r.get("raw") or {}
    cash = _money(raw.get("cash"))
    cash_pct = _pct(r.get("cash_to_assets"))
    pb = r.get("pb_ratio")
    perf = _perf_phrase(r, trig)
    gov = _gov_phrase(trig)
    extra = _catalyst_sentence(trig)
    arch = _archetype(trig)

    if arch == "cash_laggard" and cash and cash_pct:
        s1 = (f"{name} {perf}, yet sits on {cash} of cash ({cash_pct} of its assets)"
              f"{gov}.")
        s2 = ("A cash-rich laggard: the textbook setup for a return-of-capital or "
              "strategic-review campaign.")
    elif arch == "strategic_review":
        d = _drop(r.get("tsr_1y"))
        s1 = (f"{name} has de-rated sharply — down {d} over the past year{gov}."
              if d else f"{name} {perf}{gov}.")
        s2 = ("A sharp de-rating rather than an operational stumble — the classic setup for a "
              "sale, take-private, or strategic review, not a cut-costs turnaround.")
    elif arch == "turnaround":
        cost = "bloated overhead" if "high_sga" in trig else "weak returns on its assets"
        s1 = f"{name} {perf} with {cost}, well below its sector peers{gov}."
        s2 = "An operating-margin turnaround story a dissident can run on."
    elif arch == "value":
        pbtxt = f"trades at just {pb:.1f}x book" if pb else "trades cheap to book value"
        tail = ""
        if "weak_tsr_1y" in trig:
            d = _drop(r.get("tsr_1y"))
            if d:
                tail = f", down {d} over the past year"
        s1 = f"{name} {pbtxt}{tail}{gov}."
        s2 = ("A cheap asset relative to its peers — fertile ground for a value or "
              "break-up campaign.")
    elif arch == "governance":
        s1 = f"{name} {perf}, but its board is insulated by {_gov_list(trig)}."
        s2 = ("An entrenched, unaccountable board — a board-refresh or governance campaign "
              "waits to be made.")
    else:
        s1 = f"{name} {perf}, screening in the worst quartile of its sector on several measures."
        s2 = "A clear match for the profile activists target."

    parts = [s1]
    hook = _holder_hook(r)
    if hook:
        parts.append(hook)
    if _acq_rerate(r, trig):
        parts.append("The sharp de-rating since a large, goodwill-heavy acquisition reads as "
                     "the market's verdict on a value-destructive deal.")
    if extra:
        parts.append(extra)
    parts.append(s2)
    cav = _derate_caveat(r, trig)
    if cav:
        parts.append(cav)
    return " ".join(parts)


# ---- talking points ---------------------------------------------------------
def _point(key, r):
    raw = r.get("raw") or {}
    if key == "cash_hoard":
        cash, pct = _money(raw.get("cash")), _pct(r.get("cash_to_assets"))
        if cash and pct:
            return (f"Holds {cash} of cash ({pct} of assets) — idle capital an activist "
                    f"would push to return to shareholders.")
    if key == "low_margin":
        m = _pct(r.get("operating_margin"), 1)
        return f"Operating margin of {m or 'near zero'} sits in the bottom quartile of its sector — a clear cost-and-turnaround lever."
    if key == "low_roa":
        return f"Return on assets of {_pct(r.get('roa'),1) or 'below zero'} — the asset base simply isn't earning."
    if key == "high_sga":
        return f"SG&A runs {_pct(r.get('sga_pct')) or 'high'} of revenue, top-quartile — obvious overhead to cut."
    if key == "weak_tsr_1y":
        d, g = _drop(r.get("tsr_1y")), _gap_pts(r.get("tsr_1y"), r.get("_spy_1y"))
        if d and g:
            return f"Down {d} over the year and {g} points behind the market — a cheap, frustrated shareholder base."
        return "Has badly lagged the market over the past year — a frustrated shareholder base."
    if key == "lags_own_peers":
        pa = r.get("_peers") or {}
        rank, rof = pa.get("rank"), pa.get("rank_of")
        med = (pa.get("median") or {}).get("tsr_1y")
        st = (pa.get("self") or {}).get("tsr_1y")
        if rank and rof and st is not None and med is not None:
            return (f"Ranks {rank} of {rof} on 1-yr return against the peer group it chose itself "
                    f"({st * 100:+.0f}% vs the {med * 100:+.0f}% peer median) — underperformance by "
                    f"its own yardstick.")
        return "Trails the compensation peer group it selected itself — underperformance by its own yardstick."
    if key == "weak_tsr_3y":
        t3 = r.get("tsr_3y"); sp = r.get("_spy_3y")
        if t3 is not None and sp is not None:
            return (f"Price return of {t3 * 100:+.0f}% over three years vs the S&P's "
                    f"{sp * 100:+.0f}% — a structural, multi-year underperformer, not a one-year blip.")
        return "Has trailed the market for years — a structural underperformer, not a one-year blip."
    if key in ("cheap_abs", "cheap_pb"):
        pb = r.get("pb_ratio")
        return f"Trades at {pb:.1f}x book — a cheap entry point below its peer cutoff." if pb else "Trades cheap to book value — a cheap entry point."
    if key == "cheap_ev_ebitda":
        ev = r.get("ev_ebitda")
        return (f"Values at just {ev:.1f}x EV/EBITDA, bottom-quartile for its sector — a textbook undervaluation argument."
                if ev else "Trades at a bottom-quartile EV/EBITDA multiple — a textbook undervaluation argument.")
    if key == "high_goodwill":
        pct = _pct(r.get("goodwill_to_assets"))
        return (f"Goodwill is {pct} of assets — a stretched acquisition history an activist can attack as value-destructive M&A."
                if pct else "Goodwill dominates the balance sheet — a stretched acquisition history an activist can attack as value-destructive M&A.")
    if key == "weak_growth":
        return f"Revenue growth of {_pct(r.get('revenue_growth'),1) or 'near zero'} lags the sector — a stalling top line."
    if key == "underlevered":
        return "Carries little debt — an under-levered balance sheet an activist could push to releverage."
    if key == "gov_classified":
        return "A staggered board entrenches directors against change — a built-in governance angle."
    if key == "gov_poison":
        return "A poison pill blocks stake-building — itself a lightning rod for a 'just vote no' campaign."
    if key == "gov_dual":
        return "Super-voting insider stock concentrates control — a governance flashpoint."
    if key == "ceo_departure":
        return "A recent C-suite departure — a leadership transition to watch."
    if key == "insider_selling":
        return "A cluster of insider selling — management is voting with its feet."
    if key == "weak_vote_support":
        return "Weak say-on-pay support — shareholders are already signaling discontent."
    if key == "overpaid_ceo":
        c = r.get("_comp") or {}
        pct = c.get("pct_change")
        lt, ly = _money_precise(c.get("latest_total")), c.get("latest_year")
        if pct is not None and lt:
            return (f"CEO pay climbed {pct * 100:.0f}% to {lt} ({ly}) even as the stock lagged — "
                    f"a pay-for-performance gap that anchors a governance campaign.")
        return "CEO pay rose while shareholders lagged — a textbook pay-for-performance attack."
    if key == "exec_reaction_drop":
        rc = r.get("_reaction") or {}
        mv = rc.get("move")
        if mv is not None:
            return (f"The stock fell {abs(mv) * 100:.0f}% the day the leadership-change 8-K hit — "
                    f"a visible vote of no confidence in the transition.")
        return "The market sold off on the leadership-change announcement — a visible loss of confidence."
    if key == "restatement":
        return ("A financial restatement (non-reliance 8-K) — an accounting-integrity failure "
                "that hands an activist a board-accountability and audit-committee-refresh campaign.")
    if key == "earnings_miss":
        return "A recent earnings miss — a natural moment for a shareholder to press for change."
    if key == "divestiture":
        return ("A recent business/segment divestiture shows the board is already willing to "
                 "reshape the portfolio — a concrete opening for a sum-of-the-parts or "
                 "further-breakup case.")
    return None


# Order points by how compelling they are in a pitch (not raw score weight).
_POINT_PRIORITY = [
    "cash_hoard", "divestiture", "overpaid_ceo", "restatement", "exec_reaction_drop", "lags_own_peers",
    "weak_tsr_1y", "weak_tsr_3y", "cheap_ev_ebitda", "low_margin", "high_sga", "low_roa", "cheap_pb",
    "cheap_abs", "high_goodwill", "weak_growth", "gov_classified", "gov_poison", "gov_dual",
    "ceo_departure", "weak_vote_support", "insider_selling", "earnings_miss", "underlevered",
]


def _points(r, trig, n=3):
    out, seen = [], set()
    for key in _POINT_PRIORITY:
        if key in trig and key not in seen:
            p = _point(key, r)
            if p:
                out.append(p)
                seen.add(key)
        if len(out) >= n:
            break
    return out


# ---- public -----------------------------------------------------------------
def build_pitch(r, trig):
    """r: the scoring rec (name, raw, metrics, tsr, _spy_1y...). trig: firing signal keys.
    Returns {thesis, points, archetype}. Pure + deterministic."""
    trig = list(trig or [])
    pts = _points(r, trig)
    if _acq_rerate(r, trig):
        # Lead with the acquisition-rerate point and drop a plain goodwill point to avoid dup.
        pts = [p for p in pts if "goodwill" not in p.lower()]
        pts = ([_acq_rerate_point(r)] + pts)[:3]
    return {
        "thesis": _thesis(r, trig),
        "points": pts,
        "archetype": _archetype(trig),
    }
