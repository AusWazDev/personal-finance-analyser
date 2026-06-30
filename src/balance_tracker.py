"""
Account balance history tracker.

Extracts closing balances from statement files and persists them in SQLite
(balance_history table), giving one data point per statement per account.
Used to generate the net worth trend chart.

Supported sources:
  - ANZ Plus PDF  (opening + closing balance explicit in statement)
  - Revolut CSV   (Balance column, last row = closing)

28 Degrees and PayPal are omitted: the credit card balance would require
parsing "Amount Due" text whose layout varies, and PayPal balances are
typically negligible. Add extract_*_balance() functions here as needed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src import db as _db


_BALANCE_COLS = ["date", "account", "account_type", "balance", "source_file"]


# ── Persistence (delegates to SQLite) ─────────────────────────────────────────

def load_balance_history(config: dict) -> pd.DataFrame:
    """Load stored balance snapshots from SQLite as a typed DataFrame."""
    return _db.load_balance_snapshots(config)


def save_balance_history(snapshots_or_df: "list[dict] | pd.DataFrame", config: dict) -> None:
    """Persist balance snapshots to SQLite (and legacy CSV for compatibility)."""
    if isinstance(snapshots_or_df, pd.DataFrame):
        snapshots = snapshots_or_df.to_dict("records")
        # Normalise dates to strings for upsert
        for s in snapshots:
            if hasattr(s.get("date"), "strftime"):
                s["date"] = s["date"].strftime("%Y-%m-%d")
    else:
        snapshots = snapshots_or_df
    _db.upsert_balance_snapshots(snapshots, config)


def merge_snapshots(existing: pd.DataFrame, new_snapshots: list[dict]) -> pd.DataFrame:
    """
    Merge new balance snapshots into an existing DataFrame.
    Deduplicates by (date, account) — new value wins over old.
    Used to build an in-memory merged set before calling save_balance_history.
    """
    if not new_snapshots:
        return existing
    new_df = pd.DataFrame(new_snapshots)
    new_df["date"] = pd.to_datetime(new_df["date"], errors="coerce")
    new_df["balance"] = pd.to_numeric(new_df["balance"], errors="coerce")
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date", "account"], keep="last")
    return combined.sort_values(["account", "date"]).reset_index(drop=True)


# ── Extractors ────────────────────────────────────────────────────────────────

def extract_anz_plus_balance(
    filepath: str | Path,
    account_name: str,
    account_type: str = "transaction",
) -> dict | None:
    """Return the closing balance snapshot from an ANZ Plus PDF statement.

    The ANZ Plus PDF lists transactions newest-first. The balance column of the
    first transaction row equals the closing balance for the period. The period
    end date from the statement header is used as the snapshot date.
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    # Reuse the already-compiled regexes from parsers
    from src.parsers import _TXN_RE, _OPENING_RE, _extract_period

    lines: list[str] = []
    try:
        with pdfplumber.open(str(filepath)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(ln.strip() for ln in text.split("\n"))
    except Exception:
        return None

    _, period_end = _extract_period(lines)
    if period_end is None:
        return None

    # First TXN_RE match in the PDF = newest transaction = closing balance
    closing_balance: float | None = None
    for line in lines:
        m = _TXN_RE.match(line)
        if m:
            closing_balance = float(m.group(4).replace(",", ""))
            break

    # Empty statement: fall back to Opening Balance (= closing = opening when no transactions)
    if closing_balance is None:
        for line in lines:
            m = _OPENING_RE.match(line)
            if m:
                closing_balance = float(m.group(1).replace(",", ""))
                break

    if closing_balance is None:
        return None

    return {
        "date": period_end.strftime("%Y-%m-%d"),
        "account": account_name,
        "account_type": account_type,
        "balance": closing_balance,
        "source_file": Path(filepath).name,
    }


def extract_revolut_balance(
    filepath: str | Path,
    account_name: str = "Revolut",
) -> dict | None:
    """Return the closing balance snapshot from a Revolut CSV export."""
    try:
        try:
            raw = pd.read_csv(str(filepath), encoding="utf-8-sig")
        except UnicodeDecodeError:
            raw = pd.read_csv(str(filepath), encoding="latin-1")
    except Exception:
        return None

    if "Balance" not in raw.columns:
        return None

    if "State" in raw.columns:
        raw = raw[raw["State"] == "COMPLETED"].copy()

    if raw.empty:
        return None

    date_col = "Completed Date" if "Completed Date" in raw.columns else "Started Date"
    raw["_date"] = pd.to_datetime(raw[date_col], errors="coerce")
    raw["_balance"] = pd.to_numeric(
        raw["Balance"].astype(str).str.replace(",", ""), errors="coerce"
    )
    valid = raw.dropna(subset=["_date", "_balance"]).sort_values("_date")
    if valid.empty:
        return None

    last = valid.iloc[-1]
    return {
        "date": last["_date"].strftime("%Y-%m-%d"),
        "account": account_name,
        "account_type": "revolut",
        "balance": float(last["_balance"]),
        "source_file": Path(filepath).name,
    }
