"""
SQLite storage: companies (incl. market_cap/P-B), filings, news, scores,
subscribers, SEC XBRL fundamentals, stored Alpha Vantage OVERVIEW blobs, and a
daily score-history snapshot for week-over-week trend tracking.
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik TEXT PRIMARY KEY, ticker TEXT, name TEXT, market_cap REAL,
    pb_ratio REAL, tsr_1y REAL, tsr_3y REAL, tsr_5y REAL, updated_at TEXT
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
    first_flagged TEXT, evidence TEXT, active_situation INTEGER, vuln INTEGER,
    situation_tier TEXT, situation_meta TEXT, pitch TEXT, fin_context TEXT,
    peer_analysis TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS subscribers (
    email TEXT PRIMARY KEY, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, message TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_lead (
    day INTEGER PRIMARY KEY, cik TEXT, chosen_at TEXT
);
CREATE TABLE IF NOT EXISTS fundamentals (
    cik TEXT PRIMARY KEY, ticker TEXT, sector TEXT,
    revenue REAL, revenue_growth REAL, operating_margin REAL, sga_pct REAL,
    roa REAL, cash_to_assets REAL, debt_to_assets REAL,
    shares REAL, book_equity REAL, raw TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS av_overview (
    cik TEXT PRIMARY KEY, ticker TEXT, data TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS score_history (
    cik TEXT, date TEXT, score INTEGER, PRIMARY KEY (cik, date)
);
CREATE TABLE IF NOT EXISTS watchlist (
    cik TEXT PRIMARY KEY, ticker TEXT, company TEXT,
    note TEXT, added_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS governance (
    cik TEXT PRIMARY KEY, classified_board INTEGER, poison_pill INTEGER,
    dual_class INTEGER, proxy_accn TEXT, proxy_date TEXT, proxy_url TEXT,
    comp_json TEXT, peers_json TEXT, meeting_date TEXT,
    nom_min_days INTEGER, nom_max_days INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS insider_txn (
    accn TEXT PRIMARY KEY, cik TEXT, filed TEXT, name TEXT, role TEXT,
    buy_value REAL, sell_value REAL, url TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS insider (
    cik TEXT PRIMARY KEY, buy_value REAL, sell_value REAL, net_value REAL,
    n_buyers INTEGER, n_sellers INTEGER, last_date TEXT, top_url TEXT,
    window_days INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS activist_flag (
    cik TEXT PRIMARY KEY, kind TEXT, form TEXT, label TEXT,
    filed TEXT, url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS votes (
    cik TEXT PRIMARY KEY, say_on_pay REAL, low_director REAL, director_name TEXT,
    meeting_date TEXT, accn TEXT, url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS earnings (
    cik TEXT PRIMARY KEY, ticker TEXT, next_date TEXT, last_date TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    cik TEXT PRIMARY KEY, note TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS finnhub_extra (
    cik TEXT PRIMARY KEY, mspr REAL, mspr_month TEXT,
    rec_strongbuy INTEGER, rec_buy INTEGER, rec_hold INTEGER,
    rec_sell INTEGER, rec_strongsell INTEGER, rec_period TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS company_profile (
    cik TEXT PRIMARY KEY, ceo TEXT, phone TEXT, address TEXT, city TEXT, state TEXT,
    zip TEXT, country TEXT, employees INTEGER, ipo TEXT, website TEXT,
    sector TEXT, industry TEXT, description TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    cik TEXT PRIMARY KEY, series TEXT, last_close REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS exec_reactions (
    cik TEXT PRIMARY KEY, ticker TEXT, filed_at TEXT, event_date TEXT,
    move REAL, bench_move REAL, abnormal REAL, title TEXT, url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_pitch (
    cik TEXT PRIMARY KEY, hash TEXT, pitch TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS company_contacts (
    cik TEXT PRIMARY KEY, ir_name TEXT, ir_email TEXT, ir_phone TEXT,
    comms_name TEXT, comms_email TEXT, comms_phone TEXT, source_url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS advisors (
    cik TEXT PRIMARY KEY, advisors_json TEXT, source_url TEXT, source_date TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS manual_situations (
    cik TEXT PRIMARY KEY, status TEXT, actor TEXT, note TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS situation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cik TEXT, company TEXT, action TEXT,
    actor TEXT, note TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS entity (
    cik TEXT PRIMARY KEY, ticker TEXT, name TEXT, sector TEXT, industry TEXT,
    exchange TEXT, market_cap REAL, index_tags TEXT, cusip TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip TEXT PRIMARY KEY, ticker TEXT, name TEXT, source TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS activist_filers (
    fund TEXT PRIMARY KEY, cik TEXT, source TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS activist_holdings (
    ticker TEXT, fund TEXT, fund_cik TEXT, cusip TEXT,
    value REAL, shares REAL, weight_in_fund REAL, ownership_pct REAL,
    filed TEXT, quarter TEXT, updated_at TEXT,
    PRIMARY KEY (ticker, fund)
);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON activist_holdings (ticker);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # readers + writer don't block each other
    conn.execute("PRAGMA busy_timeout=10000")   # wait up to 10s for a lock instead of erroring
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Safe migrations for DBs created before these columns existed.
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(scores)")]
        if "evidence" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN evidence TEXT")
        if "active_situation" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN active_situation INTEGER")
        if "vuln" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN vuln INTEGER")
        if "situation_tier" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN situation_tier TEXT")
        if "situation_meta" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN situation_meta TEXT")
        if "pitch" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN pitch TEXT")
        if "fin_context" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN fin_context TEXT")
        if "peer_analysis" not in scols:
            conn.execute("ALTER TABLE scores ADD COLUMN peer_analysis TEXT")
        ccols = [r["name"] for r in conn.execute("PRAGMA table_info(companies)")]
        if "tsr_5y" not in ccols:
            conn.execute("ALTER TABLE companies ADD COLUMN tsr_5y REAL")
        gcols = [r["name"] for r in conn.execute("PRAGMA table_info(governance)")]
        if "comp_json" not in gcols:
            conn.execute("ALTER TABLE governance ADD COLUMN comp_json TEXT")
        if "peers_json" not in gcols:
            conn.execute("ALTER TABLE governance ADD COLUMN peers_json TEXT")
        if "meeting_date" not in gcols:
            conn.execute("ALTER TABLE governance ADD COLUMN meeting_date TEXT")
        if "nom_min_days" not in gcols:
            conn.execute("ALTER TABLE governance ADD COLUMN nom_min_days INTEGER")
        if "nom_max_days" not in gcols:
            conn.execute("ALTER TABLE governance ADD COLUMN nom_max_days INTEGER")
        fcols = [r["name"] for r in conn.execute("PRAGMA table_info(fundamentals)")]
        if "raw" not in fcols:
            conn.execute("ALTER TABLE fundamentals ADD COLUMN raw TEXT")


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


def set_company_market(cik, market_cap=None, pb_ratio=None, tsr_1y=None, tsr_3y=None,
                       tsr_5y=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE companies SET
                 market_cap=COALESCE(?, market_cap), pb_ratio=COALESCE(?, pb_ratio),
                 tsr_1y=COALESCE(?, tsr_1y), tsr_3y=COALESCE(?, tsr_3y),
                 tsr_5y=COALESCE(?, tsr_5y),
                 updated_at=? WHERE cik=?""",
            (market_cap, pb_ratio, tsr_1y, tsr_3y, tsr_5y, now_iso(), cik),
        )


