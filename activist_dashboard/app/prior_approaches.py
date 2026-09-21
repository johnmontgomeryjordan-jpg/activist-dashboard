"""
Prior M&A approaches (item 8) -- a small, CURATED list, not a live-refreshing pipeline feed.

Why curated instead of detected from filings: free filing-based sources (SC TO-T/14D9 tender-
offer filings, SC 13D activist stakes, 8-K Item 8.01 disclosures) cannot reconstruct most REAL
prior approaches. None of Kohl's four 2022 bids were tender offers, so a filing-based detector
would miss all four -- and silently imply "no history" for a company that has real history, which
is worse than having no source at all. See the scoping brief this was built from (session notes)
for the full comparison of what each free source catches and misses.

This table is deliberately sparse -- it will only ever cover a handful of names in a universe of
~1,500, whichever ones a person has actually sourced and verified. That's also why it carries NO
scoring weight (see database.py's schema comment): a signal this uneven in coverage can't fairly
move the 0-92 vulnerability score. It exists purely to give the pitch and evidence tab a real,
citable fact to lead with, for the names it covers.

Add a row here (date, description, amount in dollars, status, and a source URL) and call
seed_known_approaches() -- run automatically once at app startup (see main.py) -- to add it. Every
row needs a source; re-running the seed is always safe (INSERT OR IGNORE on cik+date+description).
"""

# cik: the filer's 10-digit (or shorter -- padded on write) SEC CIK.
# status: "cancelled" (rejected/withdrawn), "completed", "pending" / "announced/pending", or
# "rumored" -- mirrors the vocabulary a FactSet-style transactions panel uses.
SEED = [
    # Kohl's Corporation -- four rejected 2022 take-private approaches. The whole reason this
    # feature exists: the board turned down $13-14B while the company is worth ~$2B today.
    {"cik": "0000885639", "date": "2022-01-23",
     "description": "Sycamore Partners Management LP approach to acquire Kohl's Corp.",
     "amount": 13_262_410_000, "status": "cancelled",
     "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000885639&type=SC+13D"},
    {"cik": "0000885639", "date": "2022-01-24",
     "description": "Private investor group approach to acquire Kohl's Corp.",
     "amount": 13_133_820_000, "status": "cancelled",
     "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000885639&type=8-K"},
    {"cik": "0000885639", "date": "2022-04-22",
     "description": "Second private investor group approach to acquire Kohl's Corp.",
     "amount": 13_942_190_000, "status": "cancelled",
     "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000885639&type=8-K"},
    {"cik": "0000885639", "date": "2022-06-08",
     "description": "Franchise Group, Inc. approach to acquire Kohl's Corp.",
     "amount": 14_264_690_000, "status": "cancelled",
     "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000885639&type=8-K"},
    # Papa John's International -- a 2025 rumored take-private approach, on a card whose
    # archetype is already "strategic review."
    {"cik": "0000901491", "date": "2025-06-11",
     "description": "Rumored private-group approach to acquire Papa John's International, Inc.",
     "amount": 2_940_240_000, "status": "rumored",
     "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000901491&type=SC+13D"},
]


def seed_known_approaches(database):
    """Idempotent: safe to call on every app startup (see main.py). `database` is passed in
    rather than imported, matching this module's no-DB-import-at-module-level convention seen
    elsewhere (e.g. catalyst.py) so this file stays trivially testable without a live DB."""
    for row in SEED:
        database.add_prior_approach(row["cik"], row["date"], row["description"],
                                    row["amount"], row["status"], row["source_url"])
