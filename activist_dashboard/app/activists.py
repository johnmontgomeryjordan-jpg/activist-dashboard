"""
Canonical known-activist list — the single source of truth shared by:
  * activist.py  -> the 13D/proxy FILER gate (is a noisy DFAN14A/PX14A6G really an activist?)
  * scoring.py   -> news auto-routing to Active Situations (KNOWN_FUNDS)
  * news.py      -> activist-headline capture/retention (_ACTIVIST_CUES)

It merges the code's historical fund list, the FGS "Recurring Activist Investor List"
(06.23.26), and a conservative set of known activist INDIVIDUALS (EDGAR files people as
"LASTNAME FIRSTNAME", so a surname substring matches; only distinctive surnames are
included to avoid false hits like Cohen/Smith). Matching is a lowercased substring test.

Nothing is imported from the app here, so every module can import this without cycles.
"""
import re

# Recurring activist FUNDS (distinctive substrings; lowercased match against a filer name
# or a news headline). Keep each entry specific enough to avoid collisions.
FUNDS = [
    # large / multi-strategy
    "elliott", "starboard", "trian", "jana partners", "third point", "valueact",
    "value act", "pershing square", "engine capital", "engine no", "engaged capital",
    "ancora", "politan", "sachem head", "legion partners", "corvex",
    "land & buildings", "land and buildings", "mantle ridge", "inclusive capital",
    "saba capital", "h partners", "irenic", "barington", "d.e. shaw", "glenview",
    "hestia", "bluebell", "soroban", "kimmeridge", "impactive", "scopia",
    "lynrock", "browning west", "ananym", "caligan", "kanen", "gatemore",
    "vision one", "cruiser capital", "blue harbour", "marcato", "white tale",
    "starboard value",
    # FGS "Recurring Activist Investor List" additions
    "adw capital", "alta fox", "amber capital", "anson funds", "bulldog investors",
    "carronade", "cevian", "dalton investments", "holdco asset", "mhr fund",
    "oasis management", "p2 capital", "palliser", "parvus", "pl capital", "sarissa",
    "steel partners", "stilwell", "toms capital", "voss capital", "wynnefield",
    "macellum", "the children's investment", "tci fund", "jec capital", "fondren",
    "irenic capital", "engine no. 1", "third point llc",
]

# Known activist INDIVIDUALS — distinctive surnames only (deliberately conservative).
# NOTE: gadfly/ESG proponents who flood PX14A6G filings (e.g. John Chevedden) are
# intentionally EXCLUDED — they are exactly the noise the filer gate exists to remove.
INDIVIDUALS = [
    "icahn", "peltz", "ackman", "loeb", "radoff", "biglari",
    "jonathan litt", "ryan cohen", "rc ventures",
]

# The full term list used for filer matching AND news-headline matching.
NEWS_TERMS = FUNDS + INDIVIDUALS

_ALL = [t for t in NEWS_TERMS if t]


def _norm(s):
    return " " + re.sub(r"\s+", " ", (s or "").lower()) + " "


def is_known_activist(name):
    """True if `name` (a filer or a headline) contains a known activist term."""
    if not name:
        return False
    t = _norm(name)
    return any(a in t for a in _ALL)


def which_activist(name):
    """The first known-activist term found in `name`, else None (for a 'who' label)."""
    if not name:
        return None
    t = _norm(name)
    for a in _ALL:
        if a in t:
            return a
    return None


def clean_filer(display_name):
    """Turn an EDGAR display_name ('Starboard Value LP  (CIK 0001517137)' or
    'RADOFF BRADLEY LOUIS  (CIK ...)') into a clean name by dropping the parenthetical
    ticker/CIK tags."""
    if not display_name:
        return ""
    return re.sub(r"\s*\([^)]*\)", "", str(display_name)).strip()
