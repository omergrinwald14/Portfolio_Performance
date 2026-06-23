"""
parsers.py — Parse uploaded CSVs and insert them into the database.

Supports:
  - IBKR positions CSV (position_value.csv style)
  - IBKR transactions CSV (activity statement style)
  - IBI transactions CSV (Hebrew headers)
  - TASE EOD price CSV (securityHistoryEOD.csv style)
"""

import csv
import io
import re
import sqlite3
from datetime import datetime, date, timedelta
from contextlib import closing
from typing import Dict, List, Optional, Any, Tuple

from database import get_db

# ---------------------------------------------------------------------------
# Constants & Mappings
# ---------------------------------------------------------------------------
TASE_ALIASES: Dict[str, str] = {
    "IBI.TA":   "איביאי בית השק",
    "NICE.TA":  "נייס",
    "TLSYS.TA": "טלסיס",
    "ELWS.TA":  "אלקטריאון",
}
TASE_ALIASES_REV: Dict[str, str] = {v: k for k, v in TASE_ALIASES.items()}

EXCLUDED_IBI_FUNDS = {
    "אינ.חוץS&P500EW",
    "אשס.חוץMSCIEURO",
    "הרל.SP500 EW",
    "אשס.חוץ SPX 500"
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(value: Any, fmt: Optional[str] = None) -> Optional[str]:
    """Parse various date formats into a standard YYYY-MM-DD string."""
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


def to_float(value: Any) -> float:
    """Parse a number string, stripping commas and currency symbols."""
    try:
        return float(str(value).replace(",", "").replace("₪", "").strip())
    except (ValueError, TypeError):
        return 0.0


def read_csv_rows(text: str) -> List[Dict[str, str]]:
    """Parse a plain CSV text into a list of dicts, skipping repeated header rows."""
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return []
        
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    headers = reader.fieldnames or []
    rows = []
    
    for row in reader:
        if headers and row.get(headers[0], "") == headers[0]:
            continue
        rows.append(dict(row))
        
    return rows


def split_csv_line(line: str) -> List[str]:
    """Split a CSV line respecting quoted fields."""
    result = []
    cur = ""
    in_q = False
    
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
# IBKR Parsers
# ---------------------------------------------------------------------------

def parse_ibkr_positions(text: str) -> Dict[str, int]:
    """Parse IBKR position_value.csv."""
    rows = read_csv_rows(text)
    inserted = skipped = 0

    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()
            for row in rows:
                symbol = row.get("Symbol", "").strip()
                if not symbol or symbol in ("Symbol", "RW"):
                    continue

                dt = parse_date(row.get("ReportDate", ""))
                if not dt:
                    skipped += 1
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
                except sqlite3.Error:
                    skipped += 1

    return {"inserted": inserted, "skipped": skipped}


IBKR_CASHFLOW_RE = re.compile(r"^(buy|sell|dividend|payment in lieu)", re.I)
IBKR_TAX_RE      = re.compile(r"^foreign tax withholding", re.I)
IBKR_SKIP_RE     = re.compile(r"^(credit interest|adjustment|other fee|deposit|withdrawal)", re.I)

def parse_ibkr_transactions(text: str) -> Dict[str, int]:
    """Parse IBKR activity statement CSV (Transaction History section)."""
    lines = text.replace("\r", "").split("\n")
    raw: List[Dict[str, Any]] = []

    for line in lines:
        p = split_csv_line(line)
        if len(p) < 12 or p[0] != "Transaction History" or p[1] != "Data":
            continue
            
        tx_type = p[4].strip()
        symbol  = p[5].strip()
        
        if symbol == "RW" or IBKR_SKIP_RE.match(tx_type):
            continue

        dt  = parse_date(p[2])
        net = to_float(p[11])

        if IBKR_TAX_RE.match(tx_type):
            raw.append({"date": dt, "type": "tax", "symbol": symbol, "net": net})
        elif IBKR_CASHFLOW_RE.match(tx_type):
            raw.append({"date": dt, "type": tx_type, "symbol": symbol, "net": net})

    tax_map: Dict[Tuple[Optional[str], str], float] = {}
    div_gross: Dict[Tuple[Optional[str], str], float] = {}
    buy_sell: List[Dict[str, Any]] = []

    for r in raw:
        if r["type"] == "tax":
            key = (r["date"], r["symbol"])
            tax_map[key] = tax_map.get(key, 0.0) + r["net"]
        elif re.match(r"dividend|payment in lieu", r["type"], re.I):
            key = (r["date"], r["symbol"])
            div_gross[key] = div_gross.get(key, 0.0) + r["net"]
        else:
            buy_sell.append(r)

    inserted = skipped = 0

    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()

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
                    inserted += 1 if c.rowcount else 0
                    skipped  += 0 if c.rowcount else 1
                except sqlite3.Error:
                    skipped += 1

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
                    inserted += 1 if c.rowcount else 0
                    skipped  += 0 if c.rowcount else 1
                except sqlite3.Error:
                    skipped += 1

    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# IBI Transactions (Hebrew CSV)
# ---------------------------------------------------------------------------

def parse_ibi_transactions(text: str) -> Dict[str, int]:
    """Parse IBI Hebrew CSV for continuous buys/sells and dividends."""
    text = text.lstrip("\ufeff")
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    inserted = skipped = 0

    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()
            for line in lines[1:]:
                vals = [v.replace("\t", "").strip() for v in split_csv_line(line)]
                if len(vals) < 5:
                    continue

                tx_type = vals[4]
                sec     = vals[3]

                is_dividend = "דיבדנד" in tx_type
                is_ratzuf   = "קניה רצף" in tx_type or "מכירה רצף" in tx_type

                if (not is_dividend and not is_ratzuf) or sec in EXCLUDED_IBI_FUNDS:
                    continue

                dt = parse_date(vals[2])
                if not dt:
                    skipped += 1
                    continue

                amt_raw = vals[12] if len(vals) > 12 else ""
                amount_ils = to_float(amt_raw) if amt_raw and amt_raw not in ("-", "") else 0.0
                
                if amount_ils == 0.0 and is_ratzuf:
                    qty   = to_float(vals[6]) if len(vals) > 6 else 0.0
                    price = to_float(vals[7]) if len(vals) > 7 else 0.0
                    amount_ils = qty * price / 100

                tx_label = "Dividend" if is_dividend else ("Buy" if "קניה" in tx_type else "Sell")

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
                except sqlite3.Error:
                    skipped += 1

    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# TASE Parsers
# ---------------------------------------------------------------------------

def parse_tase_holdings(text: str) -> Dict[str, int]:
    """Parse a user-maintained holdings CSV."""
    rows = read_csv_rows(text)
    inserted = skipped = 0

    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()
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
                except sqlite3.Error:
                    skipped += 1

    return {"inserted": inserted, "skipped": skipped}


def parse_tase_eod(text: str) -> Dict[str, Any]:
    """Parse TASE securityHistoryEOD.csv."""
    text = text.lstrip("\ufeff")
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return {"inserted": 0, "skipped": 0}

    title_match = re.search(r"סוף יום (.+?)(?:,|$)", lines[0])
    hebrew_name = title_match.group(1).strip() if title_match else ""
    ticker = TASE_ALIASES_REV.get(hebrew_name, hebrew_name)
    inserted = skipped = 0

    with closing(get_db()) as conn:
        with conn:
            c = conn.cursor()
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
                except sqlite3.Error:
                    skipped += 1
    
    return {"inserted": inserted, "skipped": skipped, "ticker": ticker, "name": hebrew_name}