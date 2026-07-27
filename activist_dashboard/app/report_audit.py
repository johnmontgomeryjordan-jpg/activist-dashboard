"""
Auto-audit for the biweekly report — the QA gate that runs at GENERATION (Tuesday night) and
decides whether the Wednesday 7 AM send goes out automatically or is HELD for review. It checks
the four things that must be right before a report reaches FGS partners:

  1. theses are correct      — no AI over-claim vs the signals that actually fired
                               (reuses credibility.py's self-audit, scoped to this issue's board)
  2. financials are correct  — every board card shows real metric values; the debt/interest
                               sanity (debt-free vs. missed tag) from credibility.py
  3. headlines are relevant  — each headline tied to a featured name is on-topic and correctly
                               tagged (catches the Rollins->WEX / Tesla->GM mis-tag class)
  4. filings are tagged right — each filing of note carries a recognized 8-K classification

Returns {'status': 'clean'|'held', 'flags': [...], 'summary': str}. A HIGH-severity flag HOLDS the
send (and the Wed job alerts the admin instead of the list); MED/LOW are advisory and don't block.

Pure-ish and unit-testable: it takes the already-assembled model plus the credibility module, so it
needs no live DB of its own.
"""
import re

# Markers in an AI gloss that betray an off-topic / mis-tagged headline — the summariser itself
# admits the story is about a different company (the WEX case: "...references Rollins, but the
# context identifies WEX...").
_MISTAG_GLOSS = re.compile(
    r"cannot provide|different entit|different compan|unrelated to|does not (?:appear to )?"
    r"(?:relate|pertain|refer)|references .+ (?:not|but)|is about .+ not ",
    re.I)

# 8-K classifications edgar.classify() can emit — a "filing of note" should carry one of these.
_KNOWN_SIGNALS = {"restatement", "ceo_departure", "earnings_miss", "impairment",
                  "layoffs", "leadership_change", "results_update"}

_SEV_RANK = {"HIGH": 0, "MED": 1, "LOW": 2}


def _flag(flags, sev, check, subject, detail):
    flags.append({"severity": sev, "check": check, "subject": subject, "detail": detail})


def _headline_mentions(head, ticker, company):
    """True if the headline plausibly refers to the tagged company — the ticker as an UPPERCASE
    whole word, or a significant word from the company name. Guards against a story tagged to the
    wrong name. The ticker match is case-SENSITIVE (real tickers are written upper-case), so a
    title-cased surname or common word like "…Kumar Dash" can't mask a mis-tag to DASH."""
    raw = head or ""
    if ticker and re.search(rf"\b{re.escape(ticker.upper())}\b", raw):   # case-sensitive: 'DASH' not 'Dash'
        return True
    hl = raw.lower()
    for w in re.split(r"[^A-Za-z]+", company or ""):
        if len(w) >= 4 and w.lower() in hl:      # first meaningful company token (skip Inc/Co/&)
            return True
    return False


def audit(model, *, credibility=None):
    """Audit an assembled report model; see module docstring for the four checks."""
    flags = []
    board = model.get("board") or []
    board_tks = {(c.get("ticker") or "").upper() for c in board if c.get("ticker")}

    # 1) THESES + FINANCIALS — reuse the credibility self-audit, scoped to this issue's board.
    if credibility is not None:
        try:
            for f in (credibility.run_checks() or []):
                tk = str(f.get("ticker") or "").upper()
                if tk in board_tks:
                    _flag(flags, f.get("severity", "MED"),
                          f"credibility: {f.get('check')}", tk, f.get("detail") or "")
        except Exception as e:                                   # never let the gate crash the run
            _flag(flags, "MED", "credibility check errored", "-", str(e))

    # 2) Per-card content + catalyst sanity.
    for c in board:
        tk = (c.get("ticker") or "?").upper()
        if not (c.get("thesis") or "").strip():
            _flag(flags, "HIGH", "empty thesis", tk, "board card has no thesis text")
        if not (c.get("points") or []):
            _flag(flags, "MED", "no pitch points", tk, "board card has no supporting points")
        if not [m for m in (c.get("metrics") or []) if m.get("value") not in (None, "", "—")]:
            _flag(flags, "MED", "no financials", tk, "board card shows no metric values")
        mag = (c.get("catalyst") or {}).get("magnitude")
        if mag is not None:
            try:
                if abs(float(mag)) >= 0.50:      # a >50% one-day "reaction" is almost always an
                    _flag(flags, "MED", "implausible catalyst move", tk,   # earnings-day move mis-
                          f"catalyst reaction {float(mag) * 100:+.0f}% on the day — verify it isn't "
                          f"an earnings-day move mis-attributed to the event")
            except (TypeError, ValueError):
                pass

    # 3) HEADLINE relevance / mis-tag — trust the gloss's own admission, and require the headline
    #    to actually mention the tagged company. A mis-tag on a FEATURED name is a HIGH (it lands
    #    in front of partners); off-board it's advisory.
    for h in (model.get("headlines") or []):
        tk = (h.get("ticker") or "").upper()
        if not tk:
            continue
        head, co, summ = h.get("headline") or "", h.get("company") or "", h.get("summary") or ""
        gloss_mismatch = bool(summ and _MISTAG_GLOSS.search(summ))
        no_mention = not _headline_mentions(head, tk, co)
        if gloss_mismatch or no_mention:
            sev = "HIGH" if h.get("on_board") else "MED"
            why = ("AI gloss flags a company mismatch" if gloss_mismatch
                   else f"headline doesn't mention {tk} or “{co}”")
            _flag(flags, sev, "headline may be mis-tagged", tk, f"{why}: “{head[:90]}”")

    # 4) FILING tags — every filing of note should carry a recognized classification.
    for f in (model.get("filings") or []):
        sigs = {s.strip() for s in (f.get("signals") or "").split(",") if s.strip()}
        if not (sigs & _KNOWN_SIGNALS):
            _flag(flags, "MED", "filing unclassified", (f.get("ticker") or "-").upper(),
                  f"“{(f.get('title') or '')[:70]}” has no recognized signal "
                  f"({f.get('signals') or 'none'})")

    held = any(f["severity"] == "HIGH" for f in flags)
    flags.sort(key=lambda x: (_SEV_RANK.get(x["severity"], 9), x["check"]))
    n = {s: sum(1 for f in flags if f["severity"] == s) for s in ("HIGH", "MED", "LOW")}
    summary = ("clean — safe to send" if not flags else
               f"{'HELD' if held else 'clean'} — {len(flags)} flag(s): "
               f"{n['HIGH']}H/{n['MED']}M/{n['LOW']}L")
    return {"status": "held" if held else "clean", "flags": flags, "summary": summary}
