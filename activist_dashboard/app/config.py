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
# Which provider to use: "gmail", "resend", or "sendgrid". Leave blank to disable.
#   * gmail    -> sends through your own Gmail via SMTP. No domain/DNS setup needed:
#                 turn on 2-Step Verification, generate a 16-digit App Password, and
#                 put it in GMAIL_APP_PASSWORD below. Sends to anyone (~500 recipients/
#                 day on a free @gmail.com; ~2,000/day on Google Workspace).
#   * resend   -> needs a verified sending domain to reach anyone but the account owner.
#   * sendgrid -> free up to 100 emails/day.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend").lower()
EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "digest@example.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Activist Vulnerability Dashboard")
# --- Gmail SMTP (only used when EMAIL_PROVIDER=gmail) -------------------------
# GMAIL_USER is the full Gmail address you send from (e.g. you@gmail.com).
# GMAIL_APP_PASSWORD is the 16-digit App Password from your Google account
# (Security -> 2-Step Verification -> App passwords). NOT your normal password.
# If EMAIL_FROM is left at its default, the Gmail sender falls back to GMAIL_USER.
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
# Public base URL of the dashboard (used for links in the emailed pitch kit).
SITE_URL = os.getenv("SITE_URL", "https://activist-dashboard.onrender.com")

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

# --- U2b: free CUSIP -> ticker mapping ---------------------------------------
# SEC Fails-to-Deliver files: free, public, pipe-delimited ZIPs carrying
# CUSIP + SYMBOL + issuer name for ~every actively traded security. Primary
# source for the cusip->ticker spine that 4b's 13F matching resolves against.
FTD_BASE_URL = os.getenv(
    "FTD_BASE_URL", "https://www.sec.gov/files/data/fails-deliver-data")
FTD_MONTHS = int(os.getenv("FTD_MONTHS", "3"))     # look back this many months for files
FTD_MAX_FILES = int(os.getenv("FTD_MAX_FILES", "2"))  # parse this many recent files
# OpenFIGI: free identifier-mapping API used only to gap-fill a CUSIP the FTD map
# misses (works keyless at a lower rate limit; set a key to raise it).
OPENFIGI_URL = os.getenv("OPENFIGI_URL", "https://api.openfigi.com/v3/mapping")
OPENFIGI_API_KEY = os.getenv("OPENFIGI_API_KEY", "")

# --- 13F activist-holder signal (early-warning tier) -------------------------
# A known activist already HOLDS a name (per its quarterly Form 13F) but hasn't yet agitated
# (no 13D / proxy). We resolve each fund's 13F-filing CIK via EDGAR company search, parse its
# information table, map CUSIP -> ticker off the cusip_map (U2b), and fire the signal when the
# stake shows conviction on EITHER measure below. All free SEC data.
# Materiality = influence over the COMPANY, so ownership-of-company is the necessary condition
# (a big % of a fund's book in a mega-cap is a passive core long, not a campaign — Third Point's
# 0.02% of Amazon). A name qualifies if a holder owns >= OWNERSHIP_PCT_MIN of the company, OR
# owns >= OWNERSHIP_SOFT of it AND that stake is a concentrated >= FUND_WEIGHT_CONV of its book.
THIRTEENF_ENABLED = os.getenv("THIRTEENF_ENABLED", "1") == "1"
OWNERSHIP_PCT_MIN = float(os.getenv("OWNERSHIP_PCT_MIN", "0.01"))  # >= 1% of the company (strong)
OWNERSHIP_SOFT = float(os.getenv("OWNERSHIP_SOFT", "0.005"))      # >= 0.5% of the company (soft)
FUND_WEIGHT_CONV = float(os.getenv("FUND_WEIGHT_CONV", "0.05"))   # + >= 5% of the fund's book
# Real activists run CONCENTRATED books; a quant / multi-strat fund (D.E. Shaw) holds hundreds of
# names each a rounding error of its book. Require the position to be >= this share of the fund's
# 13F book so a diversified 0.x%-of-book holding doesn't count as activist conviction.
FUND_WEIGHT_FLOOR = float(os.getenv("FUND_WEIGHT_FLOOR", "0.01"))  # >= 1% of the fund's book
# Ownership above this is physically impossible -> bad shares-outstanding (e.g. wrong share class);
# reject it as a data error rather than let it clear the ownership gate.
OWNERSHIP_MAX = float(os.getenv("OWNERSHIP_MAX", "1.0"))
# Crossing 5% forces a Schedule 13D/13G, so a >= this stake is DISCLOSED (already public), not a
# stealth accumulation -- flagged as such in the UI.
OWNERSHIP_DISCLOSED = float(os.getenv("OWNERSHIP_DISCLOSED", "0.05"))
# Activists don't run campaigns on the largest mega-caps (the capital is prohibitive), so a big
# holder in a >$100B name is a passive/compounder long (TCI in Visa, Soroban in Microsoft), not a
# campaign setup. Exclude them from the Accumulating tab.
MEGA_CAP_MAX = float(os.getenv("MEGA_CAP_MAX", "100000000000"))   # $100B
# Backstop for the mega-cap filter: these >$150B names have no reliable market cap in the DB
# (un-enriched universe rows) and sometimes carry bad shares-outstanding that inflate ownership
# (Mastercard). Activists don't stealth-accumulate them, so exclude by ticker regardless.
_MEGA_CAP_DENY_DEFAULT = (
    "AAPL,MSFT,NVDA,AMZN,GOOGL,GOOG,META,AVGO,TSLA,BRK.A,BRK.B,LLY,JPM,V,MA,WMT,UNH,XOM,ORCL,"
    "JNJ,PG,HD,COST,ABBV,KO,BAC,MRK,CVX,PEP,CRM,NFLX,TMO,ACN,LIN,MCD,ADBE,CSCO,ABT,WFC,IBM,GE,"
    "DIS,NOW,SPGI,QCOM,TXN,AMD,INTU,CAT,GS,AXP,MS,BLK,ISRG,PM,AMGN,GILD,C,BKNG,SYK,DHR,PFE,"
    "UNP,RTX,HON,LOW,T,VZ,CMCSA,COP,BSX,PLD,ADP,MU,PANW,KLAC,LRCX,SNPS,CDNS,ANET,APH,MMC,ELV,"
    "CB,CME,ICE,MO,SO,DUK,ZTS,PGR,APO,BX,SCHW,USB,PNC,CI,UPS,SBUX,MDT,TT,ETN,DE,BA,LMT,GD")
