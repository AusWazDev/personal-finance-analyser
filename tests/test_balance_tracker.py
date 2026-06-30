"""Tests for src/balance_tracker.py — merge_snapshots and DB persistence."""
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from src.balance_tracker import (
    merge_snapshots, load_balance_history, save_balance_history,
    extract_anz_plus_balance, extract_revolut_balance,
)
from src.db import get_db, init_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snap(date, account, balance, account_type="transaction"):
    return {"date": date, "account": account, "balance": balance, "account_type": account_type, "source_file": "test.csv"}


def _snap_df(*snaps):
    df = pd.DataFrame(snaps)
    df["date"] = pd.to_datetime(df["date"])
    df["balance"] = df["balance"].astype(float)
    return df


# ── merge_snapshots ───────────────────────────────────────────────────────────

def test_merge_adds_new_snapshots():
    existing = _snap_df(_snap("2025-09-30", "ANZ Personal", 1000.0))
    new = [_snap("2025-10-31", "ANZ Personal", 1200.0)]
    result = merge_snapshots(existing, new)
    assert len(result) == 2


def test_merge_deduplicates_by_date_and_account_new_wins():
    existing = _snap_df(_snap("2025-09-30", "ANZ Personal", 1000.0))
    new = [_snap("2025-09-30", "ANZ Personal", 1500.0)]  # same key, new value
    result = merge_snapshots(existing, new)
    assert len(result) == 1
    assert result.iloc[0]["balance"] == 1500.0


def test_merge_different_accounts_not_deduplicated():
    existing = _snap_df(_snap("2025-09-30", "ANZ Personal", 1000.0))
    new = [_snap("2025-09-30", "ANZ Savings", 5000.0)]  # same date, different account
    result = merge_snapshots(existing, new)
    assert len(result) == 2


def test_merge_with_empty_new_returns_existing():
    existing = _snap_df(_snap("2025-09-30", "ANZ Personal", 1000.0))
    result = merge_snapshots(existing, [])
    assert len(result) == 1


def test_merge_result_sorted_by_account_then_date():
    existing = _snap_df(
        _snap("2025-10-31", "ANZ Personal", 1200.0),
        _snap("2025-09-30", "ANZ Personal", 1000.0),
    )
    new = [_snap("2025-08-31", "ANZ Personal", 900.0)]
    result = merge_snapshots(existing, new)
    dates = result[result["account"] == "ANZ Personal"]["date"].tolist()
    assert dates == sorted(dates)


# ── load_balance_history / save_balance_history (DB round-trip) ───────────────

def test_save_and_load_round_trip(tmp_path):
    cfg = {"data": {"database": str(tmp_path / "test.db")}}
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    snapshots = [
        _snap("2025-09-30", "ANZ Personal", 1000.0),
        _snap("2025-09-30", "ANZ Savings", 5000.0),
    ]
    save_balance_history(snapshots, cfg)
    loaded = load_balance_history(cfg)

    assert len(loaded) == 2
    assert set(loaded["account"]) == {"ANZ Personal", "ANZ Savings"}


def test_load_returns_empty_df_when_db_absent(tmp_path):
    cfg = {"data": {"database": str(tmp_path / "nonexistent.db")}}
    result = load_balance_history(cfg)
    assert result.empty or len(result) == 0


def test_save_deduplicates_on_date_account(tmp_path):
    cfg = {"data": {"database": str(tmp_path / "test.db")}}
    conn = get_db(cfg)
    init_db(conn)
    conn.close()

    snap = _snap("2025-09-30", "ANZ Personal", 1000.0)
    save_balance_history([snap], cfg)
    snap_updated = _snap("2025-09-30", "ANZ Personal", 1500.0)
    save_balance_history([snap_updated], cfg)

    loaded = load_balance_history(cfg)
    anz = loaded[loaded["account"] == "ANZ Personal"]
    assert len(anz) == 1  # upsert — not duplicated
    assert float(anz.iloc[0]["balance"]) == 1500.0


# ── Extractors (mocked) ───────────────────────────────────────────────────────

def test_extract_anz_plus_balance():
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "\n".join([
        "1 January 2025 - 31 January 2025",
        "15 Jan Some Purchase $100.00 $2,345.67",
    ])
    mock_pdf = MagicMock()
    mock_pdf.pages = [mock_page]
    mock_pdf.__enter__ = lambda s: mock_pdf
    mock_pdf.__exit__ = MagicMock(return_value=False)

    with patch("pdfplumber.open", return_value=mock_pdf):
        result = extract_anz_plus_balance("fake.pdf", "ANZ Plus Everyday")

    assert result is not None
    assert result["balance"] == 2345.67
    assert result["date"] == "2025-01-31"
    assert result["account"] == "ANZ Plus Everyday"
    assert result["source_file"] == "fake.pdf"


def test_extract_revolut_balance(tmp_path):
    csv_file = tmp_path / "revolut.csv"
    csv_file.write_text(
        "Completed Date,Balance,State\n"
        "2025-01-15,1234.56,COMPLETED\n"
        "2025-01-20,1500.00,COMPLETED\n"
        "2025-01-22,999.00,PENDING\n",
        encoding="utf-8",
    )

    result = extract_revolut_balance(str(csv_file), "Revolut")

    assert result is not None
    assert result["balance"] == 1500.00  # last COMPLETED row
    assert result["date"] == "2025-01-20"
    assert result["account"] == "Revolut"
    assert result["account_type"] == "revolut"
