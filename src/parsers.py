"""
Transaction file parsers.

Supported formats:
  - ANZ CSV (legacy ANZ, no header, 3-8 columns)
  - ANZ Plus PDF statements (monthly statements from ANZ Plus app)
  - ANZ Access Advantage PDF (legacy ANZ branch account monthly statements)
  - Latitude / 28 Degrees HTML (Chakra UI React page saved from browser)
  - PayPal CSV (standard activity export)
"""

import csv
import fnmatch
import hashlib
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ── ANZ (legacy) CSV ──────────────────────────────────────────────────────────

_ANZ_COLS = ["date", "amount", "description", "payee_name", "reference",
             "reference2", "_blank", "note"]


def _parse_anz_date(s: str) -> pd.Timestamp:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return pd.Timestamp(datetime.strptime(s.strip(), fmt))
        except ValueError:
            pass
    return pd.NaT


def parse_anz_csv(filepath: str | Path, account_name: str, account_type: str = "transaction") -> pd.DataFrame:
    rows = []
    with open(filepath, encoding="utf-8", errors="ignore", newline="") as f:
        for parts in csv.reader(f):
            if not parts:
                continue
            while len(parts) < 8:
                parts.append("")
            rows.append(parts[:8])

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=_ANZ_COLS)
    df["date"] = df["date"].apply(_parse_anz_date)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["date", "amount"])
    df["account"] = account_name
    df["account_type"] = account_type
    df["source_file"] = Path(filepath).name
    df["is_pending"] = False
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── Statement period extraction (shared across all PDF parsers) ───────────────

_STMT_PERIOD_SLASH_RE = re.compile(r'(\d{2}/\d{2}/\d{4})-(\d{2}/\d{2}/\d{4})')
_STMT_PERIOD_WORDS_RE = re.compile(
    r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})(?:\s*\[[^\]]*\])?\s*(?:[-–]|TO)\s*'
    r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
    re.IGNORECASE,
)


def _extract_statement_period(text: str) -> tuple[datetime | None, datetime | None]:
    """Extract statement start/end dates from first-page PDF text.

    Handles all supported formats:
    - 28 Degrees:  Statementperiod:DD/MM/YYYY-DD/MM/YYYY (spaces stripped)
    - ANZ Plus:    1 January 2025 - 31 January 2025
    - ANZ Advantage: 29 JULY 2022 TO 30 SEPTEMBER 2022
    - Wise:        4 December 2025 [GMT+10:00] - 3 May 2026 [GMT+10:00]
    """
    m = _STMT_PERIOD_SLASH_RE.search(text.replace(" ", ""))
    if m:
        try:
            return (datetime.strptime(m.group(1), "%d/%m/%Y"),
                    datetime.strptime(m.group(2), "%d/%m/%Y"))
        except ValueError:
            pass
    m = _STMT_PERIOD_WORDS_RE.search(text)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return (datetime.strptime(m.group(1).title(), fmt),
                        datetime.strptime(m.group(2).title(), fmt))
            except ValueError:
                continue
    return None, None


