import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "twr.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # IBKR daily position values (one row per symbol per date, already in ILS)
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,        -- YYYY-MM-DD
            symbol        TEXT NOT NULL,
            position_value_ils REAL NOT NULL,
            fx_rate       REAL NOT NULL DEFAULT 1,
            UNIQUE(date, symbol)
        )
    """)

    # All cashflow transactions (IBKR + IBI), net of tax where applicable
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,        -- YYYY-MM-DD
            broker        TEXT NOT NULL,        -- 'IBKR' | 'IBI'
            tx_type       TEXT NOT NULL,        -- 'Buy' | 'Sell' | 'Dividend'
            security      TEXT NOT NULL,
            amount_ils    REAL NOT NULL,        -- net of tax, already in ILS
            UNIQUE(date, broker, tx_type, security, amount_ils)
        )
    """)

    # TASE end-of-day prices (in ILS, already converted from agorot)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tase_prices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,        -- YYYY-MM-DD
            ticker        TEXT NOT NULL,        -- e.g. 'IBI.TA'
            hebrew_name   TEXT,                 -- e.g. 'איביאי בית השק'
            price_ils     REAL NOT NULL,        -- agorot / 100
            UNIQUE(date, ticker)
        )
    """)

    # TASE holdings — share count ranges per ticker
    c.execute("""
        CREATE TABLE IF NOT EXISTS tase_holdings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT NOT NULL,
            hebrew_name   TEXT,
            from_date     TEXT NOT NULL,        -- YYYY-MM-DD
            to_date       TEXT NOT NULL DEFAULT '9999-12-31',
            qty           REAL NOT NULL,
            UNIQUE(ticker, from_date)
        )
    """)
    # daily_portfolio table 
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_portfolio (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT NOT NULL,
        broker      TEXT NOT NULL,
        name        TEXT NOT NULL,
        type        TEXT NOT NULL,       -- 'position' | 'Buy' | 'Sell' | 'Dividend'
        cashflow    REAL NOT NULL DEFAULT 0,
        open_value  REAL NOT NULL DEFAULT 0,  -- portfolio value BEFORE cashflow (prev-day close)
        close_value REAL NOT NULL DEFAULT 0,  -- portfolio value AFTER cashflow (this-day close)
        UNIQUE(date, broker, name, type)
    )
""")

def migrate_db():
    """Add open_value / close_value columns if upgrading from old schema."""
    conn = get_db()
    c = conn.cursor()
    cols = [r[1] for r in c.execute("PRAGMA table_info(daily_portfolio)").fetchall()]
    if "open_value" not in cols:
        c.execute("ALTER TABLE daily_portfolio ADD COLUMN open_value REAL NOT NULL DEFAULT 0")
    if "close_value" not in cols:
        c.execute("ALTER TABLE daily_portfolio ADD COLUMN close_value REAL NOT NULL DEFAULT 0")
    if "end_value" in cols:
        c.execute("UPDATE daily_portfolio SET close_value = end_value")
    conn.commit()   # ← commit first
    conn.close()    # ← then close


if __name__ == "__main__":
    init_db()
