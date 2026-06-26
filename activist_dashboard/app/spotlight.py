"""
Lead-of-the-day picker — the single backend source of truth for which company is the
daily spotlight. Mirrors the original client-side rotation so the site, the emailed pitch
kit, and (next) the spotlight force-pull all feature the SAME name.

Rotation: from the proactive shortlist (active situations excluded), prefer names with a
recent catalyst, rank by the 0-92 vulnerability index, take the top 8, and rotate by a
day index that flips at 6 AM ET (in step with the daily rebuild). The day index uses
days-since-epoch so it matches the frontend's Math.floor(Date.UTC(...)/86400000).
"""
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    _ET = None

_CATALYST = re.compile(r"recent (ceo|exec|earnings|results|impairment|layoff|leadership)", re.I)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def day_index():
    """Whole-day counter that advances at 6 AM ET (days since the Unix epoch)."""
    now = datetime.now(_ET) if _ET else datetime.utcnow()
    if now.hour < 6:
        now = now - timedelta(days=1)
    midnight_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return (midnight_utc - _EPOCH).days


def pick_lead(rows):
    """rows: score rows (get_scores already excludes active situations). Returns the lead
    row for today, or None. Deterministic for the day."""
    pool = [r for r in rows if not r.get("active_situation")]
    if not pool:
        return None
    def vk(r):
        return r.get("vuln") or 0
    # Catalyst names first, then padded with the highest-vuln names so the rotation pool is
    # always deep enough to vary day to day (a single catalyst name must not lock the lead).
    cats = sorted([r for r in pool if _CATALYST.search(r.get("signals") or "")], key=vk, reverse=True)
    rest = sorted([r for r in pool if not _CATALYST.search(r.get("signals") or "")], key=vk, reverse=True)
    top = (cats + rest)[:min(8, len(pool))]
    if not top:
        return None
    return top[day_index() % len(top)]


def lead_cik(rows):
    lead = pick_lead(rows)
    return lead.get("cik") if lead else None


# No name may be lead again until this many days have passed (subject to pool size).
NO_REPEAT_DAYS = 30


def _ordered(pool):
    """Pool ranked: catalyst names first (fresh angle), then the rest, each by vuln desc."""
    def vk(r):
        return r.get("vuln") or 0
    cats = sorted([r for r in pool if _CATALYST.search(r.get("signals") or "")], key=vk, reverse=True)
    rest = sorted([r for r in pool if not _CATALYST.search(r.get("signals") or "")], key=vk, reverse=True)
    return cats + rest


def choose_lead(pool, history):
    """history: {cik: most-recent day featured}. Prefer the highest-priority name that hasn't
    been featured in the window; if every candidate has (pool smaller than the window), fall
    back to the LEAST-recently-featured one — a round-robin, so it never sticks on one name."""
    ordered = _ordered(pool)
    if not ordered:
        return None
    fresh = [r for r in ordered if r.get("cik") not in history]
    if fresh:
        return fresh[0]                              # never featured in window -> top priority
    # all featured recently: pick the one featured longest ago (ties keep ranking order)
    return min(ordered, key=lambda r: history.get(r.get("cik"), -1))


def todays_lead(rows, db):
    """Backend source of truth for the lead of the day. Locks one pick per publish-day
    (so it's stable through the day and identical on the site and in the email), and won't
    repeat a name until the others have cycled through (up to NO_REPEAT_DAYS, capped by how
    many distinct leads exist)."""
    pool = [r for r in rows if not r.get("active_situation")]
    if not pool:
        return None
    by_cik = {r.get("cik"): r for r in pool}
    day = day_index()
    locked = db.get_lead_for_day(day)
    if locked and locked in by_cik:
        return by_cik[locked]                        # already chosen today
    history = db.get_lead_history(NO_REPEAT_DAYS, day)
    if not history:
        # First run with no rotation memory: the top-ranked name has effectively been the
        # de-facto lead, so seed it as "yesterday" — today's pick then rotates off it.
        top = _ordered(pool)[0]
        db.record_daily_lead(day - 1, top.get("cik"))
        history = db.get_lead_history(NO_REPEAT_DAYS, day)
    lead = choose_lead(pool, history)
    if lead:
        db.record_daily_lead(day, lead.get("cik"))
    return lead