def _fmt_period(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f"{start.strftime('%d %b %Y')}-{end.strftime('%d %b %Y')}"
    return ""


_CSV_PERIOD_RE = re.compile(r'[_\-]([A-Za-z]{3,9})(\d{4})(?:[_\-.]|$)', re.IGNORECASE)
_CSV_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def _extract_period_from_csv_filename(name: str) -> tuple[datetime | None, datetime | None]:
    """Extract month-year from filenames like 'Paypal_Sept2019.CSV' → first/last day of month."""
    m = _CSV_PERIOD_RE.search(Path(name).stem)
    if not m:
        return None, None
    month = _CSV_MONTH_MAP.get(m.group(1).lower()[:3])
    if not month:
        return None, None
    try:
        year = int(m.group(2))
        start = datetime(year, month, 1)
        end = datetime(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)
        return start, end
    except (ValueError, OverflowError):
        return None, None


# ── ANZ Plus PDF statements ───────────────────────────────────────────────────
#
# Format observed across all statements:
#   - Text-only (no table objects)
#   - Transactions listed newest → oldest
#   - Each transaction: "DD Mon DESCRIPTION $AMOUNT $BALANCE"
#   - Multi-line continuations: location string or "Effective Date DD/MM/YYYY"
#   - Sign determined by: balance_change = current_balance − prev_balance
#   - Year inferred from statement period header
#   - Empty statements contain "There are no transactions to display."

_TXN_RE = re.compile(
    r"^(\d{1,2} [A-Z][a-z]{2})\s+(.+)\s+\$([0-9,]+\.\d{2})\s+\$([0-9,]+\.\d{2})\s*$"
)
_OPENING_RE = re.compile(r"^Opening Balance \$([0-9,]+\.\d{2})$")
_EFFECTIVE_RE = re.compile(r"^Effective Date (\d{2}/\d{2}/\d{4})$")
_PERIOD_RE = re.compile(r"^(\d{1,2} \w+ \d{4}) - (\d{1,2} \w+ \d{4})$")
_BSB_RE = re.compile(r"^\d{3} \d{3} \d{3} \d{3} \d{3}")
_INTEREST_RE = re.compile(r"^\+ \$[0-9,]+\.\d{2}")

_SKIP_EXACT = {
    "Account Statement", "ANZ Plus Everyday", "ANZ Plus Growth Saver",
    "Date Description Credit Debit Balance", "Transactions", "Account Name",
    "Please check your statement carefully.",
    "Interest Earned Account Name",
}
_SKIP_PREFIXES = (
    "Australia and New Zealand Banking Group",
    "AFSL", "www.afca", "Telephone:", "If you notice", "If an issue",
    "For information", "a complaint with", "PO BOX", "BRIAR HILL VIC", "AUS",
    "Branch Number",
)


def _extract_period(lines: list[str]) -> tuple[datetime | None, datetime | None]:
    for line in lines[:20]:
        m = _PERIOD_RE.match(line)
        if m:
            try:
                return (
                    datetime.strptime(m.group(1), "%d %B %Y"),
                    datetime.strptime(m.group(2), "%d %B %Y"),
                )
            except ValueError:
                pass
    return None, None


def _plus_date(date_str: str, period_start: datetime | None, period_end: datetime | None) -> pd.Timestamp:
    """Parse 'DD Mon' or 'DD/MM/YYYY', inferring year from statement period."""
    if "/" in date_str:
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                return pd.Timestamp(datetime.strptime(date_str, fmt))
            except ValueError:
                pass
        return pd.NaT

    year = period_end.year if period_end else datetime.now().year
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return pd.Timestamp(datetime.strptime(f"{date_str} {year}", fmt))
        except ValueError:
            pass

    # Spanning Dec-Jan: try alternate year
    if period_start and period_start.year != year:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                candidate = datetime.strptime(f"{date_str} {period_start.year}", fmt)
                if period_start <= candidate <= period_end:
                    return pd.Timestamp(candidate)
            except ValueError:
                pass
    return pd.NaT


def _parse_anz_plus_lines(lines: list[str], period_start: datetime | None, period_end: datetime | None) -> list[dict]:
    raw: list[dict] = []   # newest first, as they appear
    current = None
    opening_balance = None

    for line in lines:
        if not line:
            continue
        if line in _SKIP_EXACT:
            continue
        if any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if _PERIOD_RE.match(line) or _BSB_RE.match(line) or _INTEREST_RE.match(line):
            continue
        # Account holder name / address — skip lines before Transactions header
        # (We only get here after the first "Date Description..." header, handled below)

        # Opening Balance → end of transactions for this page block
        m = _OPENING_RE.match(line)
        if m:
            opening_balance = float(m.group(1).replace(",", ""))
            if current:
                raw.append(current)
                current = None
            continue

        # New transaction line
        m = _TXN_RE.match(line)
        if m:
            if current:
                raw.append(current)
            current = {
                "date_str": m.group(1),
                "desc": m.group(2).strip(),
                "amount_abs": float(m.group(3).replace(",", "")),
                "balance": float(m.group(4).replace(",", "")),
                "effective_date_str": None,
            }
            continue

        # Effective Date continuation
        m = _EFFECTIVE_RE.match(line)
        if m and current:
            current["effective_date_str"] = m.group(1)
            continue

        # Location / continuation — append to current description
        if current:
            current["desc"] = current["desc"] + " " + line

    if current:
        raw.append(current)

    # Determine credit/debit from running balance (raw is newest-first)
    transactions = []
    for i, txn in enumerate(raw):
        prev_balance = (
            raw[i + 1]["balance"] if i < len(raw) - 1
            else (opening_balance if opening_balance is not None else 0.0)
        )
        balance_change = txn["balance"] - prev_balance
        amount = txn["amount_abs"] if balance_change >= 0 else -txn["amount_abs"]

        txn_date = _plus_date(
            txn["effective_date_str"] or txn["date_str"],
            period_start, period_end,
        )
        if txn_date is pd.NaT:
            continue

        transactions.append({
            "date": txn_date,
            "amount": round(amount, 2),
            "description": txn["desc"].strip(),
            "payee_name": "",
            "reference": "",
            "note": "",
        })

    return transactions


def parse_anz_plus_pdf(filepath: str | Path, account_name: str, account_type: str = "transaction") -> pd.DataFrame:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("  pdfplumber not installed — run: pip install pdfplumber")
        return pd.DataFrame()

    all_lines: list[str] = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(l.strip() for l in text.split("\n"))

    period_start, period_end = _extract_period(all_lines)
    period_str = _fmt_period(period_start, period_end)
    if period_str:
        logger.info(f"{period_str}")

    # Empty statement
    if any("There are no transactions to display" in l for l in all_lines):
        return pd.DataFrame()

    transactions = _parse_anz_plus_lines(all_lines, period_start, period_end)

    if not transactions:
        logger.warning(f"  Warning: no transactions parsed from {Path(filepath).name} — check PDF format")
        return pd.DataFrame()

    df = pd.DataFrame(transactions)
    df["account"] = account_name
    df["account_type"] = account_type
    df["source_file"] = Path(filepath).name
    df["is_pending"] = False
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── ANZ Access Advantage PDF ──────────────────────────────────────────────────
#
# Legacy ANZ branch account monthly statements (PDF).
# Columns: Date | Transaction Details | Withdrawals ($) | Deposits ($) | Balance ($)
# Empty cells are rendered as the literal word "blank" by pdfplumber.
# Year appears on its own line: "YYYY blank blank"
# Merchant names and reference numbers appear on continuation lines after each row.

_ANZ_ADVANTAGE_TXN_RE = re.compile(
    r"^(\d{1,2} [A-Z]{3})\s+(.+?)\s+"
    r"([\d,]+\.\d{2}|blank)\s+"   # withdrawal or blank
    r"([\d,]+\.\d{2}|blank)\s+"   # deposit or blank
    r"([\d,]+\.\d{2})$"            # balance
)
_ANZ_ADVANTAGE_YEAR_RE = re.compile(r"^(\d{4})\s+blank\s+blank$")
_ANZ_ADVANTAGE_SKIP = {
    "anz access advantage", "statement number", "account number",
    "transaction details", "withdrawals", "deposits", "balance",
    "please retain", "date transaction", "opening balance",
    "closing balance", "welcome to your", "account details",
    "need to get in touch", "anz internet banking", "anz.com",
    "lost/stolen", "australia and new zealand", "rtbsp", "xprcap",
}


def parse_anz_access_advantage_pdf(
    filepath: "str | Path", account_name: str, account_type: str = "transaction"
) -> "pd.DataFrame":
    try:
        import pdfplumber
    except ImportError:
        logger.warning("  pdfplumber not installed — run: pip install pdfplumber")
        return pd.DataFrame()

    all_lines: list[str] = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(l.strip() for l in text.split("\n"))

    # Extract statement period from header e.g. "29 JULY 2022 TO 30 SEPTEMBER 2022"
    current_year = datetime.now().year
    _adv_period_str = ""
    for line in all_lines:
        m = re.search(
            r"(\d{1,2}\s+\w+\s+\d{4})\s+TO\s+(\d{1,2}\s+\w+\s+\d{4})", line, re.IGNORECASE
        )
        if m:
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    ps = datetime.strptime(m.group(1).title(), fmt)
                    pe = datetime.strptime(m.group(2).title(), fmt)
                    current_year = ps.year
                    _adv_period_str = _fmt_period(ps, pe)
                    break
                except ValueError:
                    pass
            if current_year != datetime.now().year:
                break
    if _adv_period_str:
        logger.info(f"{_adv_period_str}")

    transactions: list[dict] = []
    current_txn: "dict | None" = None

    for line in all_lines:
        if not line:
            continue

        # Year-change marker
        ym = _ANZ_ADVANTAGE_YEAR_RE.match(line)
        if ym:
            if current_txn:
                transactions.append(current_txn)
                current_txn = None
            current_year = int(ym.group(1))
            continue

        # Transaction row
        m = _ANZ_ADVANTAGE_TXN_RE.match(line)
        if m:
            date_str, desc, withdrawal, deposit, _ = m.groups()
            if any(k in desc.lower() for k in ("opening balance", "closing balance")):
                if current_txn:
                    transactions.append(current_txn)
                    current_txn = None
                continue
            try:
                txn_date = pd.Timestamp(datetime.strptime(f"{date_str} {current_year}", "%d %b %Y"))
            except ValueError:
                continue
            if withdrawal != "blank":
                amount = -float(withdrawal.replace(",", ""))
            elif deposit != "blank":
                amount = float(deposit.replace(",", ""))
            else:
                continue
            if current_txn:
                transactions.append(current_txn)
            current_txn = {
                "date": txn_date, "amount": amount, "description": desc,
                "payee_name": "", "reference": "", "note": "",
                "account": account_name, "account_type": account_type,
                "source_file": Path(filepath).name, "is_pending": False,
            }
            continue

        # Continuation line — append merchant/reference detail to previous transaction
        if current_txn:
            line_lo = line.lower()
            if line_lo.startswith("effective date"):
                continue
            if any(k in line_lo for k in _ANZ_ADVANTAGE_SKIP):
                continue
            current_txn["description"] = current_txn["description"] + " " + line

    if current_txn:
        transactions.append(current_txn)

    if not transactions:
        logger.warning(f"  Warning: no transactions parsed from {Path(filepath).name} — check PDF format")
        return pd.DataFrame()

    df = pd.DataFrame(transactions)
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── ANZ Cash Investment / ETrade PDF statements ───────────────────────────────
#
# Format: "ANZ CASH INVESTMENT ACCT STATEMENT" — text-only, no embedded tables.
# Two PDF generations exist:
#   Older (pre-~2023): empty columns rendered as "blank" by pdfplumber.
#   Newer (post-~2023): empty columns simply omitted, giving only 2 amounts per row.
# Year marker lines start with a 4-digit year; newer PDFs add a type label
# (e.g. "2026 TRANSFER blank") which the original Access Advantage regex rejects.

_ANZ_ETRADE_YEAR_RE   = re.compile(r"^((?:19|20)\d{2})\b")
_ANZ_ETRADE_TXN3_RE   = re.compile(          # DD MON desc blank|amt blank|amt balance
    r"^(\d{1,2} [A-Z]{3})\s+(.+?)\s+"
    r"([\d,]+\.\d{2}|blank)\s+"
    r"([\d,]+\.\d{2}|blank)\s+"
    r"([\d,]+\.\d{2})$"
)
_ANZ_ETRADE_TXN2_RE   = re.compile(          # DD MON desc amt balance (no blank marker)
    r"^(\d{1,2} [A-Z]{3})\s+(.+?)\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})$"
)
_ANZ_ETRADE_OPENBAL_RE = re.compile(r"^(\d{1,2} [A-Z]{3}) OPENING BALANCE\s+([\d,]+\.\d{2})$")
_ANZ_ETRADE_FINALIZE = {
    "totals at end", "totals at", "important information",
    "this statement includes", "yearly summary",
}
_ANZ_ETRADE_SKIP = {
    "anz cash investment", "statement number", "account number",
    "transaction details", "please retain", "date transaction",
    "opening balance", "closing balance", "welcome to your",
    "account details", "need to get in touch", "anz internet banking",
    "anz.com", "lost/stolen", "australia and new zealand",
    "rtbsp", "xprcap", "withdrawals", "deposits", "balance",
    "please check", "all entries", "if you have a complaint",
    "further information", "page ", "www.anz", "https://",
    "keep your", "send you", "you can also", "interest earned",
    "interest on deposits", "call ", "write ", "visit ",
    "online:", "email:", "web:", "afca",
}


def parse_anz_etrade_pdf(
    filepath: "str | Path",
    account_name: str = "ANZ ETrade",
    account_type: str = "investment",
) -> "pd.DataFrame":
    """Parse ANZ Cash Investment Account (ETrade) PDF statements.

    Handles both older PDFs (empty cells rendered as "blank") and newer PDFs
    (empty cells omitted). Infers deposit/withdrawal sign from balance delta.
    """
    try:
        import pdfplumber as _pp
    except ImportError:
        return pd.DataFrame()

    all_lines: list[str] = []
    with _pp.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(l.strip() for l in text.split("\n"))

    # Extract and print statement period
    current_year = datetime.now().year
    for line in all_lines:
        m = re.search(
            r"(\d{1,2}\s+\w+\s+\d{4})\s+TO\s+(\d{1,2}\s+\w+\s+\d{4})", line, re.IGNORECASE
        )
        if m:
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    ps = datetime.strptime(m.group(1).title(), fmt)
                    _pe = datetime.strptime(m.group(2).title(), fmt)
                    current_year = ps.year
                    logger.info(f"{_fmt_period(ps, _pe)}")
                    break
                except ValueError:
                    pass
            break

    transactions: list[dict] = []
    current_txn: "dict | None" = None
    prev_balance: "float | None" = None

    for line in all_lines:
        if not line:
            continue

        line_lo = line.lower()

        # Finalize keywords: commit the current transaction and stop appending
        if any(k in line_lo for k in _ANZ_ETRADE_FINALIZE):
            if current_txn:
                transactions.append(current_txn)
                current_txn = None
            continue

        # Opening balance — extract balance for sign inference, skip as a transaction
        ob = _ANZ_ETRADE_OPENBAL_RE.match(line)
        if ob:
            prev_balance = float(ob.group(2).replace(",", ""))
            if current_txn:
                transactions.append(current_txn)
                current_txn = None
            continue

        # Year marker — line starts with 4-digit year but is not a transaction row
        ym = _ANZ_ETRADE_YEAR_RE.match(line)
        if ym and not re.match(r"^\d{1,2} [A-Z]{3}\s", line):
            if current_txn:
                transactions.append(current_txn)
                current_txn = None
            current_year = int(ym.group(1))
            continue

        # 3-amount transaction row (older PDF: withdrawal/deposit/balance, blanks explicit)
        m3 = _ANZ_ETRADE_TXN3_RE.match(line)
        if m3:
            date_str, desc, f1, f2, f3 = m3.groups()
            if any(k in desc.lower() for k in ("opening balance", "closing balance")):
                if current_txn:
                    transactions.append(current_txn)
                    current_txn = None
                continue
            try:
                txn_date = pd.Timestamp(datetime.strptime(f"{date_str} {current_year}", "%d %b %Y"))
            except ValueError:
                continue
            if f1 != "blank":
                amount = -float(f1.replace(",", ""))
            elif f2 != "blank":
                amount = float(f2.replace(",", ""))
            else:
                continue
            balance = float(f3.replace(",", ""))
            prev_balance = balance
            if current_txn:
                transactions.append(current_txn)
            current_txn = {
                "date": txn_date, "amount": amount, "description": desc,
                "payee_name": "", "reference": "", "note": "",
                "account": account_name, "account_type": account_type,
                "source_file": Path(filepath).name, "is_pending": False,
            }
            continue

        # 2-amount transaction row (newer PDF: no blank marker for empty column)
        m2 = _ANZ_ETRADE_TXN2_RE.match(line)
        if m2:
            date_str, desc, single_str, balance_str = m2.groups()
            if any(k in desc.lower() for k in ("opening balance", "closing balance")):
                if current_txn:
                    transactions.append(current_txn)
                    current_txn = None
                continue
            try:
                txn_date = pd.Timestamp(datetime.strptime(f"{date_str} {current_year}", "%d %b %Y"))
            except ValueError:
                continue
            single = float(single_str.replace(",", ""))
            balance = float(balance_str.replace(",", ""))
            # Infer sign from balance delta
            if prev_balance is not None:
                amount = single if (balance - prev_balance) > 0 else -single
            else:
                amount = single  # assume deposit when opening balance unknown
            prev_balance = balance
            if current_txn:
                transactions.append(current_txn)
            current_txn = {
                "date": txn_date, "amount": amount, "description": desc,
                "payee_name": "", "reference": "", "note": "",
                "account": account_name, "account_type": account_type,
                "source_file": Path(filepath).name, "is_pending": False,
            }
            continue

        # Continuation line — append to current transaction description
        if current_txn:
            if any(k in line_lo for k in _ANZ_ETRADE_SKIP):
                continue
            current_txn["description"] = current_txn["description"] + " " + line

    if current_txn:
        transactions.append(current_txn)

    if not transactions:
        logger.warning(f"  Warning: no transactions parsed from {Path(filepath).name} — check PDF format")
        return pd.DataFrame()

    df = pd.DataFrame(transactions)
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── Latitude / 28 Degrees HTML ────────────────────────────────────────────────

def parse_latitude_html(filepath: str | Path) -> pd.DataFrame:
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        content = f.read()

    soup = BeautifulSoup(content, "lxml")
    transactions = []

    for group in soup.find_all(attrs={"data-testid": "transaction"}):
        txn_date = None
        for p in group.find_all("p"):
            text = p.get_text(strip=True)
            m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text)
            if m:
                try:
                    txn_date = pd.Timestamp(datetime.strptime(m.group(1), "%d %B %Y"))
                    break
                except ValueError:
                    pass
        if txn_date is None:
            continue

        for row in group.find_all(attrs={"role": "row"}):
            title_elem = row.find(attrs={"data-testid": "transaction-title"})
            if not title_elem:
                continue
            merchant = title_elem.get_text(strip=True)

            location = ""
            parent_div = title_elem.parent
            if parent_div:
                sibling_ps = parent_div.find_all("p")
                if len(sibling_ps) >= 2:
                    location = sibling_ps[1].get_text(strip=True)

            amount = None
            for cell in row.find_all(attrs={"role": "cell"}):
                for p in cell.find_all("p"):
                    m = re.search(r"([+\-])\s*\$([0-9,]+\.?\d*)", p.get_text(" ", strip=True))
                    if m:
                        sign = -1 if m.group(1) == "-" else 1
                        amount = sign * float(m.group(2).replace(",", ""))
                        break
                if amount is not None:
                    break

            if amount is None:
                continue

            is_pending = bool(row.find(attrs={"data-testid": "transaction-pending"}))
            transactions.append({
                "date": txn_date, "amount": amount,
                "description": merchant, "payee_name": "",
                "reference": location, "note": "",
                "account": "28 Degrees Credit Card", "account_type": "credit_card",
                "source_file": Path(filepath).name, "is_pending": is_pending,
            })

    return pd.DataFrame(transactions)


