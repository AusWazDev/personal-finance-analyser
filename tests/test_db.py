"""Smoke tests for src/db.py — schema, upsert, update, load."""
import hashlib
import sqlite3

import pandas as pd
import pytest

from src.db import (
    _CURRENT_VERSION, _MIGRATIONS, _apply_migrations, _get_schema_version,
    get_db, init_db, load_transactions, run_data_quality_checks,
    update_transaction, upsert_transactions,
)
from src.utils import is_valid_subcat


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mem_config(tmp_path):
    """Config pointing at a temp-dir DB (avoids polluting real Data/)."""
    return {"data": {"database": str(tmp_path / "test.db")}}


def _make_df(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "txn_id": f"txn{i:04d}",
            "date": pd.Timestamp("2025-09-01"),
            "amount": -100.0 * i,
            "description": f"MERCHANT {i}",   # ALL CAPS — migration is a no-op
            "category": "Groceries",
            "account": "ANZ Personal",
            "account_type": "personal",
            "source_file": "test.csv",
            "is_business": False,
            "is_tax_deductible": False,
            "is_gst_claimable": False,
        }
        for i in range(1, n + 1)
    ])


# ── Schema version / migration system ────────────────────────────────────────

def test_current_version_matches_migration_registry():
    """_CURRENT_VERSION must equal the highest version number in _MIGRATIONS."""
    assert _MIGRATIONS[-1][0] == _CURRENT_VERSION
    assert len(_MIGRATIONS) == _CURRENT_VERSION


def test_schema_version_zero_on_fresh_connection(tmp_path):
    """A brand-new SQLite file has user_version = 0 before init_db."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    assert _get_schema_version(conn) == 0
    conn.close()


def test_fresh_init_db_sets_current_version(tmp_path):
    """After init_db on a new DB the version equals _CURRENT_VERSION."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    assert _get_schema_version(conn) == _CURRENT_VERSION
    conn.close()


def test_migrations_applied_to_old_schema(tmp_path):
    """Simulate a pre-migration DB — only is_tax_deductible and is_gst_claimable are absent."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    # Realistic original schema: all base columns present but without the two added columns
    conn.execute(
        "CREATE TABLE transactions ("
        "txn_id TEXT PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL, "
        "description TEXT, payee_name TEXT, reference TEXT, note TEXT, "
        "account TEXT, account_type TEXT, bank TEXT, bsb TEXT, account_number TEXT, "
        "category TEXT, sub_category TEXT, is_business INTEGER DEFAULT 0, "
        "user_note TEXT, source_file TEXT, zip_source TEXT)"
    )
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "is_tax_deductible" not in cols_before
    assert "is_gst_claimable" not in cols_before

    init_db(conn)

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "is_tax_deductible" in cols_after
    assert "is_gst_claimable" in cols_after
    assert _get_schema_version(conn) == _CURRENT_VERSION
    conn.close()


def test_migrations_not_rerun_after_init(tmp_path):
    """Calling init_db twice keeps the same schema version — migrations don't re-run."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    v1 = _get_schema_version(conn)
    init_db(conn)
    v2 = _get_schema_version(conn)
    assert v1 == v2 == _CURRENT_VERSION
    conn.close()


def test_apply_migrations_is_no_op_on_current_db(tmp_path):
    """_apply_migrations on an already-current DB leaves version unchanged."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    _apply_migrations(conn)
    assert _get_schema_version(conn) == _CURRENT_VERSION
    conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def test_init_db_creates_tables(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "transactions" in tables
    assert "balance_snapshots" in tables
    assert "override_history" in tables
    conn.close()


def test_schema_has_gst_column(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "is_gst_claimable" in cols
    assert "is_tax_deductible" in cols
    assert "is_business" in cols
    conn.close()


def test_init_db_idempotent(tmp_path):
    """Calling init_db twice must not raise."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    init_db(conn)
    conn.close()


# ── Upsert / load round-trip ──────────────────────────────────────────────────

