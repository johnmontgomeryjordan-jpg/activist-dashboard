"""
Catalyst engine — pick the single most material RECENT catalyst per company.

The daily screen scores fundamentals; the biweekly report needs to lead each name with
the *event* that puts it in play right now — "restatement filed Jun 17," "CEO out, stock
−28% on the day," "guidance cut." Those events already live in the `filings` table
(edgar.py classifies every 8-K into signal keys with a filed date + source URL) and, for
leadership 8-Ks, in `exec_reactions` (reaction.py's 1-day abnormal move). This module
selects the driving catalyst from that data and writes a dated, factual, source-linked
one-liner — nothing invented, everything traceable to a filing.

Two jobs, one selector:
  * REPORT — the chosen catalyst is the "Fresh catalyst" line on a board card.
  * INTAKE — a strong catalyst on a name NOT yet covered is the front door to the pipeline:
    it flags the name so a profile gets built (the IBM case, automated).

Pure + deterministic: `pick_catalyst()` takes plain data (a list of filing dicts + an
optional reaction dict) so it is trivially unit-testable and has no DB import.
"""
from datetime import datetime, timedelta

# A catalyst counts if its filing is within this window. The report is fortnightly, but a
# driving event (a restatement, a guidance cut, a CEO exit) stays relevant across a couple
# of reporting cycles, so we look back a fiscal quarter rather than just 14 days.
FRESH_DAYS = 90

# Catalyst kinds ranked by activist relevance, most → least. These are exactly the signal
# keys edgar.classify() emits, so the engine reads the stored `signals` field directly.
# NOTE: generic "results_update" is deliberately NOT a catalyst — a routine earnings release
# isn't an opening. An earnings *miss* (earnings_miss) is; a plain result is note-only, so a
# name whose only recent event is a routine 8-K shows NO catalyst line rather than a weak one.
_KIND_RANK = [
    "restatement",        # non-reliance 8-K — accounting-integrity failure, the strongest
    "ceo_departure",      # CEO / CFO / COO / President out
    "earnings_miss",      # miss / guidance cut (2.02 with miss language)
    "impairment",         # goodwill / asset write-down
    "layoffs",            # restructuring / exit costs
    "leadership_change",  # lesser officer change (GC / other)
]
_RANK = {k: i for i, k in enumerate(_KIND_RANK)}

_LABEL = {
    "restatement": "Restatement / non-reliance",
    "ceo_departure": "CEO / executive departure",
    "earnings_miss": "Earnings miss / guidance cut",
    "impairment": "Material impairment",
    "layoffs": "Restructuring / layoffs",
    "leadership_change": "Leadership change",
    "results_update": "Results",
}

# A move from exec_reactions is only meaningful for the leadership events reaction.py
# actually measures (it computes the 1-day abnormal move around a leadership 8-K).
_MOVE_KINDS = {"ceo_departure", "leadership_change"}

# Results/earnings signals. reaction.py measures the raw 1-day move around a leadership 8-K, but if
# a RESULTS/earnings 8-K landed the SAME day, that move is driven by the print, not the leadership
# change — so it must NOT be attributed to the transition. Shake Shack is the case: a CFO
# appointment filed alongside the quarter's results, stock ~-28% that day; the -28% was the earnings
# reaction, not "the market losing confidence in the new CFO." When a results signal is present on
# the same filing day, we drop the move from the leadership catalyst and note it ran alongside
# results, so the report never claims a false reaction to the leadership event.
_RESULTS_SIGNALS = {"results_update", "earnings_miss"}


def _has_results_signal(sig_csv):
    return any(s.strip() in _RESULTS_SIGNALS for s in (sig_csv or "").split(","))

# Catalysts strong enough to pull a NOT-yet-covered name into the pipeline for a profile.
_INTAKE_KINDS = {"restatement", "ceo_departure", "earnings_miss", "impairment"}


