"""
SQLite storage: companies (incl. market_cap/P-B), filings, news, scores,
subscribers, SEC XBRL fundamentals, stored Alpha Vantage OVERVIEW blobs, and a
daily score-history snapshot for week-over-week trend tracking.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik TEXT PRIMARY KEY, ticker TEXT, name TEXT, market_cap REAL,
    pb_ratio REAL, tsr_1y REAL, tsr_3y REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS filings (
    id TEXT PRIMARY KEY, cik TEXT, ticker TEXT, company TEXT, form TEXT,
    filed_at TEXT, title TEXT, url TEXT, signals TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, headline TEXT, source TEXT, published_at TEXT,
    url TEXT, matched_tickers TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    cik TEXT PRIMARY KEY, ticker TEXT, company TEXT, market_cap REAL,
    score INTEGER, signals TEXT, top_item_title TEXT, top_item_url TEXT,
    first_flagged TEXT, evidence TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS subscribers (
    email TEXT PRIMARY KEY, created_at TEXT
);
CREATE TABLE IF NOT EXISTS fundamentals (
    cik TEXT PRIMARY KEY, ticker TEXT, sector TEXT,
    revenue REAL, revenue_growth REAL, operating_margin REAL, sga_pct REAL,
    roa REAL, cash_to_assets REAL, debt_to_assets REAL,
    shares REAL, book_equity REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS av_overview (
    cik TEXT PRIMARY KEY, ticker TEXT, data TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS score_history (
    cik TEXT, date TEXT, score INTEGER, PRIMARY KEY (cik, date)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Safe migration for DBs created before the `evidence` column existed.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(scores)")]
        if "evidence" not in cols:
            conn.execute("ALTER TABLE scores ADD COLUMN evidence TEXT")


def now_iso():
    return datetime.utcnow().isoformat()


# --- Companies ---------------------------------------------------------------
def upsert_company(cik, ticker, name, market_cap=None, pb_ratio=None,
                   tsr_1y=None, tsr_3y=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO companies (cik,ticker,name,market_cap,pb_ratio,tsr_1y,tsr_3y,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, name=excluded.name,
                 market_cap=COALESCE(excluded.market_cap, companies.market_cap),
                 pb_ratio=COALESCE(excluded.pb_ratio, companies.pb_ratio),
                 tsr_1y=COALESCE(excluded.tsr_1y, companies.tsr_1y),
                 tsr_3y=COALESCE(excluded.tsr_3y, companies.tsr_3y),
                 updated_at=excluded.updated_at""",
            (cik, ticker, name, market_cap, pb_ratio, tsr_1y, tsr_3y, now_iso()),
        )


def set_company_market(cik, market_cap=None, pb_ratio=None, tsr_1y=None, tsr_3y=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE companies SET
                 market_cap=COALESCE(?, market_cap), pb_ratio=COALESCE(?, pb_ratio),
                 tsr_1y=COALESCE(?, tsr_1y), tsr_3y=COALESCE(?, tsr_3y),
                 updated_at=? WHERE cik=?""",
            (market_cap, pb_ratio, tsr_1y, tsr_3y, now_iso(), cik),
        )


def get_companies():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM companies")]


# --- Fundamentals ------------------------------------------------------------
def upsert_fundamentals(cik, ticker, sector, m):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO fundamentals
               (cik,ticker,sector,revenue,revenue_growth,operating_margin,sga_pct,
                roa,cash_to_assets,debt_to_assets,shares,book_equity,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, sector=excluded.sector,
                 revenue=excluded.revenue, revenue_growth=excluded.revenue_growth,
                 operating_margin=excluded.operating_margin, sga_pct=excluded.sga_pct,
                 roa=excluded.roa, cash_to_assets=excluded.cash_to_assets,
                 debt_to_assets=excluded.debt_to_assets, shares=excluded.shares,
                 book_equity=excluded.book_equity, updated_at=excluded.updated_at""",
            (cik, ticker, sector, m.get("revenue"), m.get("revenue_growth"),
             m.get("operating_margin"), m.get("sga_pct"), m.get("roa"),
             m.get("cash_to_assets"), m.get("debt_to_assets"),
             m.get("shares"), m.get("book_equity"), now_iso()),
        )


