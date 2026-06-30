"""Tests for src/bank_profiles.py and parse_generic_csv in src/parsers.py."""
import json
from pathlib import Path

import pandas as pd
import pytest

from src.bank_profiles import (
    delete_profile,
    find_profile,
    headers_key,
    load_profiles,
    save_profile,
    save_profiles,
)
from src.parsers import _detect_file_type, parse_generic_csv


def _cfg(tmp_path: Path) -> dict:
    (tmp_path / "Data").mkdir(exist_ok=True)
    return {"data": {"bank_profiles_file": str(tmp_path / "Data/bank_profiles.json")}}


def _minimal_profile() -> dict:
    return {
        "bank_name": "Test Bank",
        "display_name": "Test Chequing",
        "account_type": "transaction",
        "date_col": "Date",
        "date_format": "%d/%m/%Y",
        "amount_col": "Amount",
        "credit_col": "",
        "debit_col": "",
        "description_col": "Description",
        "negate_amounts": False,
        "skip_rows": 0,
    }


# ── headers_key ───────────────────────────────────────────────────────────────

def test_headers_key_lowercases():
    assert headers_key(["Date", "Amount", "Description"]) == "date|amount|description"


def test_headers_key_strips_whitespace():
    assert headers_key([" Date ", " Amount "]) == "date|amount"


def test_headers_key_skips_blank_columns():
    assert headers_key(["Date", "", "Amount"]) == "date|amount"


def test_headers_key_order_preserving():
    assert headers_key(["A", "B"]) != headers_key(["B", "A"])


# ── load / save profiles ──────────────────────────────────────────────────────

def test_load_missing_file_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert load_profiles(cfg) == {}


def test_save_and_load_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    profiles = {"key1": {"bank_name": "ANZ"}}
    save_profiles(profiles, cfg)
    assert load_profiles(cfg) == profiles


def test_save_profile_inserts(tmp_path):
    cfg = _cfg(tmp_path)
    save_profile("k1", _minimal_profile(), cfg)
    loaded = load_profiles(cfg)
    assert "k1" in loaded
    assert loaded["k1"]["bank_name"] == "Test Bank"


def test_save_profile_overwrites_existing(tmp_path):
    cfg = _cfg(tmp_path)
    save_profile("k1", {"bank_name": "Old"}, cfg)
    save_profile("k1", {"bank_name": "New"}, cfg)
    assert load_profiles(cfg)["k1"]["bank_name"] == "New"


def test_delete_profile_existing(tmp_path):
    cfg = _cfg(tmp_path)
    save_profile("k1", _minimal_profile(), cfg)
    assert delete_profile("k1", cfg) is True
    assert "k1" not in load_profiles(cfg)


def test_delete_profile_missing_returns_false(tmp_path):
    cfg = _cfg(tmp_path)
    assert delete_profile("nonexistent", cfg) is False


# ── find_profile ──────────────────────────────────────────────────────────────

def test_find_profile_match(tmp_path):
    cfg = _cfg(tmp_path)
    key = headers_key(["Date", "Amount", "Description"])
    save_profile(key, _minimal_profile(), cfg)
    result = find_profile(["Date", "Amount", "Description"], cfg)
    assert result is not None
    assert result["bank_name"] == "Test Bank"


def test_find_profile_case_insensitive_headers(tmp_path):
    cfg = _cfg(tmp_path)
    key = headers_key(["DATE", "AMOUNT", "DESCRIPTION"])
    save_profile(key, _minimal_profile(), cfg)
    result = find_profile(["DATE", "AMOUNT", "DESCRIPTION"], cfg)
    assert result is not None


def test_find_profile_no_match(tmp_path):
    cfg = _cfg(tmp_path)
    save_profile("somekey", _minimal_profile(), cfg)
    result = find_profile(["Col1", "Col2"], cfg)
    assert result is None


# ── parse_generic_csv — single amount column ──────────────────────────────────

@pytest.fixture
def csv_single_amount(tmp_path) -> Path:
    p = tmp_path / "bank.csv"
    p.write_text("Date,Amount,Description\n01/06/2026,-25.50,WOOLWORTHS\n03/06/2026,1200.00,SALARY\n")
    return p