# ── PayPal CSV ────────────────────────────────────────────────────────────────
#
# Actual PayPal export columns (after BOM strip):
#   Date, Time, Time zone, Name, Type, Status, Currency, Amount, Fees, Total,
#   Exchange Rate, Receipt ID, Balance, Transaction ID, Item Title
#
# Internal "Transfer to PayPal account" rows are mirror entries (PayPal's
# internal ledger) and must be filtered out before analysis.

_PAYPAL_INTERNAL_TYPES = {
    "transfer to paypal account",
    "general currency conversion",
    "currency conversion",
    "void of authorisation",
    "reversal of ach deposit",
    "reversal of ach withdrawal transaction",
}


def parse_paypal_csv(filepath) -> pd.DataFrame:
    try:
        raw = pd.read_csv(filepath, encoding="utf-8-sig")   # utf-8-sig strips BOM
    except UnicodeDecodeError:
        raw = pd.read_csv(filepath, encoding="latin-1")

    raw.columns = [c.strip().lower().replace(" ", "_") for c in raw.columns]

    # Detect columns flexibly
    date_col = next((c for c in raw.columns if c == "date"), None)
    name_col = next((c for c in raw.columns if c in ("name", "description", "item_title")), None)
    amount_col = next((c for c in raw.columns if c in ("amount", "gross")), None)
    status_col = next((c for c in raw.columns if c == "status"), None)
    type_col = next((c for c in raw.columns if c == "type"), None)
    txn_id_col = next((c for c in raw.columns if "transaction_id" in c or c == "txn_id"), None)

    if not (date_col and amount_col):
        logger.warning(f"  Warning: unrecognised PayPal CSV columns: {list(raw.columns)}")
        return pd.DataFrame()

    time_col = next((c for c in raw.columns if c == "time"), None)
    currency_col = next((c for c in raw.columns if c == "currency"), None)

    # For FX purchases (non-AUD), swap the foreign-currency amount for the AUD equivalent
    # that PayPal records on the paired "Transfer to PayPal account" row (same timestamp).
    # This makes the enricher's amount-match work against the actual ANZ bank debit.
    _FX_PURCHASE_TYPES = {
        "pre-approved payment bill user payment",
        "express checkout payment",
        "website payment",
        "general payment",
        "subscription payment",
        "order",
    }
    if type_col and currency_col and time_col and status_col:
        raw["_ts"] = raw[date_col].astype(str) + "|" + raw[time_col].astype(str)
        type_lower = raw[type_col].str.strip().str.lower()
        status_lower = raw[status_col].str.strip().str.lower()
        fx_mask = (
            (raw[currency_col].str.strip().str.upper() != "AUD")
            & (status_lower == "completed")
            & type_lower.isin(_FX_PURCHASE_TYPES)
        )
        for idx in raw[fx_mask].index:
            funding = raw[
                (raw["_ts"] == raw.at[idx, "_ts"])
                & (type_lower == "transfer to paypal account")
            ]
            if not funding.empty:
                aud_str = (
                    str(funding.iloc[0][amount_col])
                    .replace(",", "").replace("$", "").strip()
                )
                try:
                    raw.at[idx, amount_col] = -abs(float(aud_str))
                except ValueError:
                    pass
        raw = raw.drop(columns=["_ts"])

    # Filter out internal PayPal ledger entries
    if type_col:
        internal_mask = raw[type_col].str.strip().str.lower().isin(_PAYPAL_INTERNAL_TYPES)
        raw = raw[~internal_mask].reset_index(drop=True)

    amounts = pd.to_numeric(
        raw[amount_col].astype(str).str.replace(",", "").str.replace("$", "").str.strip(),
        errors="coerce",
    )
    df = pd.DataFrame({
        "date": pd.to_datetime(raw[date_col], dayfirst=True, errors="coerce"),
        "amount": amounts,
        "description": raw[name_col].fillna("PayPal") if name_col else "PayPal",
        "payee_name": raw[name_col].fillna("") if name_col else "",
        "reference": raw[txn_id_col].fillna("") if txn_id_col else "",
        "note": raw[type_col].fillna("") if type_col else "",
        "account": "PayPal",
        "account_type": "paypal",
        "source_file": Path(filepath).name,
        "is_pending": (
            raw[status_col].str.strip().str.lower() != "completed"
            if status_col else False
        ),
    })

    return df.dropna(subset=["date", "amount"])


