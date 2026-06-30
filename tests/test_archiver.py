"""
Tests for src/archiver.py — deduplication and zip creation.

Run with:  python -m pytest tests/ -v
"""

import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.archiver import archive_and_update_master, _zip_name
from src.db import load_transactions as load_master_csv


# ── Config helper ─────────────────────────────────────────────────────────────

def _config(tmp_path: Path) -> dict:
    raw_dir = tmp_path / "Raw Data"
    raw_dir.mkdir()
    archive_dir = tmp_path / "Archive"
    db_file = tmp_path / "finance.db"
    return {
        "data": {
            "input_dir": str(raw_dir),
            "archive_dir": str(archive_dir),
            "database": str(db_file),
        },
        "accounts": {},
    }


def _sample_df(txn_ids: list[str]) -> pd.DataFrame:
    rows = []
    for i, tid in enumerate(txn_ids):
        rows.append({
            "txn_id": tid,
            "date": pd.Timestamp("2025-03-01"),
            "amount": -float(i + 1) * 10,
            "description": f"MERCHANT {i}",
            "payee_name": "",
            "reference": "",
            "note": "",
            "account": "ANZ Personal",
            "account_type": "transaction",
            "bank": "",
            "bsb": "",
            "account_number": "",
            "category": "Groceries",
            "is_business": False,
            "source_file": "test.csv",
        })
    return pd.DataFrame(rows)


# ── Zip creation ──────────────────────────────────────────────────────────────

def test_zip_created_with_raw_files(tmp_path):
    config = _config(tmp_path)
    raw_dir = Path(config["data"]["input_dir"])
    (raw_dir / "test_statement.csv").write_text("01/03/2025,-10.00,WOOLWORTHS,,,,\n")

    df = _sample_df(["txn001"])
    zip_name = archive_and_update_master(df, config)

    assert zip_name != ""
    zip_path = Path(config["data"]["archive_dir"]) / zip_name
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert "test_statement.csv" in zf.namelist()


def test_zip_no_raw_files(tmp_path):
    config = _config(tmp_path)
    df = _sample_df(["txn001"])
    result = archive_and_update_master(df, config)
    assert result == ""


def test_raw_files_deleted_after_archive(tmp_path):
    config = _config(tmp_path)
    raw_dir = Path(config["data"]["input_dir"])
    f = raw_dir / "statement.csv"
    f.write_text("01/03/2025,-10.00,TEST,,,,\n")

    archive_and_update_master(_sample_df(["txn001"]), config)
    assert not f.exists()


# ── SQLite deduplication ──────────────────────────────────────────────────────

def test_master_created_on_first_run(tmp_path):
    config = _config(tmp_path)
    raw_dir = Path(config["data"]["input_dir"])
    (raw_dir / "s.csv").write_text("x")

    archive_and_update_master(_sample_df(["txn001", "txn002"]), config)

    result = load_master_csv(config)
    assert len(result) == 2
    assert set(result["txn_id"]) == {"txn001", "txn002"}


def test_no_duplicate_on_second_run(tmp_path):
    config = _config(tmp_path)
    raw_dir = Path(config["data"]["input_dir"])

    (raw_dir / "s1.csv").write_text("x")
    archive_and_update_master(_sample_df(["txn001", "txn002"]), config)

    (raw_dir / "s2.csv").write_text("x")
    archive_and_update_master(_sample_df(["txn002", "txn003"]), config)

    result = load_master_csv(config)
    assert len(result) == 3
    assert result["txn_id"].nunique() == 3


def test_all_new_on_second_run(tmp_path):
    config = _config(tmp_path)
    raw_dir = Path(config["data"]["input_dir"])

    (raw_dir / "s1.csv").write_text("x")
    archive_and_update_master(_sample_df(["txn001"]), config)

    (raw_dir / "s2.csv").write_text("x")
    archive_and_update_master(_sample_df(["txn002"]), config)

    result = load_master_csv(config)
    assert len(result) == 2


# ── Zip naming collision ──────────────────────────────────────────────────────

def test_zip_name_no_collision(tmp_path):
    archive_dir = tmp_path / "Archive"
    archive_dir.mkdir()
    name1 = _zip_name(archive_dir)
    (archive_dir / name1).write_text("x")
    name2 = _zip_name(archive_dir)
    assert name1 != name2