def test_parse_generic_csv_single_amount(csv_single_amount):
    profile = _minimal_profile()
    df = parse_generic_csv(csv_single_amount, profile)
    assert len(df) == 2
    assert set(df.columns) >= {"date", "amount", "description", "account", "account_type", "source_file"}
    assert df["amount"].tolist() == [-25.5, 1200.0]
    assert df["description"].tolist() == ["WOOLWORTHS", "SALARY"]
    assert df["account"].iloc[0] == "Test Chequing"


def test_parse_generic_csv_output_schema(csv_single_amount):
    df = parse_generic_csv(csv_single_amount, _minimal_profile())
    expected_cols = {"date", "amount", "description", "payee_name", "reference",
                     "note", "account", "account_type", "source_file", "is_pending"}
    assert set(df.columns) == expected_cols


# ── parse_generic_csv — credit/debit columns ─────────────────────────────────

@pytest.fixture
def csv_credit_debit(tmp_path) -> Path:
    p = tmp_path / "bank2.csv"
    p.write_text("Date,Credit,Debit,Merchant\n01/06/2026,,,WOOLWORTHS\n02/06/2026,1200.00,,SALARY\n03/06/2026,,25.50,COFFEE\n")
    return p


def test_parse_generic_csv_credit_debit(csv_credit_debit):
    profile = {**_minimal_profile(), "amount_col": "", "credit_col": "Credit", "debit_col": "Debit", "description_col": "Merchant"}
    df = parse_generic_csv(csv_credit_debit, profile)
    assert len(df) == 2  # zero row filtered out
    salary_row = df[df["description"] == "SALARY"].iloc[0]
    coffee_row = df[df["description"] == "COFFEE"].iloc[0]
    assert salary_row["amount"] == 1200.0
    assert coffee_row["amount"] == -25.5


# ── parse_generic_csv — negate_amounts ────────────────────────────────────────

@pytest.fixture
def csv_positive_debits(tmp_path) -> Path:
    p = tmp_path / "bank3.csv"
    p.write_text("Date,Amount,Details\n01/06/2026,25.50,WOOLWORTHS\n03/06/2026,1200.00,SALARY\n")
    return p


def test_parse_generic_csv_negate_amounts(csv_positive_debits):
    profile = {**_minimal_profile(), "description_col": "Details", "negate_amounts": True}
    df = parse_generic_csv(csv_positive_debits, profile)
    assert df["amount"].tolist() == [-25.5, -1200.0]


# ── parse_generic_csv — bad columns ──────────────────────────────────────────

def test_parse_generic_csv_missing_date_col_returns_empty(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Date,Amount,Desc\n01/06/2026,10.00,TEST\n")
    profile = {**_minimal_profile(), "date_col": "NonExistentCol"}
    df = parse_generic_csv(p, profile)
    assert df.empty


def test_parse_generic_csv_missing_amount_col_returns_empty(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Date,Amount,Desc\n01/06/2026,10.00,TEST\n")
    profile = {**_minimal_profile(), "amount_col": "NoAmt"}
    df = parse_generic_csv(p, profile)
    assert df.empty


# ── _detect_file_type — generic_csv / unknown_csv ────────────────────────────

def test_detect_returns_generic_csv_when_profile_exists(tmp_path):
    p = tmp_path / "westpac_export.csv"
    p.write_text("Date,Amount,Description\n01/06/2026,-25.50,COFFEE\n")
    cfg = _cfg(tmp_path)
    cfg["accounts"] = {}
    key = headers_key(["Date", "Amount", "Description"])
    save_profile(key, _minimal_profile(), cfg)
    ftype, profile = _detect_file_type(p, cfg)
    assert ftype == "generic_csv"
    assert profile["bank_name"] == "Test Bank"


def test_detect_returns_unknown_csv_when_no_profile(tmp_path):
    p = tmp_path / "mystery_bank.csv"
    p.write_text("TxnDate,TxnAmt,TxnDesc\n2026-06-01,-25.50,COFFEE\n")
    cfg = _cfg(tmp_path)
    cfg["accounts"] = {}
    ftype, _ = _detect_file_type(p, cfg)
    assert ftype == "unknown_csv"