# ── 28 Degrees / Latitude PDF ────────────────────────────────────────────────
#
# Format (text-based PDF, no table objects):
#   - Transaction rows: "DD/MM/YYYY CARD# Merchant Location $Amount"
#   - Debit amounts right-align to x≈497 (purchases)
#   - Credit amounts right-align to x≈582 (payments, refunds)
#   - FX continuation lines follow: "X.XXCCYRate:R.RRRRRR"
#   - Interest / insurance rows included; OnlineAccountPayment excluded by config

_28DEG_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_28DEG_AMOUNT_RE = re.compile(r"^\$([0-9,]+\.\d{2})$")
_28DEG_RATE_RE = re.compile(r"^([0-9,.]+)([A-Z]{3})Rate:([0-9.]+)")
_28DEG_CREDIT_X_MIN = 520  # x0 >= this threshold → Credits column


def parse_28degrees_pdf(filepath: str | Path, account_name: str = "28 Degrees Credit Card",
                        account_type: str = "credit_card") -> pd.DataFrame:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("  pdfplumber not installed")
        return pd.DataFrame()

    transactions = []

    with pdfplumber.open(filepath) as pdf:
        if pdf.pages:
            _ps, _pe = _extract_statement_period(pdf.pages[0].extract_text() or "")
            _period_str = _fmt_period(_ps, _pe)
            if _period_str:
                logger.info(f"{_period_str}")

        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue

            # Group words into rows by y-position (2pt tolerance)
            rows: dict[int, list] = {}
            for w in words:
                key = round(w["top"] / 2) * 2
                rows.setdefault(key, []).append(w)

            current: dict | None = None

            for y in sorted(rows):
                row_words = sorted(rows[y], key=lambda w: w["x0"])
                texts = [w["text"] for w in row_words]
                if not texts:
                    continue

                # Transaction row: first word is DD/MM/YYYY
                if _28DEG_DATE_RE.match(texts[0]):
                    if current:
                        transactions.append(current)

                    # Last amount-shaped word in the row
                    amount_indices = [i for i, w in enumerate(row_words)
                                      if _28DEG_AMOUNT_RE.match(w["text"])]
                    if not amount_indices:
                        current = None
                        continue
                    amt_idx = amount_indices[-1]
                    amount_word = row_words[amt_idx]

                    amount_val = float(amount_word["text"].replace("$", "").replace(",", ""))
                    is_credit = amount_word["x0"] >= _28DEG_CREDIT_X_MIN
                    amount = amount_val if is_credit else -amount_val

                    # Description: words between card# (index 1) and amount word
                    desc = " ".join(w["text"] for w in row_words[2:amt_idx]).strip()

                    try:
                        txn_date = pd.Timestamp(datetime.strptime(texts[0], "%d/%m/%Y"))
                    except ValueError:
                        current = None
                        continue

                    current = {
                        "date": txn_date,
                        "amount": amount,
                        "description": desc,
                        "note": "",
                    }

                elif current:
                    # Continuation line — extract FX currency/rate if present
                    for w in row_words:
                        m = _28DEG_RATE_RE.match(w["text"])
                        if m:
                            orig_amt, orig_ccy, rate = m.group(1), m.group(2), m.group(3)
                            if orig_ccy != "AUD":
                                current["note"] = f"{orig_ccy} {orig_amt} @ {rate}"
                            break

            # Save last transaction of this page before moving to the next
            if current:
                transactions.append(current)

    if not transactions:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "date": t["date"],
        "amount": t["amount"],
        "description": t["description"],
        "payee_name": "",
        "reference": "",
        "note": t["note"],
        "account": account_name,
        "account_type": account_type,
        "source_file": Path(filepath).name,
        "is_pending": False,
    } for t in transactions])

    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── Revolut CSV ───────────────────────────────────────────────────────────────
#
# Columns: Type, Product, Started Date, Completed Date, Description,
#          Amount, Fee, Currency, State, Balance