def get_companies():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM companies")]


def get_company_market(cik):
    """Look up a company row by CIK, tolerating padded or unpadded form
    (companies stores the unpadded CIK; scores/fundamentals store padded)."""
    try:
        unpadded = str(int(cik))
    except (ValueError, TypeError):
        unpadded = str(cik)
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM companies WHERE cik IN (?,?)", (str(cik), unpadded)
        ).fetchone()
        return dict(r) if r else {}


# --- Fundamentals ------------------------------------------------------------
def upsert_fundamentals(cik, ticker, sector, m, raw=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO fundamentals
               (cik,ticker,sector,revenue,revenue_growth,operating_margin,sga_pct,
                roa,cash_to_assets,debt_to_assets,shares,book_equity,raw,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, sector=excluded.sector,
                 revenue=excluded.revenue, revenue_growth=excluded.revenue_growth,
                 operating_margin=excluded.operating_margin, sga_pct=excluded.sga_pct,
                 roa=excluded.roa, cash_to_assets=excluded.cash_to_assets,
                 debt_to_assets=excluded.debt_to_assets, shares=excluded.shares,
                 book_equity=excluded.book_equity, raw=excluded.raw,
                 updated_at=excluded.updated_at""",
            (cik, ticker, sector, m.get("revenue"), m.get("revenue_growth"),
             m.get("operating_margin"), m.get("sga_pct"), m.get("roa"),
             m.get("cash_to_assets"), m.get("debt_to_assets"),
             m.get("shares"), m.get("book_equity"), json.dumps(raw or {}), now_iso()),
        )


def get_all_fundamentals():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM fundamentals")]


def get_fundamentals_one(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM fundamentals WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


# --- Entity master (U2): queryable per-company attributes --------------------
def upsert_entity(cik, ticker=None, name=None, sector=None, industry=None,
                  exchange=None, market_cap=None, index_tags=None, cusip=None):
    """Upsert one entity row. Any field left None is preserved (COALESCE), so a
    partial update (e.g. index_tags only, or a Finnhub miss) never wipes good data."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO entity
                 (cik,ticker,name,sector,industry,exchange,market_cap,index_tags,cusip,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=COALESCE(excluded.ticker, entity.ticker),
                 name=COALESCE(excluded.name, entity.name),
                 sector=COALESCE(excluded.sector, entity.sector),
                 industry=COALESCE(excluded.industry, entity.industry),
                 exchange=COALESCE(excluded.exchange, entity.exchange),
                 market_cap=COALESCE(excluded.market_cap, entity.market_cap),
                 index_tags=COALESCE(excluded.index_tags, entity.index_tags),
                 cusip=COALESCE(excluded.cusip, entity.cusip),
                 updated_at=excluded.updated_at""",
            (cik, ticker, name, sector, industry, exchange, market_cap,
             index_tags, cusip, now_iso()),
        )


def get_entity(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM entity WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_entities():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM entity ORDER BY ticker")]


def get_entity_by_ticker(ticker):
    """Entity-master row for a ticker (case-insensitive). Powers the debug lookup + U3 search."""
    if not ticker:
        return {}
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM entity WHERE UPPER(ticker)=UPPER(?)",
                         (ticker,)).fetchone()
        return dict(r) if r else {}


def ticker_for_cusip(cusip):
    """Resolve a CUSIP to a ticker: the FTD/OpenFIGI cusip_map first, then entity."""
    if not cusip:
        return None
    with get_conn() as conn:
        r = conn.execute("SELECT ticker FROM cusip_map WHERE cusip=?", (cusip,)).fetchone()
        if r:
            return r["ticker"]
        r = conn.execute("SELECT ticker FROM entity WHERE cusip=?", (cusip,)).fetchone()
        return r["ticker"] if r else None


# --- CUSIP map (U2b): free cusip -> ticker spine (FTD / OpenFIGI) -------------
def upsert_cusips(rows):
    """Bulk upsert of (cusip, ticker, name, source) tuples in one transaction."""
    ts = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO cusip_map (cusip,ticker,name,source,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cusip) DO UPDATE SET
                 ticker=excluded.ticker,
                 name=COALESCE(excluded.name, cusip_map.name),
                 source=excluded.source, updated_at=excluded.updated_at""",
            [(c, t, n, s, ts) for (c, t, n, s) in rows],
        )


def upsert_cusip(cusip, ticker, name=None, source="ftd"):
    upsert_cusips([(cusip, ticker, name, source)])


def cusip_map_count():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM cusip_map").fetchone()["n"]


def entity_stats():
    """Fill stats for the entity master + cusip map (U2 validation)."""
    with get_conn() as conn:
        def n(sql):
            return conn.execute(sql).fetchone()["n"]
        idx = {}
        for r in conn.execute(
                "SELECT COALESCE(NULLIF(index_tags,''),'(none)') AS t, COUNT(*) AS n "
                "FROM entity GROUP BY t"):
            idx[r["t"]] = r["n"]
        return {
            "entity_rows": n("SELECT COUNT(*) AS n FROM entity"),
            "with_market_cap": n("SELECT COUNT(*) AS n FROM entity WHERE market_cap IS NOT NULL"),
            "with_sector": n("SELECT COUNT(*) AS n FROM entity WHERE sector IS NOT NULL AND sector<>''"),
            "with_cusip": n("SELECT COUNT(*) AS n FROM entity WHERE cusip IS NOT NULL AND cusip<>''"),
            "index_tags": idx,
            "cusip_map_size": n("SELECT COUNT(*) AS n FROM cusip_map"),
        }


# --- 13F activist holders (early-warning signal) -----------------------------
def upsert_activist_filer(fund, cik, source="browse-edgar"):
    """Cache a fund's resolved 13F-filing CIK so we don't re-resolve every run."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activist_filers (fund,cik,source,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(fund) DO UPDATE SET cik=excluded.cik, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (fund, cik, source, now_iso()))


def get_activist_filers():
    """{fund: cik} of resolved 13F filers."""
    with get_conn() as conn:
        return {r["fund"]: r["cik"]
                for r in conn.execute("SELECT fund, cik FROM activist_filers")}


def replace_holdings_for_fund(fund, rows):
    """Replace ONE fund's holdings with its latest 13F (a fresh 13F supersedes the old one).
    Each row is (ticker, fund, fund_cik, cusip, value, shares, weight_in_fund, ownership_pct,
    filed, quarter)."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute("DELETE FROM activist_holdings WHERE fund=?", (fund,))
        conn.executemany(
            """INSERT INTO activist_holdings
               (ticker,fund,fund_cik,cusip,value,shares,weight_in_fund,ownership_pct,
                filed,quarter,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [tuple(r) + (ts,) for r in rows])


def holders_for_all():
    """{TICKER: [ {fund, fund_cik, value, shares, weight_in_fund, ownership_pct, filed,
    quarter}, ... ]} across every stored 13F holding, strongest conviction first. Scoring
    applies the materiality gate; this returns everything so the gate lives in one place."""
    out = {}
    with get_conn() as conn:
        for r in conn.execute(
                "SELECT * FROM activist_holdings "
                "ORDER BY COALESCE(weight_in_fund,0) DESC"):
            out.setdefault((r["ticker"] or "").upper(), []).append(dict(r))
    return out


def holders_for_ticker(ticker):
    if not ticker:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM activist_holdings WHERE UPPER(ticker)=UPPER(?) "
            "ORDER BY COALESCE(weight_in_fund,0) DESC", (ticker,))]


def activist_holdings_count():
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM activist_holdings").fetchone()["n"]


def clear_all_holdings():
    """Wipe the holdings table before a full refresh so stale rows from renamed/removed/deduped
    funds (e.g. an old 'third point' alias, a fund that stopped filing) can't linger."""
    with get_conn() as conn:
        conn.execute("DELETE FROM activist_holdings")


def _holder_material(h):
    """Materiality = a CONCENTRATED activist stake with real influence over the COMPANY. Needs two
    things: enough of the company (ownership), AND enough of the fund's own book (conviction) — the
    latter is what separates a real activist from a quant/multi-strat fund that owns 1% of hundreds
    of names. Qualifies at >=1% of the company AND >=1% of the fund's book, OR >=0.5% of the company
    when it's a concentrated >=5% of the book. Ownership must be present and not a >100% data error."""
    o = h.get("ownership_pct")
    w = h.get("weight_in_fund") or 0
    if o is None or o > config.OWNERSHIP_MAX:
        return False
    if o >= config.OWNERSHIP_PCT_MIN and w >= config.FUND_WEIGHT_FLOOR:
        return True
    return o >= config.OWNERSHIP_SOFT and w >= config.FUND_WEIGHT_CONV


def accumulating():
    """The 'Accumulating' list: universe names where a known activist holds a MATERIAL stake
    (see _holder_material) but the name is NOT already an active situation (no 13D / contested
    proxy / reported campaign) — i.e. the activist is in the stock but hasn't agitated yet. Each
    entry carries the material holder(s), the company display info, and its vuln score if it's
    also on the pitch board. Sorted by number of activist holders, then strongest conviction."""
    with get_conn() as conn:
        active = {(r["ticker"] or "").upper()
                  for r in conn.execute(
                      "SELECT ticker FROM scores WHERE active_situation=1 AND ticker IS NOT NULL")}
        comp = {}
        for r in conn.execute(
                "SELECT cik, ticker, name, market_cap FROM companies WHERE ticker IS NOT NULL"):
            comp[(r["ticker"] or "").upper()] = dict(r)
        score = {}
        for r in conn.execute(
                "SELECT ticker, cik, vuln, score FROM scores WHERE ticker IS NOT NULL"):
            score[(r["ticker"] or "").upper()] = dict(r)
        # Reliable, universe-wide market caps (Finnhub, via the entity master) — the enriched
        # companies.market_cap is null for un-scored names, and implying cap from the 13F inherits
        # any bad shares-outstanding, so entity is the trustworthy mega-cap source.
        ent_mkt = {}
        for r in conn.execute("SELECT ticker, market_cap FROM entity "
                              "WHERE ticker IS NOT NULL AND market_cap IS NOT NULL"):
            ent_mkt[(r["ticker"] or "").upper()] = r["market_cap"]
        byt = {}
        for r in conn.execute(
                "SELECT * FROM activist_holdings ORDER BY COALESCE(weight_in_fund,0) DESC"):
            byt.setdefault((r["ticker"] or "").upper(), []).append(dict(r))

    out = []
    for tk, hs in byt.items():
        if tk in active:                          # already agitating -> not "accumulating"
            continue
        # Dedupe holders that resolve to the same 13F filer CIK (two list aliases of one firm,
        # e.g. "third point" / "third point llc"), keeping the strongest-weight instance.
        seen, uniq = set(), []
        for h in hs:
            key = h.get("fund_cik") or h.get("fund")
            if key in seen:
                continue
            seen.add(key); uniq.append(h)
        if tk in getattr(config, "MEGA_CAP_DENY", set()):
            continue                              # largest mega-caps: never a stealth accumulation
        material = [h for h in uniq if _holder_material(h)]
        if not material:
            continue
        ci = comp.get(tk, {})
        # Market cap: entity (reliable, universe-wide) first, then enriched companies, then imply
        # from the 13F as a last resort. Entity is trusted because implied cap inherits any bad
        # shares-outstanding (which was letting Mastercard's inflated ownership slip a $500B name).
        mkt = ent_mkt.get(tk) or ci.get("market_cap")
        if not mkt:
            for h in material:
                v, o = h.get("value"), h.get("ownership_pct")
                if v and o:
                    mkt = v / o
                    break
        if mkt and mkt > config.MEGA_CAP_MAX:
            continue                              # mega-cap -> a holder here is a passive long
        sc = score.get(tk, {})
        top = material[0]
        max_own = max((h.get("ownership_pct") or 0) for h in material)
        out.append({
            "disclosed": max_own >= config.OWNERSHIP_DISCLOSED,   # >=5% -> 13D/13G on file
            "ticker": tk,
            "company": ci.get("name") or tk,
            "cik": ci.get("cik") or sc.get("cik"),
            "market_cap": ci.get("market_cap"),
            "vuln": sc.get("vuln"),
            "on_board": tk in score,               # also a proactive lead?
            "n_holders": len(material),
            "top_weight": top.get("weight_in_fund"),
            "top_ownership": top.get("ownership_pct"),
            "quarter": top.get("quarter"),
            "filed": top.get("filed"),
            "holders": material,
        })
    out.sort(key=lambda x: (x["n_holders"], x["top_weight"] or 0, x["top_ownership"] or 0),
             reverse=True)
    return out


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


def news_all_brief():
    """Every stored headline (id/headline/url/source) — for re-applying exclude rules to items
    ingested before a pattern existed."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id, headline, url, source FROM news")]


def delete_news(ids):
    if not ids:
        return 0
    with get_conn() as conn:
        conn.executemany("DELETE FROM news WHERE id=?", [(i,) for i in ids])
    return len(ids)


def _dedup_news(rows, limit):
    """Collapse repeats of the same headline (syndicated wires / cross-source dupes),
    keeping the newest, until `limit` items."""
    seen, out = set(), []
    for r in rows:
        k = re.sub(r"\s+", " ", (r.get("headline") or "").strip().lower())
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def recent_news(limit=40, relevant_only=False):
    """Most-recent stored headlines, de-duplicated. When relevant_only is set, only
    headlines that pass the broad-feed relevance filter (news.is_relevant) are returned
    -- used for the curated 'Top headlines' panels and the emailed pitch kit, so routine
    per-company items (earnings dates, product launches, insider option exercises) don't
    surface there. The Live feed and company-profile news tabs leave relevant_only=False
    to keep full coverage."""
    fetch = limit * (8 if relevant_only else 3)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM news ORDER BY published_at DESC LIMIT ?", (fetch,))]
    if relevant_only:
        from . import news  # local import avoids a circular import at module load
        rows = [r for r in rows if news.is_relevant(r.get("headline") or "")]
    return _dedup_news(rows, limit)


def news_for_ticker_in_window(ticker, days):
    cutoff = _cutoff(days)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM news WHERE published_at>=?
               AND (','||matched_tickers||',') LIKE ? ORDER BY published_at DESC""",
            (cutoff, f"%,{ticker},%"))]


def get_news_for_ticker(ticker, limit=10):
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM news WHERE (','||matched_tickers||',') LIKE ?
               ORDER BY published_at DESC LIMIT ?""", (f"%,{ticker},%", limit * 3))]
    return _dedup_news(rows, limit)


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
                    top_item_url,first_flagged,evidence,active_situation,vuln,
                    situation_tier,situation_meta,pitch,fin_context,peer_analysis,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["cik"], r["ticker"], r["company"], r["market_cap"], r["score"],
                 r["signals"], r["top_item_title"], r["top_item_url"], first,
                 json.dumps(r.get("evidence") or []), r.get("active_situation") or 0,
                 r.get("vuln") or 0, r.get("situation_tier") or "",
                 json.dumps(r.get("situation_meta") or {}),
                 json.dumps(r.get("pitch") or {}),
                 json.dumps(r.get("fin_context") or []),
                 json.dumps(r.get("peer_analysis") or {}), now_iso()),
            )
        today = datetime.utcnow().date().isoformat()
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO score_history (cik,date,score) VALUES (?,?,?)",
                (r["cik"], today, r["score"]),
            )


def get_scores(limit=15):
    """The proactive shortlist -- excludes companies where an activist is already engaged."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scores WHERE COALESCE(active_situation,0)=0 "
            "ORDER BY COALESCE(vuln,0) DESC, score DESC, company ASC LIMIT ?", (limit,))]