def test_upsert_and_load_round_trip(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    df = _make_df(3)
    count = upsert_transactions(df, cfg, zip_name=None)
    assert count == 3

    loaded = load_transactions(cfg)
    assert len(loaded) == 3
    assert set(loaded["txn_id"]) == {"txn0001", "txn0002", "txn0003"}


def test_upsert_deduplicates(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    df = _make_df(2)
    upsert_transactions(df, cfg, zip_name=None)
    count2 = upsert_transactions(df, cfg, zip_name=None)
    assert count2 == 0  # INSERT OR IGNORE — duplicates skipped

    loaded = load_transactions(cfg)
    assert len(loaded) == 2


def test_load_transactions_returns_bool_flags(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    loaded = load_transactions(cfg)
    assert loaded["is_business"].dtype == bool
    assert loaded["is_tax_deductible"].dtype == bool
    assert loaded["is_gst_claimable"].dtype == bool


# ── update_transaction ────────────────────────────────────────────────────────

def test_update_transaction_category(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    ok = update_transaction("txn0001", {"category": "Transport"}, cfg)
    assert ok is True

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "category"].iloc[0] == "Transport"


def test_update_transaction_gst_flag(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    ok = update_transaction("txn0001", {"is_gst_claimable": True}, cfg)
    assert ok is True

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "is_gst_claimable"].iloc[0] == True


def test_update_transaction_tax_flag(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    update_transaction("txn0001", {"is_tax_deductible": True}, cfg)
    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "is_tax_deductible"].iloc[0] == True


def test_update_transaction_ignores_disallowed_fields(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    ok = update_transaction("txn0001", {"txn_id": "hacked", "amount": 999.0}, cfg)
    assert ok is False  # safe-list rejected all fields

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "amount"].iloc[0] == -100.0


def test_update_transaction_missing_txn_id(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    ok = update_transaction("nonexistent", {"category": "Shopping"}, cfg)
    assert ok is False


# ── _clean_invalid_subcategories (called from init_db) ───────────────────────

def _make_df_with_sub(txn_id, category, sub_category):
    return pd.DataFrame([{
        "txn_id": txn_id,
        "date": pd.Timestamp("2025-09-01"),
        "amount": -10.0,
        "description": f"TEST {txn_id.upper()}",   # unique + ALL CAPS — migration is a no-op
        "category": category,
        "sub_category": sub_category,
        "account": "ANZ Personal",
        "account_type": "transaction",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])


def test_init_db_clears_invalid_subcat(tmp_path):
    """init_db must clear sub_categories that don't belong to their category."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    # Seed a row with a clearly wrong sub_category (Fuel under Gifts Given)
    upsert_transactions(_make_df_with_sub("txn-bad", "Gifts Given", "Fuel"), cfg, zip_name=None)
    conn = get_db(cfg)
    init_db(conn)
    run_data_quality_checks(conn)
    conn.close()

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn-bad", "sub_category"].iloc[0] == ""


def test_init_db_keeps_valid_subcat(tmp_path):
    """init_db must not clear valid sub_categories."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df_with_sub("txn-ok", "Transport", "Fuel"), cfg, zip_name=None)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn-ok", "sub_category"].iloc[0] == "Fuel"


def test_init_db_keeps_system_subcats(tmp_path):
    """Reversal, Refund, and Pay-in-4 are preserved even though they're not in SUBCATS."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    for tid, sub in [("rev", "Reversal"), ("ref", "Refund"), ("p4", "Pay-in-4")]:
        upsert_transactions(_make_df_with_sub(tid, "Transfers", sub), cfg, zip_name=None)

    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    loaded = load_transactions(cfg)
    for tid, sub in [("rev", "Reversal"), ("ref", "Refund"), ("p4", "Pay-in-4")]:
        assert loaded.loc[loaded["txn_id"] == tid, "sub_category"].iloc[0] == sub


# ── is_valid_subcat ───────────────────────────────────────────────────────────

def test_is_valid_subcat_empty_always_valid():
    assert is_valid_subcat("Transfers", "") is True
    assert is_valid_subcat("Anything", "") is True


def test_is_valid_subcat_system_values_always_valid():
    assert is_valid_subcat("Transfers", "Reversal") is True
    assert is_valid_subcat("Gifts Given", "Refund") is True
    assert is_valid_subcat("Housing", "Pay-in-4") is True


def test_is_valid_subcat_correct_pairs():
    assert is_valid_subcat("Transport", "Fuel") is True
    assert is_valid_subcat("Groceries", "Supermarket") is True
    assert is_valid_subcat("Transfers", "Internal Transfer") is True


def test_is_valid_subcat_wrong_pairs():
    assert is_valid_subcat("Gifts Given", "Fuel") is False
    assert is_valid_subcat("Transfers", "Fuel") is False
    assert is_valid_subcat("Family Loan Repayment", "Fuel") is False
    assert is_valid_subcat("Gifts Given", "Gifts") is False
    assert is_valid_subcat("Transfers", "Credit Card Payment") is False


def test_is_valid_subcat_unknown_category():
    """Categories not in SUBCATS reject any non-system sub_category."""
    assert is_valid_subcat("Family Loan Repayment", "Anything") is False
    assert is_valid_subcat("Family Loan Repayment", "Reversal") is True  # system


# ── _migrate_txn_ids_to_uppercase ─────────────────────────────────────────────

def _old_txn_id(date_str: str, amount: float, description: str, account: str) -> str:
    """Old (pre-migration) hash: case-sensitive description."""
    key = f"{date_str}|{amount:.2f}|{description[:50]}|{account}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _new_txn_id(date_str: str, amount: float, description: str, account: str) -> str:
    """New (post-migration) hash: uppercase-normalised description."""
    key = f"{date_str}|{amount:.2f}|{description.upper()[:50]}|{account}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _insert_raw(conn, txn_id, date, description, amount, account="PayPal"):
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, description, account, account_type, source_file) "
        "VALUES (?, ?, ?, ?, ?, 'paypal', 'test.csv')",
        (txn_id, date, amount, description, account),
    )
    conn.commit()


def test_migration_normalises_mixed_case_txn_id(tmp_path):
    """Mixed-case description → txn_id updated to uppercase-based hash."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)  # create schema

    old_id = _old_txn_id("2024-01-01", -10.0, "Netflix", "PayPal")
    _insert_raw(conn, old_id, "2024-01-01", "Netflix", -10.0)

    run_data_quality_checks(conn)

    new_id = _new_txn_id("2024-01-01", -10.0, "Netflix", "PayPal")
    rows = conn.execute("SELECT txn_id FROM transactions WHERE description = 'Netflix'").fetchall()
    assert len(rows) == 1
    assert rows[0]["txn_id"] == new_id
    assert rows[0]["txn_id"] != old_id
    conn.close()


def test_migration_leaves_uppercase_rows_unchanged(tmp_path):
    """Rows with already-uppercase descriptions are not modified."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)

    # ANZ-style description: already ALL CAPS
    orig_id = _old_txn_id("2024-02-01", -50.0, "WOOLWORTHS 1234", "ANZ Personal")
    _insert_raw(conn, orig_id, "2024-02-01", "WOOLWORTHS 1234", -50.0, "ANZ Personal")

    run_data_quality_checks(conn)

    rows = conn.execute("SELECT txn_id FROM transactions WHERE description = 'WOOLWORTHS 1234'").fetchall()
    assert rows[0]["txn_id"] == orig_id  # unchanged
    conn.close()


def test_migration_removes_case_sensitivity_duplicate(tmp_path):
    """Two rows for the same transaction with different capitalization → one survives."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)

    # Simulate overlapping import: "Netflix" and "NETFLIX" for the same transaction
    old_id  = _old_txn_id("2024-01-01", -10.0, "Netflix", "PayPal")
    new_id  = _new_txn_id("2024-01-01", -10.0, "Netflix", "PayPal")
    # new_id == _old_txn_id("2024-01-01", -10.0, "NETFLIX", "PayPal") since UPPER("NETFLIX") = "NETFLIX"
    # Insert both: old lowercase-hash row and already-correct uppercase-hash row
    _insert_raw(conn, old_id, "2024-01-01", "Netflix", -10.0)
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, description, account, account_type, source_file) "
        "VALUES (?, '2024-01-01', -10.0, 'NETFLIX', 'PayPal', 'paypal', 'test2.csv')",
        (new_id,),
    )
    conn.commit()

    run_data_quality_checks(conn)  # removes the old_id row

    count = conn.execute("SELECT COUNT(*) FROM transactions WHERE account = 'PayPal'").fetchone()[0]
    assert count == 1
    conn.close()


def test_migration_idempotent(tmp_path):
    """Running init_db a second time after migration makes no further changes."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)

    old_id = _old_txn_id("2024-03-01", -15.0, "Spotify AB", "Revolut")
    _insert_raw(conn, old_id, "2024-03-01", "Spotify AB", -15.0, "Revolut")

    run_data_quality_checks(conn)  # first run — normalises txn_id
    new_id = _new_txn_id("2024-03-01", -15.0, "Spotify AB", "Revolut")
    after_first = conn.execute("SELECT txn_id FROM transactions").fetchone()["txn_id"]
    assert after_first == new_id

    run_data_quality_checks(conn)  # second run — should be a no-op
    after_second = conn.execute("SELECT txn_id FROM transactions").fetchone()["txn_id"]
    assert after_second == new_id  # unchanged
    conn.close()


# ── Tags field ────────────────────────────────────────────────────────────────

def test_migration_003_adds_tags_column(tmp_path):
    """Migration 003 should add a 'tags' column to existing DBs."""
    from src.db import _migration_003_add_tags
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    # Use a fully-migrated DB (version 2) then manually drop 'tags' to simulate pre-v3
    init_db(conn)
    try:
        conn.execute("ALTER TABLE transactions RENAME TO _txn_bak")
        conn.execute("""CREATE TABLE transactions AS
            SELECT txn_id,date,amount,description,payee_name,reference,note,account,
                   account_type,bank,bsb,account_number,category,sub_category,
                   is_business,is_tax_deductible,is_gst_claimable,user_note,
                   source_file,zip_source
            FROM _txn_bak""")
        conn.execute("DROP TABLE _txn_bak")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    except Exception:
        pass
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    assert "tags" not in cols_before
    _migration_003_add_tags(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    assert "tags" in cols
    conn.close()


def test_fresh_db_has_tags_column(tmp_path):
    """A brand-new DB created by init_db should have a tags column."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    assert "tags" in cols
    conn.close()


def test_update_transaction_tags(tmp_path):
    """update_transaction should allow writing to the tags field."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    ok = update_transaction("txn0001", {"tags": "Bali 2025,Tax Deductible"}, cfg)
    assert ok is True

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "tags"].iloc[0] == "Bali 2025,Tax Deductible"


def test_update_transaction_tags_clear(tmp_path):
    """Setting tags to empty string clears any existing tags."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(1), cfg, zip_name=None)
    update_transaction("txn0001", {"tags": "SomeTag"}, cfg)
    update_transaction("txn0001", {"tags": ""}, cfg)

    loaded = load_transactions(cfg)
    val = loaded.loc[loaded["txn_id"] == "txn0001", "tags"].iloc[0]
    assert val == "" or pd.isna(val)


def test_update_transaction_tags_not_in_bulk_allowed(tmp_path):
    """Tags field is not in update_transactions_bulk _ALLOWED (bulk only touches category etc.)."""
    from src.db import update_transactions_bulk
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    upsert_transactions(_make_df(2), cfg, zip_name=None)
    count = update_transactions_bulk(["txn0001", "txn0002"], {"tags": "bulk-tag"}, cfg)
    # tags is NOT in bulk _ALLOWED, so 0 rows should be updated
    assert count == 0


# ── Transaction splitting ─────────────────────────────────────────────────────

def test_migration_004_adds_split_columns(tmp_path):
    """Migration 004 adds parent_txn_id and is_split_parent columns."""
    from src.db import _migration_004_add_split_columns
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    # Create a minimal schema without the split columns
    conn.execute("""
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY, date TEXT, amount REAL, description TEXT,
            account TEXT, category TEXT, sub_category TEXT, is_business INTEGER DEFAULT 0,
            is_tax_deductible INTEGER DEFAULT 0, is_gst_claimable INTEGER DEFAULT 0,
            user_note TEXT, tags TEXT DEFAULT '', source_file TEXT, zip_source TEXT
        )
    """)
    _migration_004_add_split_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "parent_txn_id" in cols
    assert "is_split_parent" in cols
    conn.close()


def test_fresh_db_has_split_columns(tmp_path):
    """A freshly initialised DB has both split columns."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "parent_txn_id" in cols
    assert "is_split_parent" in cols
    conn.close()


def test_load_transactions_excludes_split_parents_by_default(tmp_path):
    """load_transactions() default excludes split parents; children are included."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    # Insert parent row marked as split
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account, is_split_parent) "
        "VALUES ('PARENT1','2026-05-01',-100.0,'Groceries','ANZ',1)"
    )
    # Insert two children
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account, parent_txn_id) "
        "VALUES ('PARENT1_S1','2026-05-01',-60.0,'Groceries','ANZ','PARENT1')"
    )
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account, parent_txn_id) "
        "VALUES ('PARENT1_S2','2026-05-01',-40.0,'Utilities','ANZ','PARENT1')"
    )
    # Insert a regular (unsplit) transaction
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account) "
        "VALUES ('REG1','2026-05-02',-50.0,'Dining Out','ANZ')"
    )
    conn.commit()
    conn.close()

    df = load_transactions(cfg)  # default: exclude split parents
    txn_ids = set(df["txn_id"].tolist())
    assert "PARENT1" not in txn_ids        # parent excluded
    assert "PARENT1_S1" in txn_ids         # child included
    assert "PARENT1_S2" in txn_ids         # child included
    assert "REG1" in txn_ids               # regular included


