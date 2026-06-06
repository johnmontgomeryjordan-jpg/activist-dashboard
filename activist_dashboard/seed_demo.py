"""
Optional: load realistic SAMPLE data so the dashboard looks populated for a
client demo even before you've added a news API key or waited for the first
live EDGAR pull.

Run once:   python seed_demo.py
Clear it:   delete data.db, or run live refresh which will add real data alongside.

The sample rows are clearly fictional/illustrative and link to real EDGAR/issuer
pages where possible. Replace with live data for production use.
"""
from datetime import datetime, timedelta

from app import database, config

database.init_db()


def days_ago(n):
    return (datetime.utcnow() - timedelta(days=n)).date().isoformat()


# A few in-universe companies with market data so scoring/universe filter works.
COMPANIES = [
    # cik, ticker, name, market_cap, pb, tsr_1y, tsr_3y
    ("0000320193", "AAPL", "Apple Inc.", 3.1e12, 45.0, 0.12, 0.40),
    ("0000012927", "BA", "Boeing Co.", 1.1e11, -8.0, -0.18, -0.35),
    ("0000040730", "GE", "General Electric Co.", 1.8e11, 3.2, 0.22, 0.15),
    ("0000037996", "F", "Ford Motor Co.", 4.5e10, 0.9, -0.22, -0.28),
    ("0000080424", "PG", "Procter & Gamble Co.", 3.8e11, 7.5, 0.05, 0.20),
    ("0000732717", "T", "AT&T Inc.", 1.3e11, 1.2, -0.05, -0.10),
    ("0000310158", "MRK", "Merck & Co. Inc.", 3.0e11, 5.1, 0.08, 0.18),
    ("0000021344", "KO", "Coca-Cola Co.", 2.6e11, 9.0, 0.04, 0.12),
    ("0000027904", "DAL", "Delta Air Lines Inc.", 3.1e10, 1.3, -0.09, -0.04),
    ("0000063908", "M", "Macy's Inc.", 4.5e9, 0.8, -0.30, -0.45),
]
for cik, t, n, mc, pb, t1, t3 in COMPANIES:
    database.upsert_company(cik, t, n, market_cap=mc, pb_ratio=pb,
                            tsr_1y=t1, tsr_3y=t3)

# Sample filings with classified signals.
FILINGS = [
    ("demo-ba-1", "0000012927", "BA", "Boeing Co.", "8-K",
     days_ago(4), "8-K: Executive change (Item 5.02)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000012927&type=8-K",
     "ceo_departure"),
    ("demo-ba-2", "0000012927", "BA", "Boeing Co.", "8-K",
     days_ago(10), "8-K: Material impairment (Item 2.06)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000012927&type=8-K",
     "impairment"),
    ("demo-f-1", "0000037996", "F", "Ford Motor Co.", "8-K",
     days_ago(6), "8-K: Results / guidance (Item 2.02)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000037996&type=8-K",
     "earnings_miss"),
    ("demo-f-2", "0000037996", "F", "Ford Motor Co.", "8-K",
     days_ago(15), "8-K: Restructuring / exit costs (Item 2.05)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000037996&type=8-K",
     "layoffs"),
    ("demo-m-1", "0000063908", "M", "Macy's Inc.", "8-K",
     days_ago(3), "8-K: Executive change (Item 5.02)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000063908&type=8-K",
     "ceo_departure"),
    ("demo-m-2", "0000063908", "M", "Macy's Inc.", "8-K",
     days_ago(8), "8-K: Restructuring / exit costs (Item 2.05)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000063908&type=8-K",
     "layoffs"),
    ("demo-t-1", "0000732717", "T", "AT&T Inc.", "8-K",
     days_ago(12), "8-K: Material impairment (Item 2.06)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000732717&type=8-K",
     "impairment"),
    ("demo-dal-1", "0000027904", "DAL", "Delta Air Lines Inc.", "8-K",
     days_ago(5), "8-K: Results / guidance (Item 2.02)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000027904&type=8-K",
     "earnings_miss"),
    ("demo-ge-1", "0000040730", "GE", "General Electric Co.", "8-K",
     days_ago(2), "8-K: Material event",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000040730&type=8-K",
     ""),
]
for f in FILINGS:
    database.upsert_filing(dict(zip(
        ["id", "cik", "ticker", "company", "form", "filed_at", "title", "url",
         "signals"], f)))

# Sample news headlines (links go to issuer/EDGAR pages; replace with live API).
NEWS = [
    ("demo-news-1", "Activist investor builds stake, pushes Macy's for real-estate spin-off",
     "Reuters", days_ago(1),
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000063908", "M"),
    ("demo-news-2", "Boeing CEO departs amid mounting shareholder pressure",
     "CNBC", days_ago(4),
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000012927", "BA"),
    ("demo-news-3", "Ford cuts full-year guidance, announces restructuring",
     "Bloomberg", days_ago(6),
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000037996", "F"),
    ("demo-news-4", "Short seller targets AT&T over balance-sheet write-down",
     "Financial Times", days_ago(11),
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000732717", "T"),
    ("demo-news-5", "Delta misses earnings; analysts flag demand softness",
     "WSJ", days_ago(5),
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000027904", "DAL"),
]
for nid, head, src, pub, url, ticker in NEWS:
    database.upsert_news({
        "id": nid, "headline": head, "source": src,
        "published_at": pub + "T12:00:00", "url": url,
        "matched_tickers": ticker,
    })

# Compute scores from the seeded data.
from app import scoring  # noqa: E402
flagged = scoring.recompute_all()
print(f"Seeded demo data. {len(flagged)} companies flagged:")
for c in flagged:
    print(f"  {c['ticker']:5} score={c['score']}  {c['signals']}")