def get_all_fundamentals():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM fundamentals")]


def get_fundamentals_one(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM fundamentals WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


# --- Alpha Vantage overview --------------------------------------------------
def upsert_av_overview(cik, ticker, data_dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO av_overview (cik,ticker,data,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, data=excluded.data, updated_at=excluded.updated_at""",
            (cik, ticker, json.dumps(data_dict), now_iso()),
        )


def get_av_overview(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT data FROM av_overview WHERE cik=?", (cik,)).fetchone()
        if not r:
            return {}
        try:
            return json.loads(r["data"])
        except (ValueError, TypeError):
            return {}


# --- Filings -----------------------------------------------------------------
def upsert_filing(f):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO filings
               (id,cik,ticker,company,form,filed_at,title,url,signals,ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f["id"], f["cik"], f.get("ticker"), f["company"], f["form"],
             f["filed_at"], f["title"], f["url"], f.get("signals", ""), now_iso()),
        )


def recent_filings(limit=40):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM filings ORDER BY filed_at DESC LIMIT ?", (limit,))]


def filings_in_window(cik, days):
    cutoff = _cutoff(days)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM filings WHERE cik=? AND filed_at>=? ORDER BY filed_at DESC",
            (cik, cutoff))]


def get_filings_by_cik(cik, limit=12):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM filings WHERE cik=? ORDER BY filed_at DESC LIMIT ?",
            (cik, limit))]


# --- News --------------------------------------------------------------------
def upsert_news(n):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO news
               (id,headline,source,published_at,url,matched_tickers,ingested_at)
               VALUES (?,?,?,?,?,?,?)""",
            (n["id"], n["headline"], n["source"], n["published_at"], n["url"],
             n.get("matched_tickers", ""), now_iso()),
        )


def recent_news(limit=40):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM news ORDER BY published_at DESC LIMIT ?", (limit,))]


def news_for_ticker_in_window(ticker, days):
    cutoff = _cutoff(days)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM news WHERE published_at>=?
               AND (','||matched_tickers||',') LIKE ? ORDER BY published_at DESC""",
            (cutoff, f"%,{ticker},%"))]


def get_news_for_ticker(ticker, limit=10):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM news WHERE (','||matched_tickers||',') LIKE ?
               ORDER BY published_at DESC LIMIT ?""", (f"%,{ticker},%", limit))]


# --- Scores ------------------------------------------------------------------
def replace_scores(rows):
    with get_conn() as conn:
        existing = {r["cik"]: r["first_flagged"]
                    for r in conn.execute("SELECT cik, first_flagged FROM scores")}
        conn.execute("DELETE FROM scores")
        for r in rows:
            first = existing.get(r["cik"], r["first_flagged"])
            conn.execute(
                """INSERT INTO scores
                   (cik,ticker,company,market_cap,score,signals,top_item_title,
                    top_item_url,first_flagged,evidence,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["cik"], r["ticker"], r["company"], r["market_cap"], r["score"],
                 r["signals"], r["top_item_title"], r["top_item_url"], first,
                 json.dumps(r.get("evidence") or []), now_iso()),
            )
        today = datetime.utcnow().date().isoformat()
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO score_history (cik,date,score) VALUES (?,?,?)",
                (r["cik"], today, r["score"]),
            )


def get_scores(limit=15):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scores ORDER BY score DESC, company ASC LIMIT ?", (limit,))]


def get_score_one(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM scores WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def prior_score(cik, days=7):
    """Most recent history score from at least `days` ago (for week-over-week)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    with get_conn() as conn:
        r = conn.execute(
            "SELECT score FROM score_history WHERE cik=? AND date<=? ORDER BY date DESC LIMIT 1",
            (cik, cutoff)).fetchone()
        return r["score"] if r else None


# --- Subscribers -------------------------------------------------------------
def add_subscriber(email):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO subscribers (email,created_at) VALUES (?,?)",
                     (email.strip().lower(), now_iso()))


def remove_subscriber(email):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscribers WHERE email=?", (email.strip().lower(),))


def get_subscribers():
    with get_conn() as conn:
        return [r["email"] for r in conn.execute("SELECT email FROM subscribers")]


def _cutoff(days):
    return (datetime.utcnow() - timedelta(days=days)).isoformat()