def _d(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _fmt_date(s):
    dt = _d(s)
    if not dt:
        return s or ""
    # Cross-platform "Jul 13, 2026" without %-d (Windows lacks it; Render is Linux but be safe).
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _best_signal(sig_csv):
    """Highest-priority catalyst signal in a filing's comma-joined `signals` field."""
    keys = [s.strip() for s in (sig_csv or "").split(",") if s.strip() in _RANK]
    if not keys:
        return None
    return min(keys, key=lambda k: _RANK[k])


def _sentence(kind, when, move, alongside_results=False):
    """Dated, factual one-liner for the report card. `move` is a signed 1-day return or None.
    `alongside_results` is True when a leadership event shared its filing day with the quarter's
    results — in which case we never attribute the day's move to the transition, and say so."""
    move_txt = f" — the stock moved {move * 100:+.0f}% on the day" if move is not None else ""
    # For a leadership event filed the same day as results, replace the (suppressed) move clause
    # with an honest note so the reader knows the day's move isn't a clean read on the transition.
    when_txt = (f"{when} (alongside the quarter's results)"
                if (alongside_results and kind in _MOVE_KINDS) else when)
    if kind == "restatement":
        return (f"Filed a non-reliance / restatement 8-K on {when} — an accounting-integrity "
                f"failure that puts management and the audit committee squarely in play.")
    if kind == "ceo_departure":
        return (f"A C-suite departure disclosed {when_txt}{move_txt} — a leadership transition an "
                f"activist engages on.")
    if kind == "earnings_miss":
        return (f"An earnings miss / guidance cut disclosed {when} — a frustrated shareholder "
                f"base and a natural moment to press for change.")
    if kind == "impairment":
        return (f"A material impairment / write-down disclosed {when} — the market re-rating "
                f"prior capital allocation.")
    if kind == "layoffs":
        return (f"A restructuring / exit-cost action disclosed {when} — management already "
                f"conceding the cost base needs work.")
    if kind == "leadership_change":
        return f"A leadership change disclosed {when_txt}{move_txt}."
    return f"Results filed {when}."


def pick_catalyst(filings, reaction=None, today=None):
    """Select the driving catalyst from a company's recent filings.

    filings : list of dicts with keys {filed_at, title, url, signals, form}
              (exactly the rows database.get_filings_by_cik() returns).
    reaction: optional dict {move, abnormal, event_date} from exec_reactions, attached
              when the catalyst is a leadership event.
    Returns a catalyst dict, or None when nothing recent qualifies.
    """
    today = today or datetime.utcnow()
    cutoff = today - timedelta(days=FRESH_DAYS)

    best = None  # (rank, age_days, filing, kind)
    for f in filings or []:
        fd = _d(f.get("filed_at"))
        if fd is None or fd < cutoff:
            continue
        kind = _best_signal(f.get("signals"))
        if kind is None:
            continue
        cand = (_RANK[kind], (today - fd).days, fd, f, kind)
        # Lower rank wins; ties broken by smaller age (more recent).
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand
    if best is None:
        return None

    _, _, fd, f, kind = best
    # Confound guard: if ANY filing on the leadership event's day (including that 8-K itself, when
    # it bundles Item 5.02 + Item 2.02) carries a results/earnings signal, the 1-day move reflects
    # the print, not the transition — so we don't attribute it. (Shake Shack: CFO 8-K + results 8-K
    # same day, stock -28% on the earnings, not the CFO.)
    alongside_results = kind in _MOVE_KINDS and any(
        _d(g.get("filed_at")) == fd and _has_results_signal(g.get("signals"))
        for g in (filings or [])
    )
    move = None
    if reaction and kind in _MOVE_KINDS and not alongside_results:
        mv = reaction.get("move")
        if mv is not None:
            try:
                move = float(mv)
            except (ValueError, TypeError):
                move = None

    when = _fmt_date(f.get("filed_at"))
    return {
        "kind": kind,
        "label": _LABEL.get(kind, "Material event"),
        "date": f.get("filed_at"),
        "date_pretty": when,
        "url": f.get("url"),
        "form": f.get("form"),
        "magnitude": move,
        "intake_worthy": kind in _INTAKE_KINDS,
        "sentence": _sentence(kind, when, move, alongside_results=alongside_results),
    }


def is_intake_worthy(cat):
    """True when a catalyst is strong enough to flag a non-covered name for a profile build."""
    return bool(cat) and cat.get("kind") in _INTAKE_KINDS


def for_company(cik, database, today=None):
    """Convenience wrapper: fetch a company's filings + reaction from the DB and pick.
    Kept import-light (database passed in) so the core selector stays pure/testable."""
    try:
        filings = database.get_filings_by_cik(cik, limit=25)
    except Exception:
        filings = []
    reaction = None
    getter = getattr(database, "get_reaction", None)
    if getter:
        try:
            reaction = getter(cik)
        except Exception:
            reaction = None
    return pick_catalyst(filings, reaction=reaction, today=today)
