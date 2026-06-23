"""
database.py - Database initialization and connection management.
"""

import os
import sqlite3
from contextlib import closing

DB_PATH = os.path.join(os.path.dirname(__file__), "twr.db")


def get_db() -> sqlite3.Connection:
    """
    Create and return a configured database connection.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Initialize the database schemas for portfolios, transactions, and prices.
    """
    with closing(get_db()) as conn:
        with conn:  # Automatically manages transactions (commit/rollback)
            c = conn.cursor()

            c.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_value_ils REAL NOT NULL,
                    fx_rate REAL NOT NULL DEFAULT 1,
                    UNIQUE(date, symbol)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    tx_type TEXT NOT NULL,
                    security TEXT NOT NULL,
                    amount_ils REAL NOT NULL,
                    UNIQUE(date, broker, tx_type, security, amount_ils)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS tase_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    hebrew_name TEXT,
                    price_ils REAL NOT NULL,
                    UNIQUE(date, ticker)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS tase_holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    hebrew_name TEXT,
                    from_date TEXT NOT NULL,
                    to_date TEXT NOT NULL DEFAULT '9999-12-31',
                    qty REAL NOT NULL,
                    UNIQUE(ticker, from_date)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_portfolio (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    cashflow REAL NOT NULL DEFAULT 0,
                    open_value REAL NOT NULL DEFAULT 0,
                    close_value REAL NOT NULL DEFAULT 0,
                    UNIQUE(date, broker, name, type)
                )
            """)


def migrate_db() -> None:
    """
    Migrate the database to the latest schema version.
    """
    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(daily_portfolio)").fetchall()]
            
            if "open_value" not in cols:
                c.execute("ALTER TABLE daily_portfolio ADD COLUMN open_value REAL NOT NULL DEFAULT 0")
            if "close_value" not in cols:
                c.execute("ALTER TABLE daily_portfolio ADD COLUMN close_value REAL NOT NULL DEFAULT 0")
            if "end_value" in cols:
                c.execute("UPDATE daily_portfolio SET close_value = end_value")


if __name__ == "__main__":
    init_db()
    migrate_db()