MEGA_CAP_DENY = {t.strip().upper() for t in
                 os.getenv("MEGA_CAP_DENY", _MEGA_CAP_DENY_DEFAULT).split(",") if t.strip()}
FUND_WEIGHT_MIN = float(os.getenv("FUND_WEIGHT_MIN", "0.02"))     # (legacy; kept for compat)
THIRTEENF_MAX_FUNDS = int(os.getenv("THIRTEENF_MAX_FUNDS", "80")) # cap funds resolved per run
# A real 13F filer files every quarter. If the newest 13F under a resolved CIK is older than this,
# the fund has stopped filing (restructured / dropped below $100M) — skip it rather than show a
# years-stale book (JANA's 2023, Amber's 2017).
STALE_13F_DAYS = int(os.getenv("STALE_13F_DAYS", "400"))          # ~13 months
THIRTEENF_TIMEOUT = int(os.getenv("THIRTEENF_TIMEOUT", "25"))     # per-request timeout (s)

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

# --- Biweekly report (the fortnightly "Activist Vulnerability" issue) ---------
# The daily email digest is PAUSED (pipeline.daily_rescore_and_digest no longer sends); the
# product is now a curated biweekly report — an in-app /report page + an emailed issue. The
# nightly rescore still runs so the board stays fresh; only the CADENCE of the email changed.
REPORT_ENABLED = os.getenv("REPORT_ENABLED", "1") == "1"
REPORT_HOUR_ET = int(os.getenv("REPORT_HOUR_ET", "8"))       # 08:00 ET send
REPORT_DAY = os.getenv("REPORT_DAY", "wed")                  # day_of_week to send
REPORT_WEEK_STEP = os.getenv("REPORT_WEEK_STEP", "*/2")      # every 2nd ISO week = biweekly
REPORT_BOARD_SIZE = int(os.getenv("REPORT_BOARD_SIZE", "5")) # names featured on the board
# Structural disqualifiers to keep OFF the proactive board (government stakes, etc.) beyond the
# built-in list in report.py. Comma-separated tickers. INTC (U.S. government stake) is built in.
REPORT_EXCLUDE_TICKERS = {t.strip().upper() for t in
                          os.getenv("REPORT_EXCLUDE_TICKERS", "").split(",") if t.strip()}
# The report goes to the SAME small subscriber list as the (paused) digest unless a dedicated
# comma-separated recipient list is set here.
REPORT_RECIPIENTS = [e.strip() for e in os.getenv("REPORT_RECIPIENTS", "").split(",") if e.strip()]

# --- Biweekly report: two-phase schedule + 6-month no-repeat rotation ------------------------
# GENERATE + auto-audit Tuesday night, then SEND to the distribution list Wednesday morning, so
# the audited content is exactly what ships. Cadence is anchored to REPORT_ANCHOR (every 14 days)
# rather than ISO-week parity, so the first issue lands on the intended date.
REPORT_GEN_DAY = os.getenv("REPORT_GEN_DAY", "tue")               # generate + audit day
REPORT_GEN_HOUR_ET = int(os.getenv("REPORT_GEN_HOUR_ET", "19"))   # 19:00 ET = 7 PM ET
REPORT_SEND_DAY = os.getenv("REPORT_SEND_DAY", "wed")             # email the subscriber list
REPORT_SEND_HOUR_ET = int(os.getenv("REPORT_SEND_HOUR_ET", "7"))  # 07:00 ET = 7 AM ET
REPORT_ANCHOR = os.getenv("REPORT_ANCHOR", "2026-07-28")          # first GENERATE date; every 14d after
REPORT_NOREPEAT_ISSUES = int(os.getenv("REPORT_NOREPEAT_ISSUES", "13"))  # 13 biweekly issues ≈ 6 months
REPORT_POOL = int(os.getenv("REPORT_POOL", "500"))               # scored-universe pool the rotation draws from
# The 5 fresh names each issue: (by current rating, by biggest riser, by freshest catalyst).
REPORT_BLEND = tuple(int(x) for x in os.getenv("REPORT_BLEND", "2,2,1").split(","))
# When the Tuesday audit flags a blocking issue, HOLD the send and alert ONLY this address — never
# the distribution list. Defaults to John so a held-issue alert can never reach the FGS partners;
# override in Render if the owner changes. If it were ever blank, the alert is skipped entirely
# (logged only) rather than risk mailing a subscriber.
REPORT_ADMIN_EMAIL = os.getenv("REPORT_ADMIN_EMAIL", "johnmontgomeryjordan@gmail.com")

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
