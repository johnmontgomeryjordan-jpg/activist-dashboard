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
    cands = [r for r in pool if _CATALYST.search(r.get("signals") or "")]
    cands.sort(key=lambda r: r.get("vuln") or 0, reverse=True)
    if not cands:
        cands = sorted(pool, key=lambda r: r.get("vuln") or 0, reverse=True)
    top = cands[:8]
    if not top:
        return None
    return top[day_index() % len(top)]


def lead_cik(rows):
    lead = pick_lead(rows)
    return lead.get("cik") if lead else None
