"""
Central configuration. Everything is read from environment variables so that a
non-technical user only ever has to edit the .env file (or paste values into the
hosting dashboard) -- never the code itself.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load a local .env file if present (ignored in production where the host
# injects environment variables directly).
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Required by SEC: a contact string sent with every EDGAR request ---------
# SEC asks that automated tools identify themselves. Put your firm name + email.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "Activist Dashboard Demo (contact@example.com)"
)

# --- News API ----------------------------------------------------------------
# Which provider to use: "newsapi" or "gnews". Both have free tiers.
NEWS_PROVIDER = os.getenv("NEWS_PROVIDER", "newsapi").lower()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# --- Email -------------------------------------------------------------------
# Which provider to use: "resend" or "sendgrid". Leave blank to disable email.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend").lower()
EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "digest@example.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Activist Vulnerability Dashboard")

# --- Access control ----------------------------------------------------------
# Single shared firm password gating the whole site (internal tool). If SITE_PASSWORD
# is left blank the site stays open; set it in the host dashboard to require a login.
SITE_USER = os.getenv("SITE_USER", "fgs")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")

# --- Universe ----------------------------------------------------------------
# Minimum market cap (USD) for a company to be monitored.
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "1000000000"))  # $1B

# Path to the CSV of tickers we monitor (ticker,name). Defaults to bundled file.
UNIVERSE_CSV = os.getenv("UNIVERSE_CSV", str(BASE_DIR / "app" / "universe.csv"))

# --- Scoring -----------------------------------------------------------------
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "3"))   # flag at >= this
SCORE_WINDOW_DAYS = int(os.getenv("SCORE_WINDOW_DAYS", "90"))
SHORTLIST_SIZE = int(os.getenv("SHORTLIST_SIZE", "15"))

# --- Scheduler ---------------------------------------------------------------
REFRESH_MINUTES = int(os.getenv("REFRESH_MINUTES", "30"))   # data refresh cadence
# Hard daily rebuild + digest at 6:00 AM ET, so the site + pitch kit are fresh each
# morning and capture overnight filings/headlines. Override with DIGEST_HOUR_ET.
DIGEST_HOUR_ET = int(os.getenv("DIGEST_HOUR_ET", "6"))      # 06:00 ET = 6am ET
TIMEZONE = "America/New_York"

# --- Database ----------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data.db"))

# --- Keyword dictionaries used for classifying filings & news ----------------
NEWS_KEYWORDS = [
    "activist investor", "shareholder pressure", "earnings miss",
    "CEO departure", "restructuring", "write-down", "writedown",
    "guidance cut", "proxy fight", "short seller", "activist stake",
    "board shakeup", "strategic review", "spin-off", "spinoff",
]

# Phrases that, found in 8-K / 10-K / 10-Q text, indicate each signal.
SIGNAL_KEYWORDS = {
    "ceo_departure": [
        "resignation", "resigned", "departure of", "steps down", "stepped down",
        "termination of", "appointment of", "chief executive officer",
        "retirement of", "departure of director", "transition of",
    ],
    "earnings_miss": [
        "below previously", "lowered guidance", "reduced guidance",
        "revised outlook", "falls short", "did not meet", "below expectations",
        "guidance cut", "preliminary results", "revenue shortfall",
    ],
    "impairment": [
        "goodwill impairment", "impairment charge", "write-down", "writedown",
        "write-off", "asset impairment", "non-cash impairment",
    ],
    "layoffs": [
        "workforce reduction", "reduction in force", "layoff", "restructuring plan",
        "headcount reduction", "job cuts", "eliminate positions", "severance",
    ],
}