def test_load_transactions_includes_split_parents_when_requested(tmp_path):
    """load_transactions(include_split_parents=True) returns all rows."""
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account, is_split_parent) "
        "VALUES ('PAR2','2026-05-01',-200.0,'Groceries','ANZ',1)"
    )
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, category, account, parent_txn_id) "
        "VALUES ('PAR2_S1','2026-05-01',-200.0,'Groceries','ANZ','PAR2')"
    )
    conn.commit()
    conn.close()

    df = load_transactions(cfg, include_split_parents=True)
    txn_ids = set(df["txn_id"].tolist())
    assert "PAR2" in txn_ids
    assert "PAR2_S1" in txn_ids


def test_current_version_is_eight():
    assert _CURRENT_VERSION == 8


def test_migration_registry_has_eight_entries():
    assert len(_MIGRATIONS) == 8
    versions = [v for v, _ in _MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]


# ── load_transactions date-range filter ───────────────────────────────────────

def _seed_dated_rows(tmp_path):
    cfg = _mem_config(tmp_path)
    conn = get_db(cfg)
    init_db(conn)
    rows = [
        ("T1", "2024-06-30", -10.0, "Groceries"),
        ("T2", "2025-01-15", -20.0, "Dining Out"),
        ("T3", "2025-07-01", -30.0, "Groceries"),
        ("T4", "2026-06-30", -40.0, "Dining Out"),
    ]
    for txn_id, date, amount, category in rows:
        conn.execute(
            "INSERT INTO transactions (txn_id, date, amount, category, account, is_split_parent) "
            "VALUES (?, ?, ?, ?, 'ANZ', 0)",
            (txn_id, date, amount, category),
        )
    conn.commit()
    conn.close()
    return cfg


def test_load_transactions_since_filters_before(tmp_path):
    cfg = _seed_dated_rows(tmp_path)
    df = load_transactions(cfg, since="2025-01-01")
    ids = set(df["txn_id"].tolist())
    assert "T1" not in ids
    assert "T2" in ids
    assert "T3" in ids
    assert "T4" in ids


def test_load_transactions_until_filters_after(tmp_path):
    cfg = _seed_dated_rows(tmp_path)
    df = load_transactions(cfg, until="2025-12-31")
    ids = set(df["txn_id"].tolist())
    assert "T1" in ids
    assert "T2" in ids
    assert "T3" in ids    # 2025-07-01 is before the cutoff
    assert "T4" not in ids


def test_load_transactions_since_and_until_fy_window(tmp_path):
    cfg = _seed_dated_rows(tmp_path)
    df = load_transactions(cfg, since="2025-07-01", until="2026-06-30")
    ids = set(df["txn_id"].tolist())
    assert ids == {"T3", "T4"}


def test_load_transactions_no_filter_returns_all(tmp_path):
    cfg = _seed_dated_rows(tmp_path)
    df = load_transactions(cfg)
    assert len(df) == 4
