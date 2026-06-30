"""Business transaction export — OFX (Xero), QIF (MYOB/Quicken), CSV.

Generates export files for business-flagged and GST-claimable transactions
suitable for import into Xero, MYOB, or any OFX/QIF-compatible accounting package.

Category → account code mapping is read from config.yaml under
`business_export.account_codes` (optional):
    business_export:
      account_codes:
        "Business Expense": "6000"
        "Travel": "6200"
"""

import csv
import io
from datetime import date as _date


def _account_code(category: str, config: dict) -> str:
    """Return the configured account code for a category, or the category name."""
    mapping = config.get("business_export", {}).get("account_codes", {})
    return str(mapping.get(category, category))


def _ofx_date(d) -> str:
    """Format a date as OFX YYYYMMDDHHMMSS."""
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d000000")
    return str(d).replace("-", "")[:8] + "000000"


def generate_ofx(rows: list[dict], fy: int, config: dict) -> str:
    """Generate an OFX 1.02 SGML file for the given business transaction rows.

    Each row must have: txn_id, date, amount, description, category, account.
    Returns a string (not bytes — OFX 1.02 is ASCII).
    """
    if rows:
        dt_start = min(_ofx_date(r["date"]) for r in rows)
        dt_end   = max(_ofx_date(r["date"]) for r in rows)
    else:
        dt_start = f"{fy - 1}07010000000"
        dt_end   = f"{fy}06300000000"

    now_str = _date.today().strftime("%Y%m%d%H%M%S")

    txn_lines = []
    for r in rows:
        amt   = float(r.get("amount", 0))
        ttype = "DEBIT" if amt < 0 else "CREDIT"
        desc  = str(r.get("description", ""))[:32].replace("&", "&amp;").replace("<", "&lt;")
        memo  = _account_code(r.get("category", ""), config)
        fitid = str(r.get("txn_id", ""))[:36]
        txn_lines.append(
            f"<STMTTRN>\n"
            f"<TRNTYPE>{ttype}\n"
            f"<DTPOSTED>{_ofx_date(r['date'])}\n"
            f"<TRNAMT>{amt:.2f}\n"
            f"<FITID>{fitid}\n"
            f"<NAME>{desc}\n"
            f"<MEMO>{memo}\n"
            f"</STMTTRN>"
        )

    return (
        "OFXHEADER:100\n"
        "DATA:OFSGML\n"
        "VERSION:102\n"
        "SECURITY:NONE\n"
        "ENCODING:USASCII\n"
        "CHARSET:1252\n"
        "COMPRESSION:NONE\n"
        "OLDFILEUID:NONE\n"
        "NEWFILEUID:NONE\n\n"
        "<OFX>\n"
        "<SIGNONMSGSRSV1>\n"
        "<SONRS>\n"
        "<STATUS>\n<CODE>0\n<SEVERITY>INFO\n</STATUS>\n"
        f"<DTSERVER>{now_str}\n"
        "<LANGUAGE>ENG\n"
        "</SONRS>\n"
        "</SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1>\n"
        "<STMTTRNRS>\n"
        "<TRNUID>1001\n"
        "<STATUS>\n<CODE>0\n<SEVERITY>INFO\n</STATUS>\n"
        "<STMTRS>\n"
        "<CURDEF>AUD\n"
        "<BANKACCTFROM>\n"
        "<BANKID>PFAEXPORT\n"
        f"<ACCTID>FY{fy}_BUSINESS\n"
        "<ACCTTYPE>CHECKING\n"
        "</BANKACCTFROM>\n"
        "<BANKTRANLIST>\n"
        f"<DTSTART>{dt_start}\n"
        f"<DTEND>{dt_end}\n"
        + "\n".join(txn_lines) + "\n"
        "</BANKTRANLIST>\n"
        "</STMTRS>\n"
        "</STMTTRNRS>\n"
        "</BANKMSGSRSV1>\n"
        "</OFX>\n"
    )


def generate_qif(rows: list[dict], fy: int, config: dict) -> str:
    """Generate a QIF file for the given business transaction rows.

    Uses !Type:Bank. Each entry: D (date), T (amount), P (payee/desc), L (category/account).
    """
    lines = ["!Type:Bank"]
    for r in rows:
        amt  = float(r.get("amount", 0))
        desc = str(r.get("description", ""))[:80]
        cat  = _account_code(r.get("category", ""), config)
        d    = r.get("date")
        if hasattr(d, "strftime"):
            date_str = d.strftime("%m/%d/%Y")
        else:
            parts = str(d)[:10].split("-")
            date_str = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else str(d)[:10]
        lines += [
            f"D{date_str}",
            f"T{amt:.2f}",
            f"P{desc}",
            f"L{cat}",
            "^",
        ]
    return "\n".join(lines) + "\n"


def generate_csv(rows: list[dict], config: dict) -> bytes:
    """Generate a CSV export of the given business transaction rows."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Description", "Category", "Account Code", "Account", "Amount (AUD)", "TxnID"])
    for r in rows:
        w.writerow([
            str(r.get("date", ""))[:10],
            str(r.get("description", "")),
            str(r.get("category", "")),
            _account_code(r.get("category", ""), config),
            str(r.get("account", "")),
            f"{float(r.get('amount', 0)):.2f}",
            str(r.get("txn_id", "")),
        ])
    return buf.getvalue().encode("utf-8")