def parse_revolut_csv(filepath: str | Path, account_name: str = "Revolut",
                      account_type: str = "transaction") -> pd.DataFrame:
    try:
        raw = pd.read_csv(filepath, encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = pd.read_csv(filepath, encoding="latin-1")

    # Keep only COMPLETED rows (drop PENDING, REVERTED, etc.)
    if "State" in raw.columns:
        raw = raw[raw["State"] == "COMPLETED"].reset_index(drop=True)

    if raw.empty:
        return pd.DataFrame()

    # Sort by date ascending so balance deltas are computable
    date_col = "Completed Date" if "Completed Date" in raw.columns else "Started Date"
    raw["_date"] = pd.to_datetime(raw[date_col], errors="coerce")
    raw = raw.dropna(subset=["_date"]).sort_values("_date").reset_index(drop=True)

    # Balance column is always in AUD (account base currency)
    raw["_balance"] = pd.to_numeric(
        raw["Balance"].astype(str).str.replace(",", ""), errors="coerce"
    )

    # AUD rows: use Amount directly.  FX rows: derive AUD from balance change.
    aud_amounts = pd.to_numeric(
        raw["Amount"].astype(str).str.replace(",", ""), errors="coerce"
    )
    balance_delta = raw["_balance"].diff()
    # diff() is NaN for the first row; treat the opening balance as 0 so the
    # first FX transaction isn't silently dropped.
    if len(raw) > 0:
        balance_delta.iloc[0] = raw["_balance"].iloc[0]

    is_aud = (raw["Currency"] == "AUD") if "Currency" in raw.columns else pd.Series(True, index=raw.index)
    amounts = aud_amounts.where(is_aud, balance_delta)

    # Build note: include original currency + amount for FX transactions
    notes = (raw["Type"].fillna("") if "Type" in raw.columns else pd.Series("", index=raw.index)).astype(str)
    if "Currency" in raw.columns:
        fx_mask = ~is_aud & raw["Currency"].notna()
        if fx_mask.any():
            fx_note = raw.loc[fx_mask, "Currency"] + " " + raw.loc[fx_mask, "Amount"].astype(str)
            notes = notes.copy()
            notes.loc[fx_mask] = (notes.loc[fx_mask].str.strip() + " [" + fx_note + "]").str.strip()

    df = pd.DataFrame({
        "date": raw["_date"],
        "amount": amounts,
        "description": raw["Description"].fillna("Revolut"),
        "payee_name": "",
        "reference": "",
        "note": notes,
        "account": account_name,
        "account_type": account_type,
        "source_file": Path(filepath).name,
        "is_pending": False,
    })

    return df.dropna(subset=["date", "amount"])


# ── Wise PDF ──────────────────────────────────────────────────────────────────
#
# Format: "Description [Incoming] [Outgoing] Balance" on one line,
#         date on the next line as "DD Month YYYY ..."
# AUD amounts shown even for foreign-currency transactions (FX done by Wise).

_WISE_TXN_PREFIXES = (
    "Card transaction",
    "Topped up account",
    "Interest or Stocks",
    "AUD Assets service fee",
)
_WISE_DATE_RE = re.compile(r"^(\d{1,2} \w+ \d{4})")
_WISE_AMOUNTS_RE = re.compile(r"(-?[0-9,]+\.\d{2})\s+(-?[0-9,]+\.\d{2})\s*$")
_WISE_CARD_RE = re.compile(r"Card transaction of [0-9,.]+ \w+ issued by (.+)")
_WISE_TOPUP_RE = re.compile(r"^Topped up account\s+([0-9,]+\.\d{2})")


def parse_wise_pdf(filepath: str | Path, account_name: str = "Wise",
                   account_type: str = "transaction") -> pd.DataFrame:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("  pdfplumber not installed")
        return pd.DataFrame()

    all_lines: list[str] = []
    with pdfplumber.open(filepath) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if i == 0:
                _ps, _pe = _extract_statement_period(text)
                _period_str = _fmt_period(_ps, _pe)
                if _period_str:
                    logger.info(f"{_period_str}")
            all_lines.extend(l.strip() for l in text.split("\n") if l.strip())

    transactions = []
    i = 0
    while i < len(all_lines):
        line = all_lines[i]

        if not any(line.startswith(p) for p in _WISE_TXN_PREFIXES):
            i += 1
            continue

        # Extract AUD amount (second-to-last number on the line)
        m = _WISE_AMOUNTS_RE.search(line)
        if not m:
            i += 1
            continue

        amount = float(m.group(1).replace(",", ""))

        # Top-ups are positive incoming amounts
        topup_m = _WISE_TOPUP_RE.match(line)
        if topup_m:
            amount = float(topup_m.group(1).replace(",", ""))

        # Clean description
        desc = line[:m.start()].strip()
        card_m = _WISE_CARD_RE.match(desc)
        if card_m:
            desc = card_m.group(1).strip()

        # Find date on the next few lines
        txn_date = pd.NaT
        for j in range(i + 1, min(i + 4, len(all_lines))):
            date_m = _WISE_DATE_RE.match(all_lines[j])
            if date_m:
                try:
                    txn_date = pd.Timestamp(
                        datetime.strptime(date_m.group(1), "%d %B %Y")
                    )
                    break
                except ValueError:
                    pass

        if txn_date is pd.NaT:
            i += 1
            continue

        transactions.append({
            "date": txn_date,
            "amount": amount,
            "description": desc,
            "payee_name": "",
            "reference": "",
            "note": "",
        })
        i += 1

    if not transactions:
        return pd.DataFrame()

    df = pd.DataFrame(transactions)
    df["account"] = account_name
    df["account_type"] = account_type
    df["source_file"] = Path(filepath).name
    df["is_pending"] = False
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── CommBank (CBA) CSV ────────────────────────────────────────────────────────
# NetBank export: Accounts → Transaction history → Export → CSV
# Headers: Date,Amount,Description,Balance   (Date = DD/MM/YYYY, Amount signed)

def parse_commbank_csv(
    filepath: str | Path,
    account_name: str = "CommBank",
    account_type: str = "transaction",
) -> pd.DataFrame:
    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig", dtype=str)
    except Exception as exc:
        logger.error(f"  {Path(filepath).name}: CommBank CSV read failed — {exc}")
        return pd.DataFrame()

    df.columns = [c.strip() for c in df.columns]
    missing = {"Date", "Amount", "Description"} - set(df.columns)
    if missing:
        logger.error(f"  {Path(filepath).name}: CommBank CSV missing columns {missing}")
        return pd.DataFrame()

    result = pd.DataFrame()
    result["date"] = pd.to_datetime(df["Date"].str.strip(), format="%d/%m/%Y", errors="coerce")
    result["amount"] = pd.to_numeric(df["Amount"].str.strip(), errors="coerce")
    result["description"] = df["Description"].str.strip().fillna("")
    result["payee_name"] = ""
    result["reference"] = ""
    result["note"] = ""
    result["account"] = account_name
    result["account_type"] = account_type
    result["source_file"] = Path(filepath).name
    result["is_pending"] = False
    result = result.dropna(subset=["date", "amount"])
    return result[["date", "amount", "description", "payee_name", "reference",
                   "note", "account", "account_type", "source_file", "is_pending"]]


# ── Westpac CSV ───────────────────────────────────────────────────────────────
# Online Banking export: Transaction history → Export → CSV
# Headers: Transaction Date,Description,Debit,Credit,Balance
#   Debit / Credit are separate positive columns; credits positive, debits negative.
# Legacy format also supported: BSB,Account Number,Transaction Date,Narration,...,Amount,...

def parse_westpac_csv(
    filepath: str | Path,
    account_name: str = "Westpac",
    account_type: str = "transaction",
) -> pd.DataFrame:
    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig", dtype=str)
    except Exception as exc:
        logger.error(f"  {Path(filepath).name}: Westpac CSV read failed — {exc}")
        return pd.DataFrame()

    df.columns = [c.strip() for c in df.columns]
    cols = set(df.columns)

    # Determine layout
    if "Transaction Date" in cols and "Debit" in cols and "Credit" in cols:
        # Standard current format
        dates = pd.to_datetime(df["Transaction Date"].str.strip(), dayfirst=True, errors="coerce")
        credit = pd.to_numeric(df["Credit"].str.strip(), errors="coerce").fillna(0)
        debit  = pd.to_numeric(df["Debit"].str.strip(),  errors="coerce").fillna(0)
        amounts = credit - debit
        descs = df["Description"].str.strip() if "Description" in cols else pd.Series([""] * len(df))
    elif "Transaction Date" in cols and "Amount" in cols:
        # Legacy BSB-included format
        dates   = pd.to_datetime(df["Transaction Date"].str.strip(), dayfirst=True, errors="coerce")
        amounts = pd.to_numeric(df["Amount"].str.strip(), errors="coerce")
        descs   = df.get("Narration", df.get("Description", pd.Series([""] * len(df)))).str.strip()
    else:
        logger.error(f"  {Path(filepath).name}: unrecognised Westpac CSV layout — cols: {list(df.columns)}")
        return pd.DataFrame()

    result = pd.DataFrame()
    result["date"]        = dates
    result["amount"]      = amounts
    result["description"] = descs.fillna("")
    result["payee_name"]  = ""
    result["reference"]   = ""
    result["note"]        = ""
    result["account"]     = account_name
    result["account_type"]= account_type
    result["source_file"] = Path(filepath).name
    result["is_pending"]  = False
    result = result.dropna(subset=["date", "amount"])
    return result[["date", "amount", "description", "payee_name", "reference",
                   "note", "account", "account_type", "source_file", "is_pending"]]


# ── NAB CSV ───────────────────────────────────────────────────────────────────
# Internet Banking export: Transaction history → Export → CSV
# Headers: Date,Amount,Description,Balance   (Date = DD-Mon-YYYY, Amount signed)

def parse_nab_csv(
    filepath: str | Path,
    account_name: str = "NAB",
    account_type: str = "transaction",
) -> pd.DataFrame:
    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig", dtype=str)
    except Exception as exc:
        logger.error(f"  {Path(filepath).name}: NAB CSV read failed — {exc}")
        return pd.DataFrame()

    df.columns = [c.strip() for c in df.columns]
    missing = {"Date", "Amount", "Description"} - set(df.columns)
    if missing:
        logger.error(f"  {Path(filepath).name}: NAB CSV missing columns {missing}")
        return pd.DataFrame()

    result = pd.DataFrame()
    # NAB uses DD-Mon-YYYY (e.g. 09-Jan-2024); fall back to generic dayfirst parsing
    result["date"] = pd.to_datetime(
        df["Date"].str.strip(), format="%d-%b-%Y", errors="coerce"
    )
    unresolved = result["date"].isna()
    if unresolved.any():
        result.loc[unresolved, "date"] = pd.to_datetime(
            df.loc[unresolved, "Date"].str.strip(), dayfirst=True, errors="coerce"
        )
    result["amount"]      = pd.to_numeric(df["Amount"].str.strip(), errors="coerce")
    result["description"] = df["Description"].str.strip().fillna("")
    result["payee_name"]  = ""
    result["reference"]   = ""
    result["note"]        = ""
    result["account"]     = account_name
    result["account_type"]= account_type
    result["source_file"] = Path(filepath).name
    result["is_pending"]  = False
    result = result.dropna(subset=["date", "amount"])
    return result[["date", "amount", "description", "payee_name", "reference",
                   "note", "account", "account_type", "source_file", "is_pending"]]


# ── OFX (Open Financial Exchange) ────────────────────────────────────────────
# Used by CommBank OFX export, Westpac OFX, and any OFX 1.x / 2.x file.
# Parses the SGML variant (OFX 1.x) without requiring an XML parser.

_OFX_TXN_RE   = re.compile(r'<STMTTRN>(.*?)(?:</STMTTRN>|<STMTTRN>)', re.DOTALL)
_OFX_FIELD_RE = re.compile(r'<(\w+)>\s*([^\n<]+)')


def _parse_ofx_date(raw: str) -> datetime | None:
    """Parse OFX DTPOSTED: YYYYMMDDHHMMSS[tz] → datetime (date part only)."""
    s = raw.strip()[:8]
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def parse_ofx(
    filepath: str | Path,
    account_name: str = "Bank",
    account_type: str = "transaction",
) -> pd.DataFrame:
    """Parse an OFX 1.x / 2.x file exported from any Australian bank."""
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.error(f"  {Path(filepath).name}: OFX read failed — {exc}")
        return pd.DataFrame()

    # For OFX 1.x SGML the closing tag is optional; split on <STMTTRN> instead
    # Grab everything between opening tags and the next opening or a closing tag.
    blocks = re.split(r'</?STMTTRN>', text, flags=re.IGNORECASE)

    rows = []
    for block in blocks:
        fields = {m.group(1).upper(): m.group(2).strip()
                  for m in _OFX_FIELD_RE.finditer(block)}
        if "TRNAMT" not in fields or "DTPOSTED" not in fields:
            continue
        dt = _parse_ofx_date(fields.get("DTPOSTED", ""))
        if dt is None:
            continue
        try:
            amount = float(fields["TRNAMT"])
        except ValueError:
            continue
        desc = fields.get("MEMO") or fields.get("NAME") or fields.get("FITID") or ""
        rows.append({
            "date":         dt,
            "amount":       amount,
            "description":  desc,
            "payee_name":   fields.get("NAME", ""),
            "reference":    fields.get("FITID", ""),
            "note":         "",
            "account":      account_name,
            "account_type": account_type,
            "source_file":  Path(filepath).name,
            "is_pending":   False,
        })

    if not rows:
        logger.warning(f"  {Path(filepath).name}: OFX parsed 0 transactions")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["date", "amount"])
    return df[["date", "amount", "description", "payee_name", "reference",
               "note", "account", "account_type", "source_file", "is_pending"]]


