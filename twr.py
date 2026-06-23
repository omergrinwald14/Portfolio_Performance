"""
twr.py — Portfolio value and Time-Weighted Return (TWR) calculation engine.

Pure functions only. No HTTP, no CSV parsing.
All values returned in ILS.
"""

import sqlite3
from contextlib import closing
from typing import Any, Dict

from database import get_db


# ---------------------------------------------------------------------------
# Portfolio Value
# ---------------------------------------------------------------------------

def portfolio_value(date: str) -> Dict[str, Any]:
    """
    Calculate the total portfolio value (IBKR + IBI) for a specific date.
    
    Args:
        date (str): The date in YYYY-MM-DD format.
        
    Returns:
        dict: A dictionary containing IBKR value, IBI value, total value, 
              and a detailed breakdown of IBI holdings.
    """
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(position_value_ils), 0) FROM positions WHERE date = ?",
            (date,)
        ).fetchone()
        ibkr_value = float(row[0]) if row else 0.0

        holdings = conn.execute(
            """SELECT ticker, hebrew_name, qty
               FROM tase_holdings
               WHERE from_date <= ? AND to_date > ?""",
            (date, date)
        ).fetchall()

        ibi_value = 0.0
        ibi_detail = []

        for h in holdings:
            ticker, hebrew_name, qty = h["ticker"], h["hebrew_name"], h["qty"]
            price_row = conn.execute(
                "SELECT price_ils FROM tase_prices WHERE ticker = ? AND date = ?",
                (ticker, date)
            ).fetchone()
            
            if not price_row:
                continue
                
            price = float(price_row["price_ils"])
            value = qty * price
            ibi_value += value
            ibi_detail.append({
                "ticker": ticker,
                "hebrew_name": hebrew_name,
                "qty": qty,
                "price": price,
                "value": round(value, 2),
            })

    return {
        "ibkr":       round(ibkr_value, 2),
        "ibi":        round(ibi_value, 2),
        "total":      round(ibkr_value + ibi_value, 2),
        "ibi_detail": ibi_detail,
    }


# ---------------------------------------------------------------------------
# TWR Calculation
# ---------------------------------------------------------------------------

def calculate_twr(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    Calculate the cumulative Time-Weighted Return (TWR) for a date range.
    
    Args:
        start_date (str): Start date in YYYY-MM-DD format.
        end_date (str): End date in YYYY-MM-DD format.
        
    Returns:
        dict: Total TWR multiplier and a daily time series of returns.
    """
    with closing(get_db()) as conn:
        rows = conn.execute("""
            SELECT date,
                   SUM(cashflow)    AS net_cf,
                   MIN(open_value)  AS open_value,
                   MAX(close_value) AS close_value
            FROM daily_portfolio
            WHERE date >= ? AND date <= ?
            GROUP BY date
            ORDER BY date
        """, (start_date, end_date)).fetchall()

    if not rows:
        return {"twr": 0.0, "series": []}

    series = []
    twr_multiplier = 1.0

    for row in rows:
        open_val  = float(row["open_value"])
        close_val = float(row["close_value"])
        cf        = float(row["net_cf"])

        if open_val == 0:
            r = (close_val / abs(cf) - 1.0) if cf != 0 else 0.0
        else:
            r = (close_val - cf) / open_val - 1.0

        twr_multiplier *= (1.0 + r)

        series.append({
            "date":           row["date"],
            "open_value":     round(open_val, 2),
            "close_value":    round(close_val, 2),
            "net_cashflow":   round(cf, 2),
            "daily_return":   round(r, 6),
            "cumulative_twr": round(twr_multiplier - 1.0, 6),
        })

    return {"twr": round(twr_multiplier - 1.0, 4), "series": series}


# ---------------------------------------------------------------------------
# Build daily_portfolio
# ---------------------------------------------------------------------------

def build_daily_portfolio() -> None:
    """
    Rebuilds the daily_portfolio table from transactions and position histories.
    One row per (date, broker, security, type).
    open_value  = close_value of the previous transaction date.
    close_value = total portfolio value at this day's close.
    cashflow    = signed: Buy > 0, Sell/Dividend < 0.
    """

    def ibkr_value_on(db_conn: sqlite3.Connection, target_date: str) -> float:
        row = db_conn.execute("""
            SELECT COALESCE(SUM(position_value_ils), 0)
            FROM positions
            WHERE date = (SELECT MAX(date) FROM positions WHERE date <= ?)
        """, (target_date,)).fetchone()
        return float(row[0]) if row else 0.0

    def ibi_value_on(db_conn: sqlite3.Connection, target_date: str) -> float:
        holdings = db_conn.execute("""
            SELECT ticker, qty FROM tase_holdings
            WHERE from_date <= ? AND to_date > ?
        """, (target_date, target_date)).fetchall()
        
        total = 0.0
        for h in holdings:
            price_row = db_conn.execute("""
                SELECT price_ils FROM tase_prices
                WHERE ticker = ? AND date = (
                    SELECT MAX(date) FROM tase_prices WHERE ticker = ? AND date <= ?
                )
            """, (h["ticker"], h["ticker"], target_date)).fetchone()
            if price_row:
                total += h["qty"] * float(price_row["price_ils"])
        return total

    def prev_close(db_conn: sqlite3.Connection, target_date: str) -> float:
        row = db_conn.execute("""
            SELECT MAX(close_value) FROM daily_portfolio
            WHERE date = (SELECT MAX(date) FROM daily_portfolio WHERE date < ?)
        """, (target_date,)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    with closing(get_db()) as conn:
        # Load all aggregated transactions
        tx_rows = conn.execute("""
            SELECT date, broker, tx_type, security,
                   SUM(amount_ils) AS net_cf
            FROM transactions
            GROUP BY date, broker, tx_type, security
            ORDER BY date
        """).fetchall()

        # Clear existing daily portfolio data
        with conn:
            conn.execute("DELETE FROM daily_portfolio")

        # Process each transaction day sequentially
        for row in tx_rows:
            date    = row["date"]
            broker  = row["broker"]
            name    = row["security"]
            tx_type = row["tx_type"]
            raw_cf  = float(row["net_cf"])

            cashflow  = abs(raw_cf) if tx_type == "Buy" else -abs(raw_cf)
            open_val  = prev_close(conn, date)
            close_val = ibkr_value_on(conn, date) + ibi_value_on(conn, date)

            with conn:
                conn.execute("""
                    INSERT INTO daily_portfolio
                    (date, broker, name, type, cashflow, open_value, close_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, broker, name, tx_type,
                    round(cashflow, 4),
                    round(open_val, 2),
                    round(close_val, 2)
                ))