def get_active_situations(limit=40):
    """Flagged companies where an activist has already shown up (too late to pitch)."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scores WHERE active_situation=1 "
            "ORDER BY COALESCE(vuln,0) DESC, score DESC, company ASC LIMIT ?", (limit,))]


def get_score_one(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM scores WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_score_history(cik, limit=60):
    """Daily raw-score snapshots (oldest first) for the rating-trend sparkline."""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT date, score FROM score_history WHERE cik=? ORDER BY date ASC", (cik,))]
    return rows[-limit:]


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


# --- Feedback (team comments from the How-it-works page) ----------------------
def add_feedback(name, message):
    with get_conn() as conn:
        conn.execute("INSERT INTO feedback (name,message,created_at) VALUES (?,?,?)",
                     ((name or "").strip()[:120], (message or "").strip()[:4000], now_iso()))


def get_feedback(limit=50):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))]


# --- Daily lead-of-the-day (no-repeat rotation) ------------------------------
def get_lead_for_day(day):
    with get_conn() as conn:
        r = conn.execute("SELECT cik FROM daily_lead WHERE day=?", (day,)).fetchone()
        return r["cik"] if r else None


def record_daily_lead(day, cik):
    with get_conn() as conn:
        conn.execute("INSERT INTO daily_lead (day,cik,chosen_at) VALUES (?,?,?) "
                     "ON CONFLICT(day) DO UPDATE SET cik=excluded.cik, chosen_at=excluded.chosen_at",
                     (day, cik, now_iso()))


def get_lead_history(window_days, current_day):
    """{cik: most-recent day it was lead} within the prior `window_days` (excluding today).
    Lets the picker prefer never-featured names, then the least-recently-featured."""
    out = {}
    with get_conn() as conn:
        for r in conn.execute(
                "SELECT day, cik FROM daily_lead WHERE day < ? AND day >= ? ORDER BY day",
                (current_day, current_day - window_days)):
            out[r["cik"]] = r["day"]          # ascending order -> ends on the latest day
    return out


# --- Watchlist (shared) ------------------------------------------------------
def add_watchlist(cik, ticker, company):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO watchlist (cik,ticker,company,note,added_at,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, company=excluded.company,
                 updated_at=excluded.updated_at""",
            (cik, ticker, company, "", now_iso(), now_iso()),
        )


