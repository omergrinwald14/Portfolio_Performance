"""
twr.py — Portfolio value and TWR calculation engine.

Pure functions only. No HTTP, no CSV parsing.
All values returned in ILS.
"""

from database import get_db


# ---------------------------------------------------------------------------
# Portfolio Value
# ---------------------------------------------------------------------------

def portfolio_value(date: str) -> dict:
    conn = get_db()
    c = conn.cursor()

    row = c.execute(
        "SELECT COALESCE(SUM(position_value_ils), 0) FROM positions WHERE date = ?",
        (date,)
    ).fetchone()
    ibkr_value = float(row[0])

    holdings = c.execute(
        """SELECT ticker, hebrew_name, qty
           FROM tase_holdings
           WHERE from_date <= ? AND to_date > ?""",
        (date, date)
    ).fetchall()

    ibi_value = 0.0
    ibi_detail = []

    for h in holdings:
        ticker, hebrew_name, qty = h["ticker"], h["hebrew_name"], h["qty"]
        price_row = c.execute(
            "SELECT price_ils FROM tase_prices WHERE ticker = ? AND date = ?",
            (ticker, date)
        ).fetchone()
        if price_row is None:
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

    conn.close()

    return {
        "ibkr":       round(ibkr_value, 2),
        "ibi":        round(ibi_value, 2),
        "total":      round(ibkr_value + ibi_value, 2),
        "ibi_detail": ibi_detail,
    }


# ---------------------------------------------------------------------------
# TWR Calculation
# ---------------------------------------------------------------------------

def calculate_twr(start_date: str, end_date: str) -> dict:
    conn = get_db()
    c = conn.cursor()

    rows = c.execute("""
        SELECT date,
               SUM(cashflow)    AS net_cf,
               MIN(open_value)  AS open_value,
               MAX(close_value) AS close_value
        FROM daily_portfolio
        WHERE date >= ? AND date <= ?
        GROUP BY date
        ORDER BY date
    """, (start_date, end_date)).fetchall()
    conn.close()

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
# MWR Calculation
# ---------------------------------------------------------------------------

def calculate_mwr(start_date: str, end_date: str) -> float:
    """
    IRR-based MWR over the period.
    Cashflows: Buy = negative (money leaves pocket), Sell/Dividend = positive.
    Terminal value (final portfolio close) = positive cashflow at end_date.
    Returns annualized rate.
    """
    from scipy.optimize import brentq

    conn = get_db()
    c = conn.cursor()

    tx_rows = c.execute("""
        SELECT date, SUM(cashflow) AS net_cf
        FROM daily_portfolio
        WHERE date >= ? AND date <= ?
        GROUP BY date
        ORDER BY date
    """, (start_date, end_date)).fetchall()

    final = c.execute("""
        SELECT MAX(close_value) FROM daily_portfolio
        WHERE date = (SELECT MAX(date) FROM daily_portfolio WHERE date <= ?)
    """, (end_date,)).fetchone()[0] or 0.0
    conn.close()

    if not tx_rows:
        return 0.0

    t0 = tx_rows[0]["date"]  # anchor date

    cashflows = []
    for row in tx_rows:
        days = (
            __import__("datetime").date.fromisoformat(row["date"]) -
            __import__("datetime").date.fromisoformat(t0)
        ).days
        cashflows.append((days, float(row["net_cf"])))

    # Terminal value at end_date
    T = (__import__("datetime").date.fromisoformat(end_date) -
         __import__("datetime").date.fromisoformat(t0)).days
    cashflows.append((T, final))

    def npv(r_daily):
        return sum(cf / (1 + r_daily) ** t for t, cf in cashflows)

    try:
        r_daily = brentq(npv, -0.9999, 10.0, maxiter=1000)
        r_annual = (1 + r_daily) ** 365 - 1
        return round(r_annual, 6)
    except ValueError:
        return 0.0

# ---------------------------------------------------------------------------
# Build daily_portfolio
# ---------------------------------------------------------------------------

def build_daily_portfolio():
    """
    One row per (date, broker, security, type).
    open_value  = close_value of the previous transaction date.
    close_value = total portfolio value at this day's close.
    cashflow    = signed: Buy > 0, Sell/Dividend < 0.
    """

    def ibkr_value_on(date):
        conn = get_db()
        row = conn.execute("""
            SELECT COALESCE(SUM(position_value_ils), 0)
            FROM positions
            WHERE date = (SELECT MAX(date) FROM positions WHERE date <= ?)
        """, (date,)).fetchone()
        conn.close()
        return float(row[0])

    def ibi_value_on(date):
        conn = get_db()
        holdings = conn.execute("""
            SELECT ticker, qty FROM tase_holdings
            WHERE from_date <= ? AND to_date > ?
        """, (date, date)).fetchall()
        total = 0.0
        for h in holdings:
            price = conn.execute("""
                SELECT price_ils FROM tase_prices
                WHERE ticker = ? AND date = (
                    SELECT MAX(date) FROM tase_prices WHERE ticker = ? AND date <= ?
                )
            """, (h["ticker"], h["ticker"], date)).fetchone()
            if price:
                total += h["qty"] * float(price["price_ils"])
        conn.close()
        return total

    def prev_close(date):
        conn = get_db()
        row = conn.execute("""
            SELECT MAX(close_value) FROM daily_portfolio
            WHERE date = (SELECT MAX(date) FROM daily_portfolio WHERE date < ?)
        """, (date,)).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] else 0.0

    # Load all transactions
    conn = get_db()
    tx_rows = conn.execute("""
        SELECT date, broker, tx_type, security,
               SUM(amount_ils) AS net_cf
        FROM transactions
        GROUP BY date, broker, tx_type, security
        ORDER BY date
    """).fetchall()
    conn.close()

    # Write connection
    conn = get_db()
    conn.execute("DELETE FROM daily_portfolio")
    conn.commit()

    for row in tx_rows:
        date    = row["date"]
        broker  = row["broker"]
        name    = row["security"]
        tx_type = row["tx_type"]
        raw_cf  = float(row["net_cf"])

        cashflow  = abs(raw_cf) if tx_type == "Buy" else -abs(raw_cf)
        open_val  = prev_close(date)
        close_val = ibkr_value_on(date) + ibi_value_on(date)

        conn.execute("""
            INSERT INTO daily_portfolio
            (date, broker, name, type, cashflow, open_value, close_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (date, broker, name, tx_type,
            round(cashflow, 4),
            round(open_val, 2),
            round(close_val, 2)))
        conn.commit()  # commit each row so prev_close can read it immediately

    conn.close()
