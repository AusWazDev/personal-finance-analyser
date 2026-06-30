"""Tests for src/override_history.py — append, retrieve, undo, and cache cleanup."""
import json
import pytest

from src.db import get_db, init_db, upsert_transactions
from src.override_history import append_batch, get_history, undo_batch

import pandas as pd


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(tmp_path):
    return {"data": {"database": str(tmp_path / "test.db")}}


def _sample_batch(batch_id="batch001", txn_id="txn0001", old_cat="Groceries", new_cat="Transport"):
    return {
        "batch_id": batch_id,
        "applied_at": "2025-10-01T10:00:00",
        "action": "category_override",
        "summary": f"Changed 1 transaction → {new_cat}",
        "changes": [{
            "txn_id": txn_id,
            "description": "Fuel",
            "field": "category",
            "old_value": old_cat,
            "new_value": new_cat,
            "apply_to_all": False,
            "cache_key": None,
            "merchant_key": None,
        }],
    }


def _seed_txn(cfg, txn_id="txn0001", category="Groceries"):
    """Insert a single transaction into the test DB."""
    df = pd.DataFrame([{
        "txn_id": txn_id,
        "date": pd.Timestamp("2025-09-15"),
        "amount": -50.0,
        "description": "TEST MERCHANT",
        "category": category,
        "account": "ANZ Personal",
        "account_type": "transaction",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])
    upsert_transactions(df, cfg, zip_name=None)


# ── append_batch / get_history ────────────────────────────────────────────────

def test_append_and_retrieve_batch(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()

    append_batch(_sample_batch("b001"), cfg)
    history = get_history(cfg, limit=10)

    assert len(history) == 1
    assert history[0]["batch_id"] == "b001"
    assert "Transport" in history[0]["summary"]


def test_get_history_returns_most_recent_first(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()

    append_batch(_sample_batch("b001"), cfg)
    append_batch(_sample_batch("b002", new_cat="Dining Out"), cfg)
    history = get_history(cfg, limit=10)

    assert history[0]["batch_id"] == "b002"
    assert history[1]["batch_id"] == "b001"


def test_get_history_respects_limit(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()

    for i in range(5):
        append_batch(_sample_batch(f"b{i:03d}", new_cat=f"Cat{i}"), cfg)

    history = get_history(cfg, limit=3)
    assert len(history) == 3


def test_get_history_empty_when_no_batches(tmp_path):
    cfg = _cfg(tmp_path)
    assert get_history(cfg, limit=10) == []


def test_append_batch_idempotent_on_duplicate_id(tmp_path):
    """INSERT OR IGNORE — same batch_id twice must not raise or duplicate."""
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()

    append_batch(_sample_batch("b001"), cfg)
    append_batch(_sample_batch("b001"), cfg)  # duplicate
    history = get_history(cfg, limit=10)
    assert len(history) == 1


# ── undo_batch ────────────────────────────────────────────────────────────────

def test_undo_restores_category_in_db(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed_txn(cfg, "txn0001", category="Groceries")

    # Record that we changed it to Transport
    conn = get_db(cfg)
    conn.execute("UPDATE transactions SET category = ? WHERE txn_id = ?", ("Transport", "txn0001"))
    conn.commit(); conn.close()

    append_batch(_sample_batch("b001", old_cat="Groceries", new_cat="Transport"), cfg)
    result = undo_batch("b001", cfg)

    assert result["ok"] is True
    assert result["undone"] == 1

    conn = get_db(cfg)
    row = conn.execute("SELECT category FROM transactions WHERE txn_id = 'txn0001'").fetchone()
    conn.close()
    assert row["category"] == "Groceries"


def test_undo_returns_error_for_unknown_batch(tmp_path):
    cfg = _cfg(tmp_path)
    result = undo_batch("nonexistent_batch", cfg)
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_undo_batch_marks_as_undone_so_hidden_from_history(tmp_path):
    cfg = _cfg(tmp_path)
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed_txn(cfg)
    append_batch(_sample_batch("b001"), cfg)
    undo_batch("b001", cfg)
    history = get_history(cfg, limit=10)
    assert all(h["batch_id"] != "b001" for h in history)


# ── cache / merchant_rules cleanup ────────────────────────────────────────────

def test_undo_removes_entry_from_cache_file(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["data"]["cache_file"] = str(tmp_path / "cache.json")
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed_txn(cfg)

    cache_key = "FUEL|dr"
    (tmp_path / "cache.json").write_text(
        json.dumps({cache_key: {"category": "Transport", "business": False}}), "utf-8"
    )

    batch = _sample_batch("b001")
    batch["changes"][0]["apply_to_all"] = True
    batch["changes"][0]["cache_key"] = cache_key
    append_batch(batch, cfg)
    undo_batch("b001", cfg)

    remaining = json.loads((tmp_path / "cache.json").read_text("utf-8"))
    assert cache_key not in remaining


def test_undo_removes_entry_from_merchant_rules(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["data"]["merchant_rules_file"] = str(tmp_path / "rules.json")
    conn = get_db(cfg); init_db(conn); conn.close()
    _seed_txn(cfg)

    merchant_key = "FUEL"
    (tmp_path / "rules.json").write_text(
        json.dumps({merchant_key: "Transport"}), "utf-8"
    )

    batch = _sample_batch("b001")
    batch["changes"][0]["apply_to_all"] = True
    batch["changes"][0]["merchant_key"] = merchant_key
    append_batch(batch, cfg)
    undo_batch("b001", cfg)

    remaining = json.loads((tmp_path / "rules.json").read_text("utf-8"))
    assert merchant_key not in remaining
