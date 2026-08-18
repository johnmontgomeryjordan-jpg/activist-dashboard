"""
Industry taxonomy for peer cohorts (GICS-style).

The peer group and the peer cutoffs used across the report used to key off SEC's 2-digit SIC code,
which is coarse and sometimes plain wrong: SEC classifies Omnicell (a healthcare medication-
management company) under "Electronic Computers" and Copart (an online salvage-auction business)
under "Auto Dealers." That put both names in the wrong peer set and produced misleading cutoffs.

This module maps a company's Finnhub industry string (which is keyed off the TICKER, so it is
correct where SIC is not) to:
  * canon(industry)          -> a cleaned cohort label (or None if empty)
  * broad_sector(industry)   -> (key, label) for one of ~11 GICS-style sectors, used to roll a
                                thin industry cohort up to a larger, still-meaningful peer set
  * is_financial(industry)   -> True for banks / insurers / brokers / asset managers, which need
                                the existing balance-sheet carve-out (reserves/float, not levers)

Deliberately keyword-based (substring match on the lowercased string), so it degrades gracefully on
Finnhub industry values we have not seen before rather than dropping them on the floor. Anything it
cannot classify simply has no broad-sector rollup and falls back to SIC upstream.
"""

# Ordered (first match wins) keyword -> (sector_key, sector_label). More specific buckets first so,
# e.g., "financial technology" isn't swallowed by "technology" and "life sciences tools" lands in
# Health Care rather than Industrials.
_SECTOR_RULES = [
    # Financials (see is_financial too)
    (("bank", "insurance", "insurer", "reinsurance", "capital market", "asset management",
      "brokerage", "broker-dealer", "thrift", "mortgage finance", "consumer finance",
      "diversified financ", "financial servic", "financial exchange", "credit servic"),
     ("financials", "Financials")),
    # Real Estate (kept OUT of Financials — different peer characteristics)
    (("real estate", "reit", "rental & leasing"), ("real_estate", "Real Estate")),
    # Health Care
    (("health", "pharmaceutic", "pharma", "biotech", "life scienc", "medical", "drug",
      "hospital", "diagnostic", "managed care"), ("health_care", "Health Care")),
    # Energy
    (("oil", "gas", "petroleum", "coal", "energy equipment", "drilling", "midstream",
      "refining"), ("energy", "Energy")),
    # Utilities
    (("utilit", "electric power", "water utilit", "power generation", "renewable electricity"),
     ("utilities", "Utilities")),
    # Materials
    (("chemical", "metal", "mining", "steel", "gold", "copper", "materials", "paper",
      "forest", "packaging", "container", "construction material", "fertilizer"),
     ("materials", "Materials")),
    # Communication Services
    (("telecom", "wireless", "media", "entertainment", "publishing", "advertising",
      "interactive media", "communication servic", "cable", "broadcast", "gaming"),
     ("communication_services", "Communication Services")),
    # Information Technology
    (("software", "semiconduct", "it servic", "information technolog", "internet software",
      "electronic equipment", "hardware", "computer", "technology hardware", "fintech",
      "payment", "data processing", "cloud"), ("information_technology", "Information Technology")),
    # Consumer Staples
    (("food", "beverage", "tobacco", "household product", "personal product", "staples",
      "grocery", "agricultur", "farm product"), ("consumer_staples", "Consumer Staples")),
    # Consumer Discretionary
    (("retail", "apparel", "luxury", "textile", "footwear", "auto", "automobile", "vehicle",
      "hotel", "restaurant", "leisure", "homebuild", "home builder", "household durable",
      "consumer discretion", "consumer product", "e-commerce", "specialty consumer",
      "distributors", "casino", "cruise"), ("consumer_discretionary", "Consumer Discretionary")),
    # Industrials (broad; last so it doesn't swallow the more specific buckets above)
    (("aerospace", "defense", "machinery", "industrial", "construction", "engineering",
      "building product", "electrical equipment", "road", "rail", "airline", "air freight",
      "logistics", "transport", "marine", "trucking", "commercial servic", "professional servic",
      "business servic", "support servic", "trading compan", "conglomerate", "environmental",
      "waste", "human resource", "staffing"),
     ("industrials", "Industrials")),
]

# Financial industries that need the bank/insurer/broker balance-sheet carve-out (reserves, float,
# structurally high leverage; no industrial "operating margin"/"SG&A"/EBITDA). Real estate/REITs are
# intentionally excluded — they are not part of that carve-out.
_FINANCIAL_KEYS = (
    "bank", "insurance", "insurer", "reinsurance", "capital market", "asset management",
    "brokerage", "broker-dealer", "thrift", "mortgage finance", "consumer finance",
    "diversified financ", "financial servic", "financial exchange", "credit servic",
)


def canon(industry):
    """A cleaned cohort label from a raw Finnhub industry string, or None when absent/uninformative."""
    s = (industry or "").strip()
    if not s or s.lower() in ("n/a", "na", "none", "unknown", "-"):
        return None
    return " ".join(s.split())


def _match(industry, rules_or_keys):
    s = (industry or "").lower()
    if not s:
        return None
    for entry in rules_or_keys:
        if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], tuple):
            keys, out = entry
            if any(k in s for k in keys):
                return out
    return None


def broad_sector(industry):
    """(sector_key, sector_label) for one of ~11 GICS-style sectors, or None if unclassifiable.
    Used to roll a thin industry cohort up to a larger peer set."""
    return _match(industry, _SECTOR_RULES)


def is_financial(industry):
    """True for banks / insurers / brokers / asset managers (the reserve/float carve-out).
    Real estate / REITs are deliberately excluded."""
    s = (industry or "").lower()
    return bool(s) and any(k in s for k in _FINANCIAL_KEYS)
