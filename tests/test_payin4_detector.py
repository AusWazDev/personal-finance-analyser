"""Tests for src/payin4_detector.py"""

import json
from pathlib import Path

import pandas as pd
import pytest

from src.payin4_detector import (
    detect_payin4_groups,
    load_payin4_groups,
    merge_groups,
    save_payin4_groups,
)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df["amount"].astype(float)
    return df


def _purchase(txn_id="p1", date="2024-01-15", amount=-450.02,
              description="Virgin Australia Airlines", note="Express Checkout Payment"):
    return {
        "txn_id": txn_id,
        "date": date,
        "amount": amount,
        "description": description,
        "note": note,
        "account_type": "paypal",
        "account": "PayPal",
        "is_pending": False,
    }


def _instalment(txn_id, date, amount=-112.51):
    return {
        "txn_id": txn_id,
        "date": date,
        "amount": amount,
        "description": "PayPal Australia Pty Limited",
        "note": "",
        "account_type": "paypal",
        "account": "PayPal",
        "is_pending": False,
    }


def _anz_debit(txn_id, date, amount=-112.51):
    return {
        "txn_id": txn_id,
        "date": date,
        "amount": amount,
        "description": "PYPL PAYIN4 1234567890",
        "note": "",
        "account_type": "anz_plus",
        "account": "ANZ Plus",
        "is_pending": False,
    }


# ---------------------------------------------------------------------------
# detect_payin4_groups
# ---------------------------------------------------------------------------

def test_detect_empty_df():
    assert detect_payin4_groups(pd.DataFrame()) == []


def test_detect_no_paypal_rows():
    df = _make_df([_anz_debit("a1", "2024-01-15")])
    assert detect_payin4_groups(df) == []


def test_detect_complete_group():
    rows = [
        _purchase("p1", "2024-01-15", -450.04),
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-29", -112.51),
        _instalment("i3", "2024-02-12", -112.51),
        _instalment("i4", "2024-02-26", -112.51),
        _anz_debit("a1", "2024-01-15"),
        _anz_debit("a2", "2024-01-29"),
        _anz_debit("a3", "2024-02-12"),
        _anz_debit("a4", "2024-02-26"),
    ]
    groups = detect_payin4_groups(_make_df(rows))
    assert len(groups) == 1
    g = groups[0]
    assert g["group_id"] == "p1"
    assert g["merchant"] == "Virgin Australia Airlines"
    assert g["anz_matched"] == 4
    assert g["status"] == "complete"
    assert len(g["instalments"]) == 4
    assert g["instalments"][0]["sequence"] == 1
    assert g["instalments"][3]["sequence"] == 4


def test_detect_partial_group_two_anz_matched():
    rows = [
        _purchase("p1", "2024-01-15", -450.04),
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-29", -112.51),
        _instalment("i3", "2024-02-12", -112.51),
        _instalment("i4", "2024-02-26", -112.51),
        _anz_debit("a1", "2024-01-15"),
        _anz_debit("a2", "2024-01-29"),
    ]
    groups = detect_payin4_groups(_make_df(rows))
    assert len(groups) == 1
    g = groups[0]
    assert g["anz_matched"] == 2
    assert g["status"] == "partial"


def test_detect_instalment_date_tolerance():
    """Instalments ±3 days off the expected 14-day interval still match."""
    rows = [
        _purchase("p1", "2024-01-15", -450.04),
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-31", -112.51),  # +2 days from expected 1-29
        _instalment("i3", "2024-02-14", -112.51),  # +2 days from expected 2-12
        _instalment("i4", "2024-02-26", -112.51),
    ]
    groups = detect_payin4_groups(_make_df(rows))
    assert len(groups) == 1
    assert len(groups[0]["instalments"]) == 4


def test_detect_skips_too_small_purchase():
    rows = [
        _purchase("p1", "2024-01-15", -3.00),
        _instalment("i1", "2024-01-15", -0.75),
        _instalment("i2", "2024-01-29", -0.75),
        _instalment("i3", "2024-02-12", -0.75),
        _instalment("i4", "2024-02-26", -0.75),
    ]
    assert detect_payin4_groups(_make_df(rows)) == []


