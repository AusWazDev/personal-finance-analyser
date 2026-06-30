"""
SQLite database layer for the personal finance analyser.

Schema
------
transactions        — master transaction store (INSERT OR IGNORE on txn_id PK)
balance_snapshots   — closing balance per (date, account) for net-worth chart
override_history    — audit log of category/note overrides with undo support

All public functions accept a `config` dict and create their own short-lived
connection, committed and closed before returning.  WAL mode is enabled on
first open so concurrent reads never block writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

logger = logging.getLogger(__name__)


# ── Connection ─────────────────────────────────────────────────────────────────

def _db_path(config: dict) -> Path:
    return Path(config.get("data", {}).get("database", "data/finance.db"))


def get_db(config: dict) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    from src.db_crypto import get_connection, get_passphrase
    path = _db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    passphrase = get_passphrase(config)
    conn = get_connection(path, passphrase, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def open_db(config: dict) -> Iterator[sqlite3.Connection]:
    """Context manager: opens the DB and guarantees conn.close() on exit."""
    conn = get_db(config)
    try:
        yield conn
    finally:
        conn.close()


# ── Schema migrations ──────────────────────────────────────────────────────────
#
# Each migration is a plain function that runs once, guarded by PRAGMA user_version.
# To add a migration:
#   1. Write _migration_NNN_<name>(conn) below.
#   2. Append (NNN, _migration_NNN_<name>) to _MIGRATIONS.
#   3. Bump _CURRENT_VERSION to NNN.
#
# PRAGMA user_version is SQLite's built-in schema-version integer (stored in the
# DB header). No separate table needed.

_CURRENT_VERSION = 8


def _get_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _migration_001_add_is_tax_deductible(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN is_tax_deductible INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already present (fresh install from updated schema)


def _migration_002_add_is_gst_claimable(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN is_gst_claimable INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass


def _migration_003_add_tags(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN tags TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass


def _migration_004_add_split_columns(conn: sqlite3.Connection) -> None:
    for col_sql in (
        "ALTER TABLE transactions ADD COLUMN parent_txn_id TEXT DEFAULT NULL",
        "ALTER TABLE transactions ADD COLUMN is_split_parent INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass


def _migration_005_add_is_savings(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN is_savings INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass


def _migration_006_add_source_id(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN source_id TEXT DEFAULT NULL")
        conn.commit()
    except Exception:
        pass


def _migration_007_add_is_anomaly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN is_anomaly INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass


def _migration_008_add_receipt_path(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN receipt_path TEXT DEFAULT NULL")
        conn.commit()
    except Exception:
        pass


_MIGRATIONS: list[tuple[int, Any]] = [
    (1, _migration_001_add_is_tax_deductible),
    (2, _migration_002_add_is_gst_claimable),
    (3, _migration_003_add_tags),
    (4, _migration_004_add_split_columns),
    (5, _migration_005_add_is_savings),
    (6, _migration_006_add_source_id),
    (7, _migration_007_add_is_anomaly),
    (8, _migration_008_add_receipt_path),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations in version order."""
    current = _get_schema_version(conn)
    for version, fn in _MIGRATIONS:
        if version > current:
            fn(conn)
            _set_schema_version(conn, version)


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and apply schema migrations. Safe to call on every startup."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id          TEXT PRIMARY KEY,
            date            TEXT NOT NULL,
            amount          REAL NOT NULL,
            description     TEXT,
            payee_name      TEXT,
            reference       TEXT,
            note            TEXT,
            account         TEXT,
            account_type    TEXT,
            bank            TEXT,
            bsb             TEXT,
            account_number  TEXT,
            category        TEXT,
            sub_category    TEXT,
            is_business         INTEGER DEFAULT 0,
            is_tax_deductible   INTEGER DEFAULT 0,
            is_gst_claimable    INTEGER DEFAULT 0,
            user_note           TEXT,
            tags                TEXT DEFAULT '',
            source_file     TEXT,
            zip_source      TEXT,
            source_id       TEXT DEFAULT NULL,
            parent_txn_id   TEXT DEFAULT NULL,
            is_split_parent INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_txn_date     ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_txn_account  ON transactions(account);
        CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);

        CREATE TABLE IF NOT EXISTS balance_snapshots (
            date            TEXT NOT NULL,
            account         TEXT NOT NULL,
            account_type    TEXT,
            balance         REAL NOT NULL,
            source_file     TEXT,
            PRIMARY KEY (date, account)
        );

        CREATE TABLE IF NOT EXISTS override_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id        TEXT NOT NULL UNIQUE,
            applied_at      TEXT NOT NULL,
            undone          INTEGER DEFAULT 0,
            changes_json    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS statement_periods (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account      TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            source_file  TEXT NOT NULL,
            imported_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source_file, account)
        );

        CREATE TABLE IF NOT EXISTS scanned_archives (
            zip_file    TEXT PRIMARY KEY,
            scanned_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS accounts (
            account_name    TEXT PRIMARY KEY,
            friendly_name   TEXT,
            statement_name  TEXT,
            institution     TEXT,
            bsb             TEXT,
            account_number  TEXT,
            identifier      TEXT,
            identifier_type TEXT,
            notes           TEXT,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    _apply_migrations(conn)


def _clean_invalid_subcategories(conn: sqlite3.Connection) -> None:
    """Clear sub_category where it doesn't match SUBCATS[category].

    Keeps system-assigned values (Reversal, Refund, Pay-in-4) regardless.
    """
    from src.utils import is_valid_subcat
    rows = conn.execute(
        "SELECT txn_id, category, sub_category FROM transactions "
        "WHERE sub_category IS NOT NULL AND sub_category != ''"
    ).fetchall()
    invalid = [
        r["txn_id"] for r in rows
        if not is_valid_subcat(r["category"], r["sub_category"])
    ]
    if invalid:
        ph = ",".join("?" * len(invalid))
        conn.execute(
            f"UPDATE transactions SET sub_category = '' WHERE txn_id IN ({ph})", invalid
        )
        conn.commit()


def _migrate_txn_ids_to_uppercase(conn: sqlite3.Connection) -> None:
    """Recompute txn_ids using UPPER(description) before hashing.

    Affects only rows with mixed-case descriptions (PayPal, Revolut, Wise).
    ANZ rows are already all-caps — their txn_ids are unchanged.
    Idempotent: on subsequent runs, old_id == new_id for all rows → nothing to do.
    """
    rows = conn.execute(
        "SELECT txn_id, date, amount, description, account FROM transactions "
        "WHERE description != UPPER(description)"
    ).fetchall()

    if not rows:
        return

    existing_ids = {r[0] for r in conn.execute("SELECT txn_id FROM transactions").fetchall()}

    to_update: list[tuple[str, str]] = []  # (new_id, old_id)
    to_delete: list[str] = []              # old_id where new_id already exists (genuine dup)

    for row in rows:
        old_id   = row["txn_id"]
        date_str = str(row["date"])[:10]
        amount   = float(row["amount"])
        desc     = str(row["description"]).upper()[:50]
        account  = str(row["account"])

        key    = f"{date_str}|{amount:.2f}|{desc}|{account}"
        new_id = hashlib.md5(key.encode()).hexdigest()[:12]

        if new_id == old_id:
            continue  # already using uppercase hash — no-op

        if new_id in existing_ids:
            to_delete.append(old_id)
        else:
            to_update.append((new_id, old_id))
            existing_ids.add(new_id)
            existing_ids.discard(old_id)

    if to_delete:
        ph = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM transactions WHERE txn_id IN ({ph})", to_delete)

    for new_id, old_id in to_update:
        conn.execute("UPDATE transactions SET txn_id = ? WHERE txn_id = ?", (new_id, old_id))

    if to_update or to_delete:
        conn.commit()
        logger.info(f"  [migration] Normalised {len(to_update)} txn_id(s) to uppercase hash; "
                    f"removed {len(to_delete)} case-sensitivity duplicate(s)")


def run_data_quality_checks(conn: sqlite3.Connection) -> None:
    """Idempotent data-quality passes. Call once at server startup — not per-request."""
    _clean_invalid_subcategories(conn)
    _migrate_txn_ids_to_uppercase(conn)


# ── Read ───────────────────────────────────────────────────────────────────────

def load_transactions(
    config: dict,
    include_split_parents: bool = False,
    *,
    since: str | None = None,
    until: str | None = None,
) -> pd.DataFrame:
    """
    Load transactions from SQLite as a typed DataFrame.

    include_split_parents=False (default) excludes rows that have been split into
    children — only child rows count toward category totals in reports.
    Pass True to retrieve all rows (e.g. the transactions browsing page).

    since / until: optional ISO date strings ("YYYY-MM-DD") pushed into SQL to
    avoid materialising the whole table when only a date window is needed.
    """
    conn = get_db(config)
    try:
        init_db(conn)
        where_parts: list[str] = []
        params: list[str] = []
        if not include_split_parents:
            where_parts.append("(is_split_parent = 0 OR is_split_parent IS NULL)")
        if since:
            where_parts.append("date >= ?")
            params.append(since)
        if until:
            where_parts.append("date <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        df = pd.read_sql_query(
            f"SELECT * FROM transactions{where} ORDER BY date ASC",
            conn,
            params=params or None,
        )
    finally:
        conn.close()

    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["is_business"] = df["is_business"].astype(bool)
    if "is_tax_deductible" not in df.columns:
        df["is_tax_deductible"] = False
    df["is_tax_deductible"] = df["is_tax_deductible"].astype(bool)
    if "is_gst_claimable" not in df.columns:
        df["is_gst_claimable"] = False
    df["is_gst_claimable"] = df["is_gst_claimable"].astype(bool)
    df["sub_category"] = df["sub_category"].fillna("")
    df["user_note"] = df["user_note"].fillna("")
    df = df.dropna(subset=["date", "amount"]).reset_index(drop=True)

    # Apply exclude_from_analysis rules so transactions excluded at import time
    # are also excluded when reading from the DB (handles rows loaded before
    # the exclusion rules existed).
    import re as _re
    exclusions = config.get("exclude_from_analysis", [])
    if exclusions and not df.empty:
        mask = pd.Series(False, index=df.index)
        for rule in exclusions:
            frag = rule.get("description_contains", "")
            acct = rule.get("account")
            if frag:
                row_match = df["description"].str.contains(_re.escape(frag), case=False, na=False)
                if acct:
                    row_match = row_match & (df["account"] == acct)
                mask |= row_match
        excluded = int(mask.sum())
        if excluded:
            logger.info(f"  [load_transactions] excluded {excluded} row(s) matching exclude_from_analysis")
        df = df[~mask].reset_index(drop=True)

    return df


# ── Write ──────────────────────────────────────────────────────────────────────

def _is_biz_int(value: Any) -> int:
    return 1 if value in (True, 1, "True", "1", "true") else 0


def upsert_transactions(df: pd.DataFrame, config: dict, zip_name: str = "") -> int:
    """
    Insert new transactions (INSERT OR IGNORE — txn_id PK prevents duplicates).
    Enriches bank/bsb/account_number from config accounts block before insert.
    Returns the number of rows inserted.
    """
    if df.empty:
        return 0

    acct_map: dict[str, dict] = {}
    for _key, acct in config.get("accounts", {}).items():
        name = acct.get("display_name", "")
        if name:
            acct_map[name] = {
                "bank": acct.get("bank", ""),
                "bsb": acct.get("bsb", ""),
                "account_number": acct.get("account_number", ""),
            }

    enriched = df.copy()
    enriched["bank"] = enriched["account"].map(
        lambda a: acct_map.get(a, {}).get("bank", ""))
    enriched["bsb"] = enriched["account"].map(
        lambda a: acct_map.get(a, {}).get("bsb", ""))
    enriched["account_number"] = enriched["account"].map(
        lambda a: acct_map.get(a, {}).get("account_number", ""))
    enriched["zip_source"] = zip_name
    enriched["date"] = pd.to_datetime(enriched["date"]).dt.strftime("%Y-%m-%d")
    enriched["is_business"] = enriched.get(
        "is_business", pd.Series(False, index=enriched.index)
    ).apply(_is_biz_int)

    for col in ("sub_category", "user_note", "note", "reference", "payee_name"):
        if col not in enriched.columns:
            enriched[col] = ""
        enriched[col] = enriched[col].fillna("")

    rows = enriched[[
        "txn_id", "date", "amount", "description", "payee_name",
        "reference", "note", "account", "account_type",
        "bank", "bsb", "account_number",
        "category", "sub_category", "is_business", "user_note",
        "source_file", "zip_source",
    ]].to_dict("records")

    conn = get_db(config)
    try:
        init_db(conn)
        cur = conn.executemany(
            """INSERT OR IGNORE INTO transactions
               (txn_id, date, amount, description, payee_name, reference, note,
                account, account_type, bank, bsb, account_number,
                category, sub_category, is_business, user_note, source_file, zip_source)
               VALUES (:txn_id, :date, :amount, :description, :payee_name, :reference,
                       :note, :account, :account_type, :bank, :bsb, :account_number,
                       :category, :sub_category, :is_business, :user_note,
                       :source_file, :zip_source)""",
            rows,
        )
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()

    ignored = len(rows) - count
    logger.info(f"  DB: inserted {count} new transactions"
               + (f" (ignored {ignored} duplicates)" if ignored else ""))
    return count


def upsert_basiq_transactions(rows: list[dict], config: dict) -> int:
    """
    Insert Basiq CDR transactions, storing source_id for provenance.
    INSERT OR IGNORE means existing rows (same txn_id hash) are silently skipped,
    so manual CSV imports and CDR imports of the same transaction never duplicate.
    Returns the number of new rows inserted.
    """
    if not rows:
        return 0

    acct_map: dict[str, dict] = {}
    for _key, acct in config.get("accounts", {}).items():
        name = acct.get("display_name", "")
        if name:
            acct_map[name] = {
                "bank": acct.get("bank", ""),
                "bsb": acct.get("bsb", ""),
                "account_number": acct.get("account_number", ""),
            }

    enriched = []
    for row in rows:
        acct_name = row.get("account", "")
        meta = acct_map.get(acct_name, {})
        enriched.append({
            **row,
            "bank": meta.get("bank", ""),
            "bsb": meta.get("bsb", ""),
            "account_number": meta.get("account_number", ""),
            "is_business": 0,
        })

    conn = get_db(config)
    try:
        init_db(conn)
        cur = conn.executemany(
            """INSERT OR IGNORE INTO transactions
               (txn_id, source_id, date, amount, description, payee_name, reference, note,
                account, account_type, bank, bsb, account_number,
                category, sub_category, is_business, user_note, source_file, zip_source)
               VALUES (:txn_id, :source_id, :date, :amount, :description, :payee_name,
                       :reference, :note, :account, :account_type, :bank, :bsb,
                       :account_number, :category, :sub_category, :is_business,
                       :user_note, :source_file, :zip_source)""",
            enriched,
        )
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()

    ignored = len(rows) - count
    logger.info(f"  Basiq: inserted {count} new CDR transactions"
                + (f" ({ignored} duplicates skipped)" if ignored else ""))
    return count


def update_transaction(txn_id: str, fields: dict[str, Any], config: dict) -> bool:
    """
    Update one or more fields on a single transaction row (auto-save PATCH endpoint).
    Only fields in the safe-list are written.  Returns True if a row was updated.
    """
    _ALLOWED = {
        "category", "sub_category", "is_business", "is_tax_deductible",
        "is_gst_claimable", "user_note", "description", "reference", "note", "tags",
        "is_split_parent",
    }
    safe = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not safe:
        return False

    if "is_business" in safe:
        safe["is_business"] = _is_biz_int(safe["is_business"])
    if "is_tax_deductible" in safe:
        safe["is_tax_deductible"] = _is_biz_int(safe["is_tax_deductible"])
    if "is_gst_claimable" in safe:
        safe["is_gst_claimable"] = _is_biz_int(safe["is_gst_claimable"])

    set_clause = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + [txn_id]

    conn = get_db(config)
    try:
        cur = conn.execute(
            f"UPDATE transactions SET {set_clause} WHERE txn_id = ?", values
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_transactions_bulk(
    txn_ids: list[str], fields: dict[str, Any], config: dict
) -> int:
    """
    Apply field updates to a list of txn_ids (apply-to-all override).
    Returns number of rows updated.
    """
    _ALLOWED = {
        "category", "sub_category", "is_business", "user_note",
        "description", "reference", "note",
    }
    safe = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not safe or not txn_ids:
        return 0

    if "is_business" in safe:
        safe["is_business"] = _is_biz_int(safe["is_business"])

    placeholders = ",".join("?" * len(txn_ids))
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + list(txn_ids)

    conn = get_db(config)
    try:
        cur = conn.execute(
            f"UPDATE transactions SET {set_clause} WHERE txn_id IN ({placeholders})",
            values,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_transaction_categories(df: pd.DataFrame, config: dict) -> int:
    """
    Bulk-update category, sub_category, is_business from a DataFrame.
    Used by --recategorise-all and after AI categorisation runs.
    Returns number of rows updated.
    """
    if df.empty:
        return 0

    rows = [
        (
            str(row.get("category", "")),
            str(row.get("sub_category", "")),
            _is_biz_int(row.get("is_business")),
            str(row["txn_id"]),
        )
        for _, row in df.iterrows()
    ]

    conn = get_db(config)
    try:
        cur = conn.executemany(
            "UPDATE transactions SET category = ?, sub_category = ?, is_business = ? "
            "WHERE txn_id = ?",
            rows,
        )
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()

    logger.info(f"  DB: updated {count} transaction categories")
    return count


def update_transaction_descriptions(df: pd.DataFrame, config: dict) -> int:
    """
    Update description and reference for PayPal-enriched rows.
    Only processes rows where description starts with 'PayPal: '.
    Returns number of rows updated.
    """
    enriched = df[df["description"].str.startswith("PayPal: ", na=False)].copy()
    if enriched.empty:
        return 0

    rows = [
        (
            str(row["description"]),
            str(row.get("reference", "") or ""),
            str(row["txn_id"]),
        )
        for _, row in enriched.iterrows()
    ]

    conn = get_db(config)
    try:
        # Only fill reference when currently blank
        cur = conn.executemany(
            """UPDATE transactions
               SET description = ?,
                   reference = CASE WHEN (reference IS NULL OR reference = '')
                                    THEN ? ELSE reference END
               WHERE txn_id = ?""",
            rows,
        )
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()

    logger.info(f"  DB: updated {count} PayPal descriptions")
    return count


# ── Statement periods ──────────────────────────────────────────────────────────

def save_statement_periods(periods: list[dict], config: dict) -> int:
    """Insert statement periods (INSERT OR IGNORE — source_file+account is the unique key).

    Each dict must have: account, period_start (YYYY-MM-DD), period_end (YYYY-MM-DD), source_file.
    Returns number of new rows inserted.
    """
    if not periods:
        return 0
    conn = get_db(config)
    try:
        init_db(conn)
        cur = conn.executemany(
            """INSERT OR IGNORE INTO statement_periods
               (account, period_start, period_end, source_file)
               VALUES (:account, :period_start, :period_end, :source_file)""",
            periods,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def load_covered_months(config: dict) -> dict[str, list[str]]:
    """Return {account: [YYYY-MM, ...]} for every calendar month covered by a loaded statement.

    Used by the coverage page to distinguish zero-activity months (statement loaded,
    no transactions) from genuine gaps (no statement loaded for that period).
    """
    conn = get_db(config)
    try:
        init_db(conn)
        rows = conn.execute(
            "SELECT account, period_start, period_end FROM statement_periods"
        ).fetchall()
    finally:
        conn.close()

    from datetime import datetime as _dt
    covered: dict[str, set[str]] = {}
    for row in rows:
        acct = row["account"]
        try:
            ps = _dt.strptime(row["period_start"], "%Y-%m-%d")
            pe = _dt.strptime(row["period_end"], "%Y-%m-%d")
        except ValueError:
            continue
        y, m = ps.year, ps.month
        while (y, m) <= (pe.year, pe.month):
            covered.setdefault(acct, set()).add(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1

    return {acct: sorted(months) for acct, months in covered.items()}


# ── Balance snapshots ──────────────────────────────────────────────────────────

def upsert_balance_snapshots(snapshots: list[dict], config: dict) -> int:
    """
    Insert or replace balance snapshots (new value wins on (date, account) PK).
    Returns number of rows affected.
    """
    if not snapshots:
        return 0

    conn = get_db(config)
    try:
        init_db(conn)
        cur = conn.executemany(
            """INSERT OR REPLACE INTO balance_snapshots
               (date, account, account_type, balance, source_file)
               VALUES (:date, :account, :account_type, :balance, :source_file)""",
            snapshots,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def load_balance_snapshots(config: dict) -> pd.DataFrame:
    """Return all balance snapshots as a typed DataFrame."""
    conn = get_db(config)
    try:
        init_db(conn)
        df = pd.read_sql_query(
            "SELECT * FROM balance_snapshots ORDER BY account, date ASC", conn
        )
    finally:
        conn.close()

    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
    return df.dropna(subset=["date", "balance"]).reset_index(drop=True)


# ── Override history ───────────────────────────────────────────────────────────

def append_override_batch(batch: dict, config: dict) -> None:
    """Persist an override batch to the override_history table."""
    applied_at = batch.get("applied_at") or batch.get("timestamp", "")
    conn = get_db(config)
    try:
        init_db(conn)
        conn.execute(
            """INSERT OR IGNORE INTO override_history
               (batch_id, applied_at, undone, changes_json)
               VALUES (?, ?, 0, ?)""",
            (batch["batch_id"], applied_at, json.dumps(batch.get("changes", []))),
        )
        conn.commit()
    finally:
        conn.close()


def _batch_summary(changes: list[dict]) -> str:
    cats = sorted({c["new_value"] for c in changes if c.get("field") == "category"})
    n = len({c.get("txn_id", "") for c in changes})
    if cats:
        return f"Changed {n} transaction(s) → {', '.join(cats)}"
    notes = [c for c in changes if c.get("field") == "user_note"]
    if notes:
        return f"Note updated for transaction {notes[0].get('txn_id', '')[:8]}"
    return f"{n} transaction(s) updated"


def get_override_history(config: dict, limit: int = 20) -> list[dict]:
    """Return the most recent non-undone override batches, newest first."""
    conn = get_db(config)
    try:
        init_db(conn)
        rows = conn.execute(
            """SELECT batch_id, applied_at, changes_json
               FROM override_history
               WHERE undone = 0
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        changes = json.loads(row["changes_json"])
        result.append({
            "batch_id": row["batch_id"],
            "applied_at": row["applied_at"],
            "timestamp": row["applied_at"],   # kept for JS compatibility
            "summary": _batch_summary(changes),
            "changes": changes,
        })
    return result


def undo_override_batch(batch_id: str, config: dict) -> list[dict] | None:
    """
    Restore old values for all changes in a batch and mark the batch undone.
    Returns the changes list (so the caller can also clean up JSON caches),
    or None if the batch was not found or already undone.
    """
    conn = get_db(config)
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT changes_json, undone FROM override_history WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()

        if row is None or row["undone"]:
            return None

        changes: list[dict] = json.loads(row["changes_json"])
        _RESTORABLE = {"category", "sub_category", "is_business", "user_note"}

        for change in changes:
            txn_id = change.get("txn_id")
            field = change.get("field")
            old_value = change.get("old_value")
            if not txn_id or field not in _RESTORABLE:
                continue
            db_val = _is_biz_int(old_value) if field == "is_business" else old_value
            conn.execute(
                f"UPDATE transactions SET {field} = ? WHERE txn_id = ?",
                (db_val, txn_id),
            )

        conn.execute(
            "UPDATE override_history SET undone = 1 WHERE batch_id = ?", (batch_id,)
        )
        conn.commit()
        return changes
    finally:
        conn.close()


# ── Account metadata ───────────────────────────────────────────────────────────

_ACCOUNT_EDITABLE = {
    "friendly_name", "statement_name", "institution",
    "bsb", "account_number", "identifier", "identifier_type", "notes", "is_active",
    "is_savings",
}


def seed_accounts(conn: sqlite3.Connection, config: dict) -> None:
    """Ensure every account in transactions has a row in the accounts table.

    Pre-populates institution/BSB/account_number from config where available.
    Uses INSERT OR IGNORE so existing user-entered data is never overwritten.
    Safe to call on every startup.
    """
    # Merge config entries by display_name — first non-empty value wins per field
    config_by_name: dict[str, dict] = {}
    for acct_conf in config.get("accounts", {}).values():
        name = acct_conf.get("display_name", "")
        if not name:
            continue
        entry = config_by_name.setdefault(name, {})
        for field in ("bank", "bsb", "account_number"):
            if acct_conf.get(field) and not entry.get(field):
                entry[field] = acct_conf[field]

    rows = conn.execute(
        "SELECT DISTINCT account FROM transactions WHERE account IS NOT NULL"
    ).fetchall()
    for (account_name,) in rows:
        cfg = config_by_name.get(account_name, {})
        conn.execute(
            """INSERT OR IGNORE INTO accounts
               (account_name, institution, bsb, account_number)
               VALUES (?, ?, ?, ?)""",
            (account_name, cfg.get("bank", ""), cfg.get("bsb", ""), cfg.get("account_number", "")),
        )
    conn.commit()


def get_accounts(config: dict) -> list[dict]:
    """Return all accounts joined with transaction summary stats."""
    conn = get_db(config)
    try:
        rows = conn.execute("""
            SELECT a.*,
                   COUNT(t.txn_id)  AS txn_count,
                   MIN(t.date)      AS first_date,
                   MAX(t.date)      AS last_date
            FROM accounts a
            LEFT JOIN transactions t ON t.account = a.account_name
            GROUP BY a.account_name
            ORDER BY a.account_name
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_account(config: dict, account_name: str, fields: dict) -> None:
    """Create or update an account record. Only editable fields are written."""
    valid = {k: v for k, v in fields.items() if k in _ACCOUNT_EDITABLE}
    if not valid and account_name:
        # Bare upsert — ensure the row exists
        conn = get_db(config)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (account_name) VALUES (?)", (account_name,)
            )
            conn.commit()
        finally:
            conn.close()
        return
    conn = get_db(config)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (account_name) VALUES (?)", (account_name,)
        )
        sets = ", ".join(f"{k} = ?" for k in valid)
        conn.execute(
            f"UPDATE accounts SET {sets} WHERE account_name = ?",
            (*valid.values(), account_name),
        )
        conn.commit()
    finally:
        conn.close()
