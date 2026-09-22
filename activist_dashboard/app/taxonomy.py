"""
Industry taxonomy for peer cohorts (GICS-style).

The peer group and the peer cutoffs used across the report used to key off SEC's 2-digit SIC code,
which is coarse and sometimes plain wrong: SEC classifies Omnicell (a healthcare medication-
management company) under "Electronic Computers" and Copart (an online salvage-auction business)
under "Auto Dealers." That put both names in the wrong peer set and produced misleading cutoffs.

This module maps a company's Finnhub industry string (which is keyed off the TICKER, so it is
correct where SIC is not) to:
  * canon(industry)          -> a cleaned cohort label (or None if empty)
  * sub_sector(industry)     -> (key, label) for a GICS-style INDUSTRY GROUP (e.g. "Health Care
                                Equipment & Services" vs "Pharmaceuticals, Biotechnology & Life
                                Sciences") -- a mid-tier rollup for a thin industry cohort, one
                                notch narrower than the full sector. Root-caused auditing AHCO (a
                                home-medical-equipment company): its specific industry cohort was
                                too thin (< MIN_PEERS) and rolled ALL THE WAY UP to the full 56-66
                                name "Health Care" sector -- mixing a DME distributor's EV/EBITDA
                                against biotech and pharma multiples, which trade on an entirely
                                different basis. There was no stop between "too thin to use" and
                                "the whole sector."
  * broad_sector(industry)   -> (key, label) for one of ~11 GICS-style sectors, used to roll a
                                thin industry cohort (or thin sub-sector) up to a larger,
                                still-meaningful peer set
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

# Mid-tier rollup: real GICS "industry group" splits within each sector above (kept a strict
# subset of that sector's own keywords, so anything matching a sub-sector also matches its
# parent). Only sectors broad/heterogeneous enough to need a stop between "too thin" and "the
# whole sector" are split; Energy, Utilities, Materials and Real Estate are effectively single
# industry groups already at this keyword granularity, so they're left un-split (falls straight
# through to broad_sector). Ordered (first match wins), most specific first.
_SUBSECTOR_RULES = [
    # Health Care -> Pharma/Biotech/Life Sciences vs. Equipment & Services (the AHCO case: a DME
    # distributor has nothing in common, multiple-wise, with a biotech burning cash on a pipeline).
    (("pharmaceutic", "pharma", "biotech", "life scienc", "drug"),
     ("health_pharma_biotech", "Pharmaceuticals, Biotechnology & Life Sciences")),
    (("health", "medical", "hospital", "diagnostic", "managed care"),
     ("health_equipment_services", "Health Care Equipment & Services")),
    # Financials -> Banks vs. Insurance vs. everything else diversified
    (("bank", "thrift"), ("fin_banks", "Banks")),
    (("insurance", "insurer", "reinsurance"), ("fin_insurance", "Insurance")),
    (("capital market", "asset management", "brokerage", "broker-dealer", "mortgage finance",
      "consumer finance", "diversified financ", "financial servic", "financial exchange",
      "credit servic"), ("fin_diversified", "Diversified Financials")),
    # Communication Services -> Telecom vs. Media & Entertainment
    (("telecom", "wireless", "cable", "communication servic"),
     ("comm_telecom", "Telecommunication Services")),
    (("media", "entertainment", "publishing", "advertising", "interactive media", "broadcast",
      "gaming"), ("comm_media", "Media & Entertainment")),
    # Information Technology -> Semis vs. Hardware vs. Software & Services
    (("semiconduct",), ("it_semis", "Semiconductors & Semiconductor Equipment")),
    (("electronic equipment", "hardware", "computer", "technology hardware"),
     ("it_hardware", "Technology Hardware & Equipment")),
    (("software", "it servic", "internet software", "information technolog", "fintech",
      "payment", "data processing", "cloud"), ("it_software", "Software & Services")),
    # Consumer Staples -> Food/Beverage/Tobacco vs. Household & Personal Products vs. Staples Retail
    (("household product", "personal product"),
     ("staples_household", "Household & Personal Products")),
    (("staples", "grocery"), ("staples_retail", "Food & Staples Retailing")),
    (("food", "beverage", "tobacco", "agricultur", "farm product"),
     ("staples_food_bev", "Food, Beverage & Tobacco")),
    # Consumer Discretionary -> Autos vs. Durables & Apparel vs. Consumer Services vs. Retailing
    (("auto", "automobile", "vehicle"), ("disc_autos", "Automobiles & Components")),
    (("apparel", "luxury", "textile", "footwear", "homebuild", "home builder",
      "household durable"), ("disc_durables", "Consumer Durables & Apparel")),
    (("hotel", "restaurant", "leisure", "casino", "cruise"),
     ("disc_services", "Consumer Services")),
    (("retail", "e-commerce", "distributors", "specialty consumer", "consumer discretion",
      "consumer product"), ("disc_retail", "Retailing")),
    # Industrials -> Capital Goods vs. Commercial & Professional Services vs. Transportation
    (("aerospace", "defense", "machinery", "industrial", "construction", "engineering",
      "building product", "electrical equipment", "trading compan", "conglomerate"),
     ("ind_capital_goods", "Capital Goods")),
    (("commercial servic", "professional servic", "business servic", "support servic",
      "environmental", "waste", "human resource", "staffing"),
     ("ind_commercial_services", "Commercial & Professional Services")),
    (("road", "rail", "airline", "air freight", "logistics", "transport", "marine", "trucking"),
     ("ind_transportation", "Transportation")),
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


def sub_sector(industry):
    """(subsector_key, subsector_label) for a GICS-style industry group, or None when `industry`
    doesn't fall in one of the sectors split above (Energy/Utilities/Materials/Real Estate/
    unclassifiable) -- the caller falls through to broad_sector() in that case. A mid-tier rollup
    for a thin industry cohort, narrower than the full sector: see the module docstring."""
    return _match(industry, _SUBSECTOR_RULES)


def is_financial(industry):
    """True for banks / insurers / brokers / asset managers (the reserve/float carve-out).
    Real estate / REITs are deliberately excluded."""
    s = (industry or "").lower()
    return bool(s) and any(k in s for k in _FINANCIAL_KEYS)