def remove_watchlist(cik):
    with get_conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE cik=?", (cik,))


def set_watchlist_note(cik, note):
    with get_conn() as conn:
        conn.execute("UPDATE watchlist SET note=?, updated_at=? WHERE cik=?",
                     (note, now_iso(), cik))


def get_watchlist():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM watchlist ORDER BY added_at DESC")]


# --- Meta key/value (benchmarks, versions, etc.) -----------------------------
def set_meta(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def get_meta(key, default=None):
    with get_conn() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


# --- Governance (from DEF 14A proxies) ---------------------------------------
def upsert_governance(cik, flags, accn, date, url, comp=None, peers=None, meeting_date=None,
                      nom_min=None, nom_max=None):
    # comp/peers are None when not yet attempted; {} / [] are "parsed, nothing found"
    # sentinels that stop us re-fetching the same proxy every run. Stored non-None once tried.
    comp_json = json.dumps(comp) if comp is not None else None
    peers_json = json.dumps(peers) if peers is not None else None
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO governance
               (cik,classified_board,poison_pill,dual_class,proxy_accn,proxy_date,proxy_url,comp_json,peers_json,meeting_date,nom_min_days,nom_max_days,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 classified_board=excluded.classified_board, poison_pill=excluded.poison_pill,
                 dual_class=excluded.dual_class, proxy_accn=excluded.proxy_accn,
                 proxy_date=excluded.proxy_date, proxy_url=excluded.proxy_url,
                 comp_json=excluded.comp_json, peers_json=excluded.peers_json,
                 meeting_date=excluded.meeting_date, nom_min_days=excluded.nom_min_days,
                 nom_max_days=excluded.nom_max_days, updated_at=excluded.updated_at""",
            (cik, 1 if flags.get("classified_board") else 0,
             1 if flags.get("poison_pill") else 0, 1 if flags.get("dual_class") else 0,
             accn, date, url, comp_json, peers_json, meeting_date, nom_min, nom_max, now_iso()),
        )


def get_governance(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM governance WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_governance():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM governance")}


# --- Insider transactions (Form 4) -------------------------------------------
def insider_txn_seen(accn):
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM insider_txn WHERE accn=?", (accn,)).fetchone() is not None


def add_insider_txn(accn, cik, filed, name, role, buy_value, sell_value, url):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO insider_txn
               (accn,cik,filed,name,role,buy_value,sell_value,url,ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (accn, cik, filed, name, role, buy_value, sell_value, url, now_iso()))


def recompute_insider(cik, window_days=120):
    """Aggregate cached Form-4 open-market buys/sells for one CIK over the window."""
    cut = (datetime.utcnow() - timedelta(days=window_days)).date().isoformat()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM insider_txn WHERE cik=? AND filed>=?", (cik, cut))]
        buy_value = sum(r["buy_value"] or 0 for r in rows)
        sell_value = sum(r["sell_value"] or 0 for r in rows)
        n_buyers = len({r["name"] for r in rows if (r["buy_value"] or 0) > 0 and r["name"]})
        n_sellers = len({r["name"] for r in rows if (r["sell_value"] or 0) > 0 and r["name"]})
        last_date = max((r["filed"] for r in rows if r["filed"]), default=None)
        # surface the largest single open-market filing as the headline link
        ranked = sorted(rows, key=lambda r: ((r["buy_value"] or 0) + (r["sell_value"] or 0)),
                        reverse=True)
        top_url = ranked[0]["url"] if ranked else None
        conn.execute(
            """INSERT INTO insider
               (cik,buy_value,sell_value,net_value,n_buyers,n_sellers,last_date,top_url,window_days,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 buy_value=excluded.buy_value, sell_value=excluded.sell_value,
                 net_value=excluded.net_value, n_buyers=excluded.n_buyers,
                 n_sellers=excluded.n_sellers, last_date=excluded.last_date,
                 top_url=excluded.top_url, window_days=excluded.window_days,
                 updated_at=excluded.updated_at""",
            (cik, buy_value, sell_value, buy_value - sell_value, n_buyers, n_sellers,
             last_date, top_url, window_days, now_iso()))
        return {"buy_value": buy_value, "sell_value": sell_value,
                "n_buyers": n_buyers, "n_sellers": n_sellers}


def get_insider(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM insider WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_insider():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM insider")}


# --- Activist filings (13D/13G + dissident proxy) ----------------------------
def set_activist_flag(cik, kind, form, label, filed, url):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO activist_flag (cik,kind,form,label,filed,url,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 kind=excluded.kind, form=excluded.form, label=excluded.label,
                 filed=excluded.filed, url=excluded.url, updated_at=excluded.updated_at""",
            (cik, kind, form, label, filed, url, now_iso()))


def clear_activist_flag(cik):
    with get_conn() as conn:
        conn.execute("DELETE FROM activist_flag WHERE cik=?", (cik,))


def get_activist_flag(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM activist_flag WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_activist_flags():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM activist_flag")}


def reset_situations_once(version):
    """One-time clean slate for Active Situations, gated by a version string in `meta` so it
    runs exactly once per version bump (not every boot). Clears the hand-set manual tags, the
    activist_flag table, and any lingering situation columns on `scores` -- then the normal
    sweep + recompute repopulate only fresh, in-window (<=18-month) situations."""
    if get_meta("situations_reset") == version:
        return False
    with get_conn() as conn:
        conn.execute("DELETE FROM manual_situations")
        conn.execute("DELETE FROM activist_flag")
        conn.execute("UPDATE scores SET active_situation=0, situation_tier='', "
                     "situation_meta=NULL")
    set_meta("situations_reset", version)
    print(f"[situations] clean-slate reset applied (version={version})")
    return True


# --- Manual active-situation overrides + audit trail -------------------------
# A partner can force a company INTO active situations ("active") or suppress a
# false-positive auto-detection ("exclude"). Overrides always win over automated
# detection, and every change is logged for an audit trail.
def get_manual_situations():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM manual_situations")}


def get_manual_situation(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM manual_situations WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def set_manual_situation(cik, status, actor="", note="", company=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO manual_situations (cik,status,actor,note,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET status=excluded.status,
                 actor=excluded.actor, note=excluded.note, updated_at=excluded.updated_at""",
            (cik, status, actor or "", note or "", now_iso()))
        conn.execute(
            "INSERT INTO situation_audit (cik,company,action,actor,note,ts) VALUES (?,?,?,?,?,?)",
            (cik, company or "", "set " + status, actor or "", note or "", now_iso()))


def clear_manual_situation(cik, actor="", note="", company=""):
    with get_conn() as conn:
        conn.execute("DELETE FROM manual_situations WHERE cik=?", (cik,))
        conn.execute(
            "INSERT INTO situation_audit (cik,company,action,actor,note,ts) VALUES (?,?,?,?,?,?)",
            (cik, company or "", "cleared override", actor or "", note or "", now_iso()))


def get_situation_audit(cik=None, limit=200):
    with get_conn() as conn:
        if cik:
            rows = conn.execute(
                "SELECT * FROM situation_audit WHERE cik=? ORDER BY ts DESC LIMIT ?",
                (cik, limit))
        else:
            rows = conn.execute(
                "SELECT * FROM situation_audit ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# --- Shareholder votes (8-K Item 5.07) ---------------------------------------
def upsert_votes(cik, say_on_pay, low_director, director_name, meeting_date, accn, url):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO votes
               (cik,say_on_pay,low_director,director_name,meeting_date,accn,url,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 say_on_pay=excluded.say_on_pay, low_director=excluded.low_director,
                 director_name=excluded.director_name, meeting_date=excluded.meeting_date,
                 accn=excluded.accn, url=excluded.url, updated_at=excluded.updated_at""",
            (cik, say_on_pay, low_director, director_name, meeting_date, accn, url, now_iso()))


def votes_accn_seen(cik, accn):
    with get_conn() as conn:
        r = conn.execute("SELECT accn FROM votes WHERE cik=?", (cik,)).fetchone()
        return bool(r and r["accn"] == accn)


def get_votes(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM votes WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_votes():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM votes")}


# --- Earnings calendar (timing) ----------------------------------------------
def upsert_earnings(cik, ticker, next_date, last_date):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO earnings (cik,ticker,next_date,last_date,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, next_date=excluded.next_date,
                 last_date=excluded.last_date, updated_at=excluded.updated_at""",
            (cik, ticker, next_date, last_date, now_iso()))


def get_earnings(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM earnings WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


# --- Company-level pitch notes (shared; the single source of truth) ----------
def get_note(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT note FROM notes WHERE cik=?", (cik,)).fetchone()
        return (r["note"] if r else "") or ""


def set_note(cik, note):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO notes (cik,note,updated_at) VALUES (?,?,?)
               ON CONFLICT(cik) DO UPDATE SET note=excluded.note, updated_at=excluded.updated_at""",
            (cik, note or "", now_iso()))


def get_notes_set():
    """Set of CIKs that have a non-empty note (for 'has note' indicators)."""
    with get_conn() as conn:
        return {r["cik"] for r in conn.execute(
            "SELECT cik FROM notes WHERE note IS NOT NULL AND TRIM(note) <> ''")}


# --- Finnhub extras: insider sentiment (MSPR) + analyst recommendations -------
def upsert_finnhub_extra(cik, d):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO finnhub_extra
               (cik,mspr,mspr_month,rec_strongbuy,rec_buy,rec_hold,rec_sell,rec_strongsell,rec_period,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 mspr=excluded.mspr, mspr_month=excluded.mspr_month,
                 rec_strongbuy=excluded.rec_strongbuy, rec_buy=excluded.rec_buy,
                 rec_hold=excluded.rec_hold, rec_sell=excluded.rec_sell,
                 rec_strongsell=excluded.rec_strongsell, rec_period=excluded.rec_period,
                 updated_at=excluded.updated_at""",
            (cik, d.get("mspr"), d.get("mspr_month"), d.get("rec_strongbuy"), d.get("rec_buy"),
             d.get("rec_hold"), d.get("rec_sell"), d.get("rec_strongsell"), d.get("rec_period"),
             now_iso()))


def get_finnhub_extra(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM finnhub_extra WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


# --- FMP company profile (contacts + description) + price history -------------
def upsert_company_profile(cik, d):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO company_profile
               (cik,ceo,phone,address,city,state,zip,country,employees,ipo,website,sector,industry,description,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ceo=excluded.ceo, phone=excluded.phone, address=excluded.address, city=excluded.city,
                 state=excluded.state, zip=excluded.zip, country=excluded.country, employees=excluded.employees,
                 ipo=excluded.ipo, website=excluded.website, sector=excluded.sector, industry=excluded.industry,
                 description=excluded.description, updated_at=excluded.updated_at""",
            (cik, d.get("ceo"), d.get("phone"), d.get("address"), d.get("city"), d.get("state"),
             d.get("zip"), d.get("country"), d.get("employees"), d.get("ipo"), d.get("website"),
             d.get("sector"), d.get("industry"), d.get("description"), now_iso()))


def get_company_profile(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM company_profile WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def upsert_company_contacts(cik, d):
    """d: {'ir': {name,email,phone}|None, 'comms': {...}|None, 'source_url': ...}."""
    ir = d.get("ir") or {}
    co = d.get("comms") or {}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO company_contacts
               (cik,ir_name,ir_email,ir_phone,comms_name,comms_email,comms_phone,source_url,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ir_name=excluded.ir_name, ir_email=excluded.ir_email, ir_phone=excluded.ir_phone,
                 comms_name=excluded.comms_name, comms_email=excluded.comms_email,
                 comms_phone=excluded.comms_phone, source_url=excluded.source_url,
                 updated_at=excluded.updated_at""",
            (cik, ir.get("name"), ir.get("email"), ir.get("phone"),
             co.get("name"), co.get("email"), co.get("phone"), d.get("source_url"), now_iso()))


def get_company_contacts(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM company_contacts WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def upsert_advisors(cik, advisors, source_url=None, source_date=None):
    """advisors: list of {'name','type'}. Empty list is a valid 'scanned, none found' result."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO advisors (cik, advisors_json, source_url, source_date, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET advisors_json=excluded.advisors_json,
                 source_url=excluded.source_url, source_date=excluded.source_date,
                 updated_at=excluded.updated_at""",
            (cik, json.dumps(advisors or []), source_url, source_date, now_iso()))


def get_advisors(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM advisors WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def clear_advisors():
    """Wipe the advisors cache so every tracked name re-scans. Used on a scanner-version bump,
    so a widened scanner (e.g. adding offering/prospectus filings) reaches names already cached
    under the old, narrower logic instead of waiting out their 60-day freshness window."""
    with get_conn() as conn:
        conn.execute("DELETE FROM advisors")


def upsert_prices(cik, series, last_close):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO prices (cik,series,last_close,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET series=excluded.series,
                 last_close=excluded.last_close, updated_at=excluded.updated_at""",
            (cik, json.dumps(series or []), last_close, now_iso()))


def get_prices(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM prices WHERE cik=?", (cik,)).fetchone()
        if not r:
            return {}
        d = dict(r)
        try:
            d["series"] = json.loads(d.get("series") or "[]")
        except (ValueError, TypeError):
            d["series"] = []
        return d


def upsert_exec_reaction(cik, ticker, filed_at, event_date, move, bench_move,
                         abnormal, title, url):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO exec_reactions
               (cik,ticker,filed_at,event_date,move,bench_move,abnormal,title,url,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, filed_at=excluded.filed_at,
                 event_date=excluded.event_date, move=excluded.move,
                 bench_move=excluded.bench_move, abnormal=excluded.abnormal,
                 title=excluded.title, url=excluded.url, updated_at=excluded.updated_at""",
            (cik, ticker, filed_at, event_date, move, bench_move, abnormal,
             title, url, now_iso()))


def get_exec_reaction(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM exec_reactions WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def get_all_exec_reactions():
    with get_conn() as conn:
        return {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM exec_reactions")}


def get_ai_pitch(cik):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM ai_pitch WHERE cik=?", (cik,)).fetchone()
        return dict(r) if r else {}


def upsert_ai_pitch(cik, hash_, pitch):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ai_pitch (cik,hash,pitch,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET hash=excluded.hash,
                 pitch=excluded.pitch, updated_at=excluded.updated_at""",
            (cik, hash_, json.dumps(pitch or {}), now_iso()))


def _cutoff(days):
    return (datetime.utcnow() - timedelta(days=days)).isoformat()
