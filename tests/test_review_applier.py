"""Tests for src/review_applier.py — apply_entries and save_note."""
import json
import pytest
import pandas as pd

from src.db import get_db, init_db, upsert_transactions, load_transactions
from src.review_applier import apply_entries, save_note


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(tmp_path):
    return {
        "data": {
            "database":            str(tmp_path / "test.db"),
            "overrides_file":      str(tmp_path / "overrides.json"),
            "cache_file":          str(tmp_path / "cache.json"),
            "merchant_rules_file": str(tmp_path / "rules.json"),
        }
    }


def _seed(cfg, txn_id="txn0001", category="Miscellaneous", description="KMART"):
    df = pd.DataFrame([{
        "txn_id":       txn_id,
        "date":         pd.Timestamp("2025-09-15"),
        "amount":       -45.00,
        "description":  description,
        "category":     category,
        "account":      "ANZ Personal",
        "account_type": "transaction",
        "source_file":  "test.csv",
        "is_business":  False,
        "is_tax_deductible": False,
        "is_gst_claimable":  False,
    }])
    upsert_transactions(df, cfg, zip_name=None)


# ── apply_entries — category update ──────────────────────────────────────────

def test_apply_entries_updates_category_in_db(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg, "txn0001", category="Miscellaneous")

    result = apply_entries([{
        "txn_id": "txn0001", "category": "Groceries",
        "description": "KMART", "amount": -45.00,
    }], cfg)

    assert result["txn_updated"] == 1
    assert result["master_updated"] >= 1
    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "category"].iloc[0] == "Groceries"


def test_apply_entries_writes_overrides_json(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg)

    apply_entries([{"txn_id": "txn0001", "category": "Shopping",
                    "description": "KMART", "amount": -45.00}], cfg)

    overrides = json.loads((tmp_path / "overrides.json").read_text("utf-8"))
    assert "txn0001" in overrides
    assert overrides["txn0001"]["category"] == "Shopping"


def test_apply_entries_apply_to_all_updates_cache(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg, "txn0001", description="KMART")

    result = apply_entries([{
        "txn_id": "txn0001", "category": "Shopping",
        "description": "KMART", "amount": -45.00,
        "apply_to_all": True,
    }], cfg)

    assert result["cache_updated"] == 1
    cache = json.loads((tmp_path / "cache.json").read_text("utf-8"))
    assert any("KMART" in k for k in cache)


def test_apply_entries_apply_to_all_writes_merchant_rules(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg, "txn0001", description="KMART")

    apply_entries([{
        "txn_id": "txn0001", "category": "Shopping",
        "description": "KMART", "amount": -45.00,
        "apply_to_all": True,
    }], cfg)

    rules = json.loads((tmp_path / "rules.json").read_text("utf-8"))
    assert "KMART" in rules
    assert rules["KMART"] == "Shopping"


def test_apply_entries_apply_to_all_updates_other_matching_transactions(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg, "txn0001", description="KMART")
    _seed(cfg, "txn0002", description="KMART")  # same merchant, different txn

    apply_entries([{
        "txn_id": "txn0001", "category": "Shopping",
        "description": "KMART", "amount": -45.00,
        "apply_to_all": True,
    }], cfg)

    loaded = load_transactions(cfg)
    for _, row in loaded.iterrows():
        assert row["category"] == "Shopping"


def test_apply_entries_empty_list_returns_zeros(tmp_path):
    cfg = _cfg(tmp_path)
    result = apply_entries([], cfg)
    assert result == {"txn_updated": 0, "cache_updated": 0, "master_updated": 0}


def test_apply_entries_skips_missing_txn_id(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    result = apply_entries([{"category": "Shopping"}], cfg)  # no txn_id
    assert result["txn_updated"] == 0


# ── save_note ─────────────────────────────────────────────────────────────────

def test_save_note_persists_in_db(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg)

    result = save_note("txn0001", "Checked receipt — correct", cfg)
    assert result["master_updated"] == 1

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "user_note"].iloc[0] == "Checked receipt — correct"


def test_save_note_writes_overrides_json(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg)

    save_note("txn0001", "My note", cfg)

    overrides = json.loads((tmp_path / "overrides.json").read_text("utf-8"))
    assert overrides["txn0001"]["note"] == "My note"


def test_save_note_clears_note_when_empty(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed(cfg)

    save_note("txn0001", "First note", cfg)
    save_note("txn0001", "", cfg)  # clear it

    loaded = load_transactions(cfg)
    assert loaded.loc[loaded["txn_id"] == "txn0001", "user_note"].iloc[0] == ""


def test_save_note_nonexistent_txn_returns_zero(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()

    result = save_note("nonexistent", "Some note", cfg)
    assert result["master_updated"] == 0