def test_detect_skips_pending_purchase():
    rows = [
        {**_purchase("p1", "2024-01-15", -450.04), "is_pending": True},
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-29", -112.51),
        _instalment("i3", "2024-02-12", -112.51),
        _instalment("i4", "2024-02-26", -112.51),
    ]
    assert detect_payin4_groups(_make_df(rows)) == []


def test_detect_instalment_sum_tolerance():
    """Rounding: 4 × $112.51 = $450.04 ≠ $450.02 — still within ±$0.05."""
    rows = [
        _purchase("p1", "2024-01-15", -450.02),
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-29", -112.51),
        _instalment("i3", "2024-02-12", -112.51),
        _instalment("i4", "2024-02-26", -112.51),  # sum = 450.04, off by 0.02
    ]
    groups = detect_payin4_groups(_make_df(rows))
    assert len(groups) == 1


def test_detect_no_duplicate_instalment_use():
    """Two purchases on the same day should not share instalments."""
    rows = [
        _purchase("p1", "2024-01-15", -450.04, description="Merchant A"),
        _purchase("p2", "2024-01-15", -450.04, description="Merchant B"),
        _instalment("i1", "2024-01-15", -112.51),
        _instalment("i2", "2024-01-29", -112.51),
        _instalment("i3", "2024-02-12", -112.51),
        _instalment("i4", "2024-02-26", -112.51),
    ]
    groups = detect_payin4_groups(_make_df(rows))
    # Only one group can claim these 4 instalments
    assert len(groups) == 1


# ---------------------------------------------------------------------------
# merge_groups
# ---------------------------------------------------------------------------

def test_merge_empty_lists():
    assert merge_groups([], []) == []


def test_merge_adds_new_groups():
    existing = [{"group_id": "p1", "merchant": "A"}]
    new = [{"group_id": "p2", "merchant": "B"}]
    merged = merge_groups(existing, new)
    assert len(merged) == 2
    assert merged[1]["group_id"] == "p2"


def test_merge_skips_duplicate_ids():
    existing = [{"group_id": "p1", "merchant": "A"}]
    new = [{"group_id": "p1", "merchant": "A updated"}]
    merged = merge_groups(existing, new)
    assert len(merged) == 1
    assert merged[0]["merchant"] == "A"


def test_merge_preserves_order():
    existing = [{"group_id": "p1"}, {"group_id": "p2"}]
    new = [{"group_id": "p3"}]
    merged = merge_groups(existing, new)
    assert [g["group_id"] for g in merged] == ["p1", "p2", "p3"]


# ---------------------------------------------------------------------------
# load / save round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_round_trip(tmp_path):
    config = {"data": {"payin4_groups_file": str(tmp_path / "payin4.json")}}
    groups = [{"group_id": "p1", "merchant": "Test Merchant", "instalments": []}]
    save_payin4_groups(groups, config)
    loaded = load_payin4_groups(config)
    assert loaded == groups


def test_load_missing_file_returns_empty(tmp_path):
    config = {"data": {"payin4_groups_file": str(tmp_path / "nonexistent.json")}}
    assert load_payin4_groups(config) == []


def test_load_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "payin4.json"
    p.write_text("not json", encoding="utf-8")
    config = {"data": {"payin4_groups_file": str(p)}}
    assert load_payin4_groups(config) == []


def test_save_creates_parent_dirs(tmp_path):
    config = {"data": {"payin4_groups_file": str(tmp_path / "sub" / "dir" / "payin4.json")}}
    save_payin4_groups([{"group_id": "x"}], config)
    assert Path(config["data"]["payin4_groups_file"]).exists()


def test_save_overwrites_existing(tmp_path):
    config = {"data": {"payin4_groups_file": str(tmp_path / "payin4.json")}}
    save_payin4_groups([{"group_id": "p1"}], config)
    save_payin4_groups([{"group_id": "p2"}], config)
    loaded = load_payin4_groups(config)
    assert len(loaded) == 1
    assert loaded[0]["group_id"] == "p2"


def test_load_default_path_key_missing(tmp_path, monkeypatch):
    """Config with no payin4_groups_file key falls back to default path."""
    monkeypatch.chdir(tmp_path)
    loaded = load_payin4_groups({})
    assert loaded == []