# ── File-type detection ───────────────────────────────────────────────────────

def _detect_account_from_text(
    text: str, name_hint: str, config: dict
) -> tuple[str, dict]:
    """Return (file_type, account_conf) by matching first-page text against known signatures.

    Shared by _sniff_pdf_type (live files) and backfill_statement_periods (archive scan).
    """
    compact = text.replace(" ", "").lower()
    name_lower = name_hint.lower()

    def _find_acct(type_val: str) -> dict:
        for acct in config.get("accounts", {}).values():
            if isinstance(acct, dict) and acct.get("type") == type_val:
                return acct
        return {}

    if "anzcashinvestmentacct" in compact or "anz cash investment acct" in text.lower():
        for acct in config.get("accounts", {}).values():
            if isinstance(acct, dict) and acct.get("type") in ("investment", "anz_etrade"):
                return "anz_etrade_pdf", acct
        return "anz_etrade_pdf", {"display_name": "ANZ ETrade", "type": "investment"}

    if "progresssaveraccount" in compact or "progress saver account" in text.lower():
        for acct in config.get("accounts", {}).values():
            if isinstance(acct, dict) and acct.get("type") in ("savings", "anz_progress_saver") \
                    and "progress" in acct.get("display_name", "").lower():
                return "anz_progress_saver_pdf", acct
        return "anz_progress_saver_pdf", {"display_name": "ANZ Progress Saver", "type": "savings"}

    if "28degreescard.com.au" in compact or "latitudefinancialservices" in compact:
        acct = _find_acct("credit_card")
        return "latitude_pdf", acct or {"display_name": "28 Degrees Credit Card", "type": "credit_card"}

    if "anzaccessadvantage" in compact:
        acct = _find_acct("anz_access_advantage")
        return "anz_access_advantage_pdf", acct or {"display_name": "ANZ Personal", "type": "transaction"}

    if "wiseaustralia" in compact:
        acct = _find_acct("wise")
        return "wise_pdf", acct or {"display_name": "Wise", "type": "transaction"}

    if "anzplus" in compact or "anz plus" in text.lower():
        if "everyday" in compact or "everyday" in name_lower:
            for acct in config.get("accounts", {}).values():
                if isinstance(acct, dict) and "everyday" in acct.get("display_name", "").lower():
                    return "anz_plus_pdf", acct
            return "anz_plus_pdf", {"display_name": "ANZ Plus Everyday", "type": "transaction"}
        if "growthsaver" in compact or "growth" in name_lower or "saver" in name_lower:
            for acct in config.get("accounts", {}).values():
                if isinstance(acct, dict) and "growth" in acct.get("display_name", "").lower():
                    return "anz_plus_pdf", acct
            return "anz_plus_pdf", {"display_name": "ANZ Plus Growth Saver", "type": "savings"}
        return "anz_plus_pdf", {"display_name": Path(name_hint).stem, "type": "transaction"}

    if "everyday" in name_lower:
        return "anz_plus_pdf", {"display_name": "ANZ Plus Everyday", "type": "transaction"}
    if "growth" in name_lower or "saver" in name_lower:
        return "anz_plus_pdf", {"display_name": "ANZ Plus Growth Saver", "type": "savings"}

    return "anz_plus_pdf", {"display_name": Path(name_hint).stem, "type": "transaction"}


def _sniff_pdf_type(filepath: Path, config: dict) -> tuple[str, dict]:
    """Identify a PDF's type and matching account config by reading first-page content."""
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        text = ""
    return _detect_account_from_text(text, filepath.name, config)


def backfill_statement_periods(config: dict) -> int:
    """Scan archive zips and populate statement_periods for any zip not yet recorded.

    Uses the scanned_archives table as a work-log so only new zips are opened.
    Safe to call on every page load — does nothing if archives haven't changed.
    Returns the number of new periods recorded.
    """
    from src.db import get_db, init_db as _init_db, save_statement_periods as _save

    try:
        import pdfplumber
        import zipfile
        import io as _io
    except ImportError:
        return 0

    archive_dir = Path(config.get("data", {}).get("archive_dir", "Data/Archive"))
    if not archive_dir.exists():
        return 0

    conn = get_db(config)
    _init_db(conn)
    try:
        already_scanned = {
            row[0] for row in conn.execute("SELECT zip_file FROM scanned_archives").fetchall()
        }
    finally:
        conn.close()

    new_zips = [
        p for p in sorted(archive_dir.glob("*.zip"))
        if p.name not in already_scanned
    ]
    if not new_zips:
        return 0

    periods: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for zf_path in new_zips:
        try:
            with zipfile.ZipFile(zf_path) as z:
                for name in z.namelist():
                    ext = Path(name).suffix.lower()
                    if ext == ".pdf":
                        try:
                            with z.open(name) as f:
                                pdf_bytes = f.read()
                            with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
                                text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
                            ps, pe = _extract_statement_period(text)
                            if not ps or not pe:
                                continue
                            _, acct_conf = _detect_account_from_text(text, name, config)
                            account = acct_conf.get("display_name", Path(name).stem)
                            key = (name, account)
                            if key in seen:
                                continue
                            seen.add(key)
                            periods.append({
                                "account": account,
                                "period_start": ps.strftime("%Y-%m-%d"),
                                "period_end": pe.strftime("%Y-%m-%d"),
                                "source_file": name,
                            })
                        except Exception:
                            continue
                    elif ext == ".csv":
                        try:
                            with z.open(name) as f:
                                csv_bytes = f.read()
                            file_type, acct_conf = _detect_file_type(Path(Path(name).name), config)
                            if file_type == "skip":
                                continue
                            account = acct_conf.get("display_name", Path(name).stem)
                            tmp_df = pd.read_csv(_io.BytesIO(csv_bytes), low_memory=False)
                            date_col = next(
                                (c for c in tmp_df.columns
                                 if c.strip().lower() in ("date", "transaction date",
                                                          "started date", "completed date")),
                                None,
                            )
                            if date_col is not None:
                                dates = pd.to_datetime(
                                    tmp_df[date_col], format="mixed", dayfirst=True, errors="coerce"
                                ).dropna()
                                ps = dates.min().to_pydatetime() if not dates.empty else None
                                pe = dates.max().to_pydatetime() if not dates.empty else None
                            else:
                                ps, pe = None, None
                            if not ps or not pe:
                                ps, pe = _extract_period_from_csv_filename(name)
                            if not ps or not pe:
                                continue
                            key = (name, account)
                            if key in seen:
                                continue
                            seen.add(key)
                            periods.append({
                                "account": account,
                                "period_start": ps.strftime("%Y-%m-%d"),
                                "period_end": pe.strftime("%Y-%m-%d"),
                                "source_file": Path(name).name,
                            })
                        except Exception:
                            continue
        except Exception:
            continue

        # Record this zip as scanned regardless of how many periods were found
        conn2 = get_db(config)
        try:
            conn2.execute(
                "INSERT OR IGNORE INTO scanned_archives (zip_file) VALUES (?)",
                (zf_path.name,),
            )
            conn2.commit()
        finally:
            conn2.close()

    if periods:
        return _save(periods, config)
    return 0


