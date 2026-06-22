"""
parsers.py — parse uploaded CSVs and insert into the database.

Supports:
  - IBKR positions CSV      (position_value.csv style)
  - IBKR transactions CSV   (activity statement style)
  - IBI transactions CSV    (Hebrew headers)
  - TASE EOD price CSV      (securityHistoryEOD.csv style)
"""

import csv
import io
import re
from datetime import datetime, date, timedelta
from database import get_db

# ---------------------------------------------------------------------------
# Known TASE ticker <-> Hebrew name map
# ---------------------------------------------------------------------------
TASE_ALIASES = {
    "IBI.TA":   "איביאי בית השק",
    "NICE.TA":  "נייס",
    "TLSYS.TA": "טלסיס",
    "ELWS.TA":  "אלקטריאון",
}
TASE_ALIASES_REV = {v: k for k, v in TASE_ALIASES.items()}

# Funds to exclude from IBI cashflows (traded via קניה רצף but are index funds)
EXCLUDED_IBI_FUNDS = {
    "אינ.חוץS&P500EW",
    "אשס.חוץMSCIEURO",
    "הרל.SP500 EW",
    "אשס.חוץ SPX 500"
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_date(value, fmt=None) -> str | None:
    """Return YYYY-MM-DD string or None."""
    if not value:
        return None
    s = str(value).strip().strip('"')
    if fmt:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            return None
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # YYYYMMDD
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    # DD/MM/YYYY or DD.MM.YYYY
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # Excel serial number
    try:
        n = float(s)
        if 30000 < n < 60000:
            d = date(1899, 12, 30) + timedelta(days=int(n))
            return d.isoformat()
    except ValueError:
        pass
    return None


def to_float(value) -> float:
    """Parse a number string, stripping commas and currency symbols."""
    try:
        return float(str(value).replace(",", "").replace("₪", "").strip())
    except (ValueError, TypeError):
        return 0.0


def read_csv_rows(text: str) -> list[dict]:
    """Parse a plain CSV text into a list of dicts, skipping repeated header rows."""
    lines = text.replace("\r", "").split("\n")
    lines = [l for l in lines if l.strip()]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    headers = reader.fieldnames or []
    rows = []
    for row in reader:
        # Skip rows that look like repeated header rows
        if headers and row.get(headers[0], "") == headers[0]:
            continue
        rows.append(dict(row))
    return rows


def split_csv_line(line: str) -> list[str]:
    """Split a CSV line respecting quoted fields."""
    result = []
    cur, in_q = "", False
    for c in line:
        if c == '"':
            in_q = not in_q
        elif c == "," and not in_q:
            result.append(cur.strip().strip('"'))
            cur = ""
        else:
            cur += c
    result.append(cur.strip().strip('"'))
    return result


# ---------------------------------------------------------------------------
# IBKR Positions
# ---------------------------------------------------------------------------
def parse_ibkr_positions(text: str) -> dict:
    """
    Parse IBKR position_value.csv.
    Columns: CurrencyPrimary, FXRateToBase, ISIN, ReportDate, Quantity,
             MarkPrice, Symbol, PositionValue, CostBasisMoney
    Excludes: Symbol = RW (money market fund)
    Returns: {"inserted": int, "skipped": int}
    """
    rows = read_csv_rows(text)
    conn = get_db()
    c = conn.cursor()
    inserted = skipped = 0

    for row in rows:
        symbol = row.get("Symbol", "").strip()
        if not symbol or symbol in ("Symbol", "RW"):
            continue

        dt = parse_date(row.get("ReportDate", ""))
        if not dt:
            continue

        pos_val = to_float(row.get("PositionValue", 0))
        fx = to_float(row.get("FXRateToBase", 1)) or 1.0
        value_ils = pos_val * fx

        try:
            c.execute(
                """INSERT OR REPLACE INTO positions
                   (date, symbol, position_value_ils, fx_rate)
                   VALUES (?, ?, ?, ?)""",
                (dt, symbol, value_ils, fx),
            )
            inserted += 1
        except Exception:
            skipped += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# IBKR Transactions
# ---------------------------------------------------------------------------
IBKR_CASHFLOW_RE = re.compile(r"^(buy|sell|dividend|payment in lieu)", re.I)
IBKR_TAX_RE      = re.compile(r"^foreign tax withholding", re.I)
IBKR_SKIP_RE     = re.compile(r"^(credit interest|adjustment|other fee|deposit|withdrawal)", re.I)


def parse_ibkr_transactions(text: str) -> dict:
    """
    Parse IBKR activity statement CSV (Transaction History section).
    Columns (p[0]..p[12]):
      0=section  1=Data/Header  2=Date  3=Description  4=Type
      5=Symbol   6=Qty  7=Price  8=PriceCcy
      9=GrossAmount  10=Commission  11=NetAmount  12=ExchangeRate

    Net Amount is already in ILS — no fx conversion needed.
    Dividends are netted with their Foreign Tax Withholding rows (same date+symbol).
    RW is excluded.
    """
    lines = text.replace("\r", "").split("\n")

    # First pass: collect all relevant rows
    raw = []
    for line in lines:
        p = split_csv_line(line)
        if len(p) < 12:
            continue
        if p[0] != "Transaction History" or p[1] != "Data":
            continue
        tx_type = p[4].strip()
        symbol  = p[5].strip()
        if symbol == "RW":
            continue
        if IBKR_SKIP_RE.match(tx_type):
            continue

        dt  = parse_date(p[2])
        net = to_float(p[11])   # Net Amount — already in ILS

        if IBKR_TAX_RE.match(tx_type):
            raw.append({"date": dt, "type": "tax", "symbol": symbol, "net": net})
        elif IBKR_CASHFLOW_RE.match(tx_type):
            raw.append({"date": dt, "type": tx_type, "symbol": symbol, "net": net})

    # Build tax map: (date, symbol) -> total tax (negative)
    tax_map: dict[tuple, float] = {}
    for r in raw:
        if r["type"] == "tax":
            key = (r["date"], r["symbol"])
            tax_map[key] = tax_map.get(key, 0.0) + r["net"]

    # Second pass: combine all dividend/PIL rows for same date+symbol into ONE entry
    # to avoid double-counting tax when multiple dividend types exist (e.g. ASO: Dividend + PIL)
    conn = get_db()
    c = conn.cursor()
    inserted = skipped = 0

    # Aggregate dividend/PIL gross amounts per (date, symbol)
    div_gross: dict[tuple, float] = {}
    buy_sell = []
    for r in raw:
        if r["type"] == "tax":
            continue
        if re.match(r"dividend|payment in lieu", r["type"], re.I):
            key = (r["date"], r["symbol"])
            div_gross[key] = div_gross.get(key, 0.0) + r["net"]
        else:
            buy_sell.append(r)

    # Insert combined dividend rows (gross + total tax = net of withholding)
    for (dt, symbol), gross in div_gross.items():
        if not dt:
            continue
        tax = tax_map.get((dt, symbol), 0.0)
        amount_ils = gross + tax
        try:
            c.execute(
                """INSERT OR IGNORE INTO transactions
                   (date, broker, tx_type, security, amount_ils)
                   VALUES (?, 'IBKR', 'Dividend', ?, ?)""",
                (dt, symbol, round(amount_ils, 4)),
            )
            inserted += (1 if c.rowcount else 0)
            skipped  += (0 if c.rowcount else 1)
        except Exception:
            skipped += 1

    # Insert buy/sell rows (net is already final ILS)
    for r in buy_sell:
        if not r["date"]:
            continue
        tx_label = "Buy" if re.match(r"buy", r["type"], re.I) else "Sell"
        try:
            c.execute(
                """INSERT OR IGNORE INTO transactions
                   (date, broker, tx_type, security, amount_ils)
                   VALUES (?, 'IBKR', ?, ?, ?)""",
                (r["date"], tx_label, r["symbol"], round(r["net"], 4)),
            )
            inserted += (1 if c.rowcount else 0)
            skipped  += (0 if c.rowcount else 1)
        except Exception:
            skipped += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# IBI Transactions (Hebrew CSV)
# ---------------------------------------------------------------------------
# Column layout (fixed indices — header has literal " in מט"ח which breaks parsing):
#   0=תמורה במטח  1=מיספור  2=תאריך  3=שם נייר  4=סוג פעולה
#   5=מס נייר     6=כמות    7=שער ביצוע  8=מטבע   9=עמלת פעולה
#   10=עמלות נלוות  11=יתרה שקלית  12=תמורה בשקלים

def parse_ibi_transactions(text: str) -> dict:
    """
    Parse IBI Hebrew CSV.
    Included types:
      דיבדנד           — dividend (col12 = net ILS amount, already net of tax)
      קניה רצף         — TASE continuous buy
      מכירה רצף        — TASE continuous sell
    Excluded:
      קניה שח / מכירה שח  — tax bookkeeping or fund trades
      Known fund names in EXCLUDED_IBI_FUNDS
    Amount: col12 (תמורה בשקלים). For 2024-era files where col12 is empty,
            compute qty × price / 100 (agorot → ILS).
    """
    text = text.lstrip("\ufeff")   # strip BOM
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    # Skip header row (index 0)
    conn = get_db()
    c = conn.cursor()
    inserted = skipped = 0

    for line in lines[1:]:
        vals = [v.replace("\t", "").strip() for v in split_csv_line(line)]
        if len(vals) < 5:
            continue

        tx_type = vals[4]
        sec     = vals[3]

        is_dividend = "דיבדנד" in tx_type
        is_ratzuf   = "קניה רצף" in tx_type or "מכירה רצף" in tx_type

        if not is_dividend and not is_ratzuf:
            continue
        if sec in EXCLUDED_IBI_FUNDS:
            continue

        dt = parse_date(vals[2])
        if not dt:
            continue

        # Amount: prefer col12, fall back to qty * price / 100
        amt_raw = vals[12] if len(vals) > 12 else ""
        amount_ils = to_float(amt_raw) if amt_raw and amt_raw not in ("-", "") else 0.0
        if amount_ils == 0.0 and is_ratzuf:
            qty   = to_float(vals[6]) if len(vals) > 6 else 0.0
            price = to_float(vals[7]) if len(vals) > 7 else 0.0
            amount_ils = qty * price / 100   # agorot → ILS

        if is_dividend:
            tx_label = "Dividend"
        elif "קניה" in tx_type:
            tx_label = "Buy"
        else:
            tx_label = "Sell"

        try:
            c.execute(
                """INSERT OR IGNORE INTO transactions
                   (date, broker, tx_type, security, amount_ils)
                   VALUES (?, 'IBI', ?, ?, ?)""",
                (dt, tx_label, sec, round(amount_ils, 4)),
            )
            if c.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# TASE EOD Prices
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TASE Holdings (manual CSV: ticker, from_date, to_date, qty)
# ---------------------------------------------------------------------------
def parse_tase_holdings(text: str) -> dict:
    """
    Parse a user-maintained holdings CSV.
    Columns: ticker, from_date, to_date (optional), qty
    to_date defaults to '9999-12-31' (still holding).
    """
    rows = read_csv_rows(text)
    conn = get_db()
    c = conn.cursor()
    inserted = skipped = 0

    for row in rows:
        ticker    = row.get("ticker", "").strip()
        from_date = parse_date(row.get("from_date", ""), fmt="%m/%d/%Y")
        to_date   = parse_date(row.get("to_date", ""), fmt="%m/%d/%Y") or "9999-12-31"
        qty_raw   = row.get("qty", "").strip()

        if not ticker or not from_date or not qty_raw:
            skipped += 1
            continue

        qty = to_float(qty_raw)
        hebrew_name = TASE_ALIASES.get(ticker, "")

        try:
            c.execute(
                """INSERT OR REPLACE INTO tase_holdings
                   (ticker, hebrew_name, from_date, to_date, qty)
                   VALUES (?, ?, ?, ?, ?)""",
                (ticker, hebrew_name, from_date, to_date, qty),
            )
            if c.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[holdings] INSERT error: {e}")
            skipped += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# TASE EOD Prices
# ---------------------------------------------------------------------------
def parse_tase_eod(text: str) -> dict:
    """
    Parse TASE securityHistoryEOD.csv.
    Row 0: title e.g. "נתונים היסטוריים - סוף יום איביאי בית השק"
    Row 1-2: metadata (skip)
    Row 3+: data — col0=date(DD/MM/YYYY), col3=price(agorot)
    Converts agorot → ILS (/100).
    """
    text = text.lstrip("\ufeff")
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return {"inserted": 0, "skipped": 0}

    title_match = re.search(r"סוף יום (.+?)(?:,|$)", lines[0])
    hebrew_name = title_match.group(1).strip() if title_match else ""
    ticker = TASE_ALIASES_REV.get(hebrew_name, hebrew_name)

    conn = get_db()
    c = conn.cursor()
    inserted = skipped = 0

    for line in lines[3:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        date_raw = parts[0].strip()
        try:
            agorot = float(parts[3])
        except ValueError:
            continue

        dt = parse_date(date_raw)
        if not dt:
            continue
        price_ils = agorot / 100.0

        try:
            c.execute(
                """INSERT OR IGNORE INTO tase_prices
                   (date, ticker, hebrew_name, price_ils)
                   VALUES (?, ?, ?, ?)""",
                (dt, ticker, hebrew_name, price_ils),
            )
            if c.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[tase_eod] INSERT error: {e}")
            skipped += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped, "ticker": ticker, "name": hebrew_name}