def _detect_file_type(filepath: Path, config: dict) -> tuple[str, dict]:
    """Return (file_type, account_conf).
    file_type: anz_csv | anz_plus_pdf | anz_access_advantage_pdf |
               latitude_html | latitude_pdf |
               paypal_csv | revolut_csv | wise_pdf |
               commbank_csv | westpac_csv | nab_csv | ofx |
               generic_csv | skip
    """
    name = filepath.name
    suffix = filepath.suffix.lower()

    # Strip Windows re-download suffix (" (1)", " (2)", …) before pattern matching
    # so "file (1).pdf" matches the same config patterns as "file.pdf".
    norm_name = re.sub(r'\s*\(\d+\)$', '', filepath.stem) + filepath.suffix

    # Match configured account patterns first
    for _key, acct in config.get("accounts", {}).items():
        pattern = acct.get("file_pattern", "")
        if pattern and fnmatch.fnmatch(norm_name, pattern):
            if suffix == ".html":
                return "latitude_html", acct
            if suffix in (".ofx", ".qfx"):
                return "ofx", acct
            if suffix == ".pdf":
                acct_type = acct.get("type", "")
                if acct_type == "wise":
                    return "wise_pdf", acct
                if acct_type == "credit_card":
                    return "latitude_pdf", acct
                if acct_type == "anz_access_advantage":
                    return "anz_access_advantage_pdf", acct
                if acct_type in ("investment", "anz_etrade"):
                    return "anz_etrade_pdf", acct
                if acct_type == "anz_progress_saver":
                    return "anz_progress_saver_pdf", acct
                return "anz_plus_pdf", acct
            if acct.get("type") == "paypal":
                return "paypal_csv", acct
            if acct.get("type") == "revolut":
                return "revolut_csv", acct
            if acct.get("type") == "commbank_csv":
                return "commbank_csv", acct
            if acct.get("type") == "westpac_csv":
                return "westpac_csv", acct
            if acct.get("type") == "nab_csv":
                return "nab_csv", acct
            if acct.get("type") == "ofx":
                return "ofx", acct
            return "anz_csv", acct

    # Check file_account_overrides.json before falling through to filename inference
    _db_path = config.get("data", {}).get("database", "")
    if _db_path:
        _overrides_path = Path(_db_path).parent / "file_account_overrides.json"
        if _overrides_path.exists():
            try:
                _overrides = json.loads(_overrides_path.read_text(encoding="utf-8"))
                _acct_key = _overrides.get(filepath.name)
                if _acct_key and _acct_key in config.get("accounts", {}):
                    _acct_conf = config["accounts"][_acct_key]
                    if suffix == ".html":
                        return "latitude_html", _acct_conf
                    if suffix == ".pdf":
                        _t = _acct_conf.get("type", "")
                        if _t == "wise":
                            return "wise_pdf", _acct_conf
                        if _t == "credit_card":
                            return "latitude_pdf", _acct_conf
                        if _t == "anz_access_advantage":
                            return "anz_access_advantage_pdf", _acct_conf
                        if _t in ("investment", "anz_etrade"):
                            return "anz_etrade_pdf", _acct_conf
                        if _t == "anz_progress_saver":
                            return "anz_progress_saver_pdf", _acct_conf
                        return "anz_plus_pdf", _acct_conf
            except Exception:
                pass

    # Fallback inference from filename / content
    name_lower = name.lower()
    if suffix == ".html":
        return "latitude_html", {"display_name": filepath.stem, "type": "credit_card"}
    if suffix in (".ofx", ".qfx"):
        return "ofx", {"display_name": filepath.stem, "type": "transaction"}
    if suffix == ".pdf":
        if "everyday" in name_lower:
            return "anz_plus_pdf", {"display_name": "ANZ Plus Everyday", "type": "transaction"}
        if "growth saver" in name_lower or "growth_saver" in name_lower:
            return "anz_plus_pdf", {"display_name": "ANZ Plus Growth Saver", "type": "savings"}
        if "saver" in name_lower:
            return "anz_plus_pdf", {"display_name": "ANZ Plus Growth Saver", "type": "savings"}
        if "anz" in name_lower and "statement" in name_lower:
            return "anz_access_advantage_pdf", {"display_name": "ANZ Personal", "type": "transaction"}
        return "anz_plus_pdf", {"display_name": filepath.stem, "type": "transaction"}
    if suffix == ".csv":
        if "paypal" in name_lower:
            return "paypal_csv", {"display_name": "PayPal", "type": "paypal"}
        if "personal" in name_lower:
            return "anz_csv", {"display_name": "ANZ Personal", "type": "transaction"}
        if "etrade" in name_lower or "e-trade" in name_lower:
            return "anz_csv", {"display_name": "ANZ ETrade", "type": "investment"}
        if "saver" in name_lower or "progress" in name_lower:
            return "anz_csv", {"display_name": "ANZ Progress Saver", "type": "savings"}
        try:
            with open(filepath, encoding="utf-8-sig", errors="ignore") as f:
                first = f.readline()
                second = f.readline()
            hdrs = [h.strip().strip('"') for h in first.strip().split(",")]

            # PayPal
            if "Amount" in hdrs and "Transaction ID" in hdrs:
                return "paypal_csv", {"display_name": "PayPal", "type": "paypal"}

            # Saved bank profiles take priority over built-in format detection
            from src.bank_profiles import find_profile as _find_bp
            _bp = _find_bp(hdrs, config)
            if _bp:
                return "generic_csv", _bp

            # Westpac — unique "Transaction Date" + separate Debit/Credit columns
            if "Transaction Date" in hdrs and "Debit" in hdrs and "Credit" in hdrs:
                return "westpac_csv", {"display_name": "Westpac", "type": "transaction"}

            # CommBank vs NAB — both use Date,Amount,Description,Balance
            # Distinguish by date format in first data row: CBA = DD/MM/YYYY, NAB = DD-Mon-YYYY
            if hdrs[:3] == ["Date", "Amount", "Description"]:
                if second:
                    first_cell = second.split(",")[0].strip().strip('"')
                    if re.match(r'\d{2}-[A-Za-z]{3}-\d{4}', first_cell):
                        return "nab_csv", {"display_name": "NAB", "type": "transaction"}
                return "commbank_csv", {"display_name": "CommBank", "type": "transaction"}
        except Exception:
            pass
        return "unknown_csv", {}
    return "skip", {}


# ── Generic CSV parser (profile-driven) ───────────────────────────────────────

def parse_generic_csv(filepath: Path, profile: dict) -> pd.DataFrame:
    """Parse a CSV using a saved bank profile column mapping."""
    skip = int(profile.get("skip_rows", 0))
    try:
        df = pd.read_csv(
            filepath, skiprows=skip, encoding="utf-8-sig", encoding_errors="ignore", dtype=str
        )
    except Exception as e:
        logger.error(f"  {filepath.name}: could not read CSV — {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Amount: single signed column or separate credit/debit columns
    credit_col = profile.get("credit_col") or ""
    debit_col = profile.get("debit_col") or ""
    if credit_col and debit_col and credit_col in df.columns and debit_col in df.columns:
        credit = pd.to_numeric(df[credit_col], errors="coerce").fillna(0)
        debit = pd.to_numeric(df[debit_col], errors="coerce").fillna(0)
        amounts = credit - debit
    else:
        amt_col = profile.get("amount_col", "")
        if not amt_col or amt_col not in df.columns:
            logger.error(f"  {filepath.name}: amount column '{amt_col}' not found in {list(df.columns)}")
            return pd.DataFrame()
        amounts = pd.to_numeric(df[amt_col], errors="coerce")

    if profile.get("negate_amounts"):
        amounts = -amounts

    date_col = profile.get("date_col", "")
    if not date_col or date_col not in df.columns:
        logger.error(f"  {filepath.name}: date column '{date_col}' not found in {list(df.columns)}")
        return pd.DataFrame()

    desc_col = profile.get("description_col", "")
    date_fmt = profile.get("date_format") or None

    result = pd.DataFrame()
    result["date"] = pd.to_datetime(
        df[date_col], format=date_fmt, dayfirst=True, errors="coerce"
    )
    result["amount"] = amounts
    result["description"] = (
        df[desc_col].astype(str).str.strip() if desc_col and desc_col in df.columns
        else ""
    )
    result["payee_name"] = ""
    result["reference"] = ""
    result["note"] = ""
    result["account"] = profile.get("display_name", filepath.stem)
    result["account_type"] = profile.get("account_type", "transaction")
    result["source_file"] = filepath.name
    result["is_pending"] = False

    result = result.dropna(subset=["date", "amount"])
    result = result[result["amount"] != 0]
    return result[["date", "amount", "description", "payee_name", "reference",
                   "note", "account", "account_type", "source_file", "is_pending"]]


# ── Deduplication ─────────────────────────────────────────────────────────────

def _make_txn_id(row: pd.Series) -> str:
    key = f"{row['date'].date()}|{row['amount']:.2f}|{str(row['description']).upper()[:50]}|{row['account']}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── Fuzzy duplicate detection ─────────────────────────────────────────────────

def _warn_fuzzy_duplicates(df: pd.DataFrame) -> None:
    """Warn about near-duplicate transactions: same account + amount + description prefix within 1 day.

    This catches overlapping statement periods (same transaction imported twice with different
    source files). Does not auto-remove — the user should investigate and re-run with correct files.
    """
    if df.empty:
        return
    check = df.copy()
    check["desc30"] = check["description"].str[:30].str.upper().str.strip()

    # Self-join on the key fields, then filter to pairs within 1 day
    merged = check[["txn_id", "date", "amount", "account", "desc30"]].merge(
        check[["txn_id", "date", "amount", "account", "desc30"]],
        on=["account", "amount", "desc30"],
        suffixes=("_a", "_b"),
    )
    # Keep only ordered pairs (a < b) to avoid double-counting
    merged = merged[merged["txn_id_a"] < merged["txn_id_b"]]
    merged["date_diff"] = (merged["date_a"] - merged["date_b"]).abs()
    suspects = merged[merged["date_diff"] <= pd.Timedelta(days=1)]

    if not suspects.empty:
        logger.warning(f"  Warning: {len(suspects)} potential near-duplicate pair(s) detected "
                       "(same account/amount/description within 1 day). "
                       "Check for overlapping statement periods.")


# ── Exclusion filter ──────────────────────────────────────────────────────────

def _apply_exclusions(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    exclusions = config.get("exclude_from_analysis", [])
    mask = pd.Series(False, index=df.index)
    for rule in exclusions:
        frag = rule.get("description_contains", "")
        acct = rule.get("account")
        if frag:
            row_match = df["description"].str.contains(re.escape(frag), case=False, na=False)
            if acct:
                row_match = row_match & (df["account"] == acct)
            mask |= row_match
    excluded = mask.sum()
    if excluded:
        logger.info(f"  Excluded {excluded} internal transfer rows")
    return df[~mask].reset_index(drop=True)


# ── Main loader ───────────────────────────────────────────────────────────────

def load_all_transactions(
    config: dict,
    balance_collector: list | None = None,
) -> pd.DataFrame:
    """Parse all statement files in Raw Data and return a combined DataFrame.

    If *balance_collector* is provided, closing-balance snapshots are appended
    to it for supported file types (ANZ Plus PDF, Revolut CSV).
    """
    input_dir = Path(config["data"]["input_dir"])
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir.resolve()}")

    all_files: set[Path] = set()
    for suffix in ("*.csv", "*.CSV", "*.html", "*.HTML", "*.pdf", "*.PDF", "*.ofx", "*.OFX", "*.qfx", "*.QFX"):
        all_files.update(input_dir.glob(suffix))

    all_dfs = []
    _stmt_periods: list[dict] = []

    for filepath in sorted(all_files):
        if not filepath.is_file():
            continue
        file_type, acct_conf = _detect_file_type(filepath, config)
        if file_type == "skip":
            continue

        # For PDFs, extract statement period before parsing so it's recorded
        # even when the parser returns an empty DataFrame (zero-activity statement).
        if filepath.suffix.lower() == ".pdf":
            try:
                import pdfplumber as _pp
                with _pp.open(filepath) as _pdf:
                    _txt = (_pdf.pages[0].extract_text() or "") if _pdf.pages else ""
                _ps, _pe = _extract_statement_period(_txt)
                if _ps and _pe:
                    _stmt_periods.append({
                        "account": acct_conf.get("display_name", filepath.stem),
                        "period_start": _ps.strftime("%Y-%m-%d"),
                        "period_end": _pe.strftime("%Y-%m-%d"),
                        "source_file": filepath.name,
                    })
            except Exception:
                pass

        logger.info(f"  Parsing {filepath.name} ...")
        try:
            if file_type == "anz_csv":
                df = parse_anz_csv(filepath,
                                   account_name=acct_conf.get("display_name", filepath.stem),
                                   account_type=acct_conf.get("type", "transaction"))
            elif file_type == "anz_plus_pdf":
                df = parse_anz_plus_pdf(filepath,
                                        account_name=acct_conf.get("display_name", filepath.stem),
                                        account_type=acct_conf.get("type", "transaction"))
            elif file_type == "anz_access_advantage_pdf":
                df = parse_anz_access_advantage_pdf(filepath,
                                                    account_name=acct_conf.get("display_name", "ANZ Personal"),
                                                    account_type=acct_conf.get("type", "transaction"))
            elif file_type == "anz_etrade_pdf":
                df = parse_anz_etrade_pdf(filepath,
                                          account_name=acct_conf.get("display_name", "ANZ ETrade"),
                                          account_type="investment")
            elif file_type == "anz_progress_saver_pdf":
                df = parse_anz_etrade_pdf(filepath,
                                          account_name=acct_conf.get("display_name", "ANZ Progress Saver"),
                                          account_type="savings")
            elif file_type == "latitude_html":
                df = parse_latitude_html(filepath)
            elif file_type == "paypal_csv":
                df = parse_paypal_csv(filepath)
            elif file_type == "revolut_csv":
                df = parse_revolut_csv(filepath,
                                       account_name=acct_conf.get("display_name", "Revolut"),
                                       account_type=acct_conf.get("type", "transaction"))
            elif file_type == "latitude_pdf":
                df = parse_28degrees_pdf(
                    filepath,
                    account_name=acct_conf.get("display_name", "28 Degrees Credit Card"),
                    account_type=acct_conf.get("type", "credit_card"),
                )
            elif file_type == "wise_pdf":
                df = parse_wise_pdf(filepath,
                                    account_name=acct_conf.get("display_name", "Wise"),
                                    account_type=acct_conf.get("type", "transaction"))
            elif file_type == "commbank_csv":
                df = parse_commbank_csv(filepath,
                                        account_name=acct_conf.get("display_name", "CommBank"),
                                        account_type=acct_conf.get("type", "transaction"))
            elif file_type == "westpac_csv":
                df = parse_westpac_csv(filepath,
                                       account_name=acct_conf.get("display_name", "Westpac"),
                                       account_type=acct_conf.get("type", "transaction"))
            elif file_type == "nab_csv":
                df = parse_nab_csv(filepath,
                                   account_name=acct_conf.get("display_name", "NAB"),
                                   account_type=acct_conf.get("type", "transaction"))
            elif file_type == "ofx":
                df = parse_ofx(filepath,
                               account_name=acct_conf.get("display_name", "Bank"),
                               account_type=acct_conf.get("type", "transaction"))
            elif file_type == "generic_csv":
                df = parse_generic_csv(filepath, acct_conf)
            elif file_type == "unknown_csv":
                logger.warning(
                    f"  {filepath.name}: unrecognised CSV — create a bank profile at "
                    "Settings > Bank Profiles to import this file"
                )
                continue
            else:
                logger.info(f"  {filepath.name}: skipped (unrecognised type)")
                continue

            if df.empty:
                if filepath.suffix.lower() != ".pdf":
                    _acct_name = acct_conf.get("display_name", filepath.stem)
                    _fs, _fe = _extract_period_from_csv_filename(filepath.name)
                    if _fs and _fe:
                        _stmt_periods.append({
                            "account": _acct_name,
                            "period_start": _fs.strftime("%Y-%m-%d"),
                            "period_end": _fe.strftime("%Y-%m-%d"),
                            "source_file": filepath.name,
                        })
                logger.info(f"  {filepath.name}: empty")
                continue
            logger.info(f"  {filepath.name}: {len(df)} transactions")
            all_dfs.append(df)

            # Record statement period for non-PDF files from transaction date range
            if filepath.suffix.lower() != ".pdf":
                try:
                    _acct_name = acct_conf.get("display_name", filepath.stem)
                    _dates = pd.to_datetime(df["date"], errors="coerce").dropna()
                    if not _dates.empty:
                        _stmt_periods.append({
                            "account": _acct_name,
                            "period_start": _dates.min().strftime("%Y-%m-%d"),
                            "period_end": _dates.max().strftime("%Y-%m-%d"),
                            "source_file": filepath.name,
                        })
                except Exception:
                    pass

            # Extract closing balance for supported file types
            if balance_collector is not None:
                try:
                    from src.balance_tracker import (
                        extract_anz_plus_balance, extract_revolut_balance,
                    )
                    snap = None
                    if file_type == "anz_plus_pdf":
                        snap = extract_anz_plus_balance(
                            filepath,
                            account_name=acct_conf.get("display_name", filepath.stem),
                            account_type=acct_conf.get("type", "transaction"),
                        )
                    elif file_type == "revolut_csv":
                        snap = extract_revolut_balance(
                            filepath,
                            account_name=acct_conf.get("display_name", "Revolut"),
                        )
                    if snap:
                        balance_collector.append(snap)
                except Exception:
                    pass  # balance extraction is best-effort; never block imports

        except Exception as exc:
            import traceback
            logger.error(f"ERROR: {exc}")
            traceback.print_exc()

    # Persist statement periods to DB (covers zero-activity statements too)
    if _stmt_periods:
        try:
            from src.db import save_statement_periods
            save_statement_periods(_stmt_periods, config)
        except Exception:
            pass  # best-effort; never block imports

    if not all_dfs:
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)
    df = df.sort_values("date", ascending=False).reset_index(drop=True)

    df["txn_id"] = df.apply(_make_txn_id, axis=1)
    before = len(df)
    df = df.drop_duplicates(subset=["txn_id"], keep="first").reset_index(drop=True)
    dupes = before - len(df)
    if dupes:
        logger.info(f"  Removed {dupes} duplicate transactions (overlapping date ranges)")

    df = _apply_exclusions(df, config)
    _warn_fuzzy_duplicates(df)
    logger.info(f"  -> {len(df)} transactions ready for analysis")
    return df
