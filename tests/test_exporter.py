"""Tests for src/exporter.py — category/date filtering and CSV output."""
import pandas as pd
import pytest

import src.exporter as exporter_mod
import src.db as db_mod


# ── Fixture ───────────────────────────────────────────────────────────────────

def _make_df():
    return pd.DataFrame([
        {"date": pd.Timestamp("2025-07-15"), "amount": -50.0,  "category": "Groceries",  "description": "Woolworths"},
        {"date": pd.Timestamp("2025-08-01"), "amount": -120.0, "category": "Transport",  "description": "Fuel"},
        {"date": pd.Timestamp("2025-09-10"), "amount": -30.0,  "category": "Groceries",  "description": "Coles"},
        {"date": pd.Timestamp("2025-10-05"), "amount": 5000.0, "category": "Income",     "description": "Trust dist"},
    ])


# load_transactions is imported *inside* export_transactions, so patch it at the source.
_PATCH = "src.db.load_transactions"


# ── Category filter ───────────────────────────────────────────────────────────

def test_category_filter_returns_matching_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "out.csv"
    exporter_mod.export_transactions({}, output=str(out), category="Groceries")
    result = pd.read_csv(out)
    assert len(result) == 2
    assert set(result["category"]) == {"Groceries"}


def test_category_filter_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "out.csv"
    exporter_mod.export_transactions({}, output=str(out), category="groceries")
    result = pd.read_csv(out)
    assert len(result) == 2


# ── Date filter ───────────────────────────────────────────────────────────────

def test_from_date_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "out.csv"
    exporter_mod.export_transactions({}, output=str(out), from_date="2025-09")
    result = pd.read_csv(out)
    assert len(result) == 2  # Sep + Oct rows


def test_to_date_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "out.csv"
    exporter_mod.export_transactions({}, output=str(out), to_date="2025-08")
    result = pd.read_csv(out)
    assert len(result) == 2  # Jul + Aug rows


def test_date_range_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "out.csv"
    exporter_mod.export_transactions({}, output=str(out), from_date="2025-08", to_date="2025-09")
    result = pd.read_csv(out)
    assert len(result) == 2  # Aug + Sep rows


# ── Output file ───────────────────────────────────────────────────────────────

def test_output_csv_written_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "export.csv"
    exporter_mod.export_transactions({}, output=str(out))
    assert out.exists()


def test_output_csv_contains_all_rows_when_no_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "export.csv"
    exporter_mod.export_transactions({}, output=str(out))
    result = pd.read_csv(out)
    assert len(result) == 4


def test_output_sorted_descending_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    out = tmp_path / "export.csv"
    exporter_mod.export_transactions({}, output=str(out))
    result = pd.read_csv(out)
    dates = result["date"].tolist()
    assert dates == sorted(dates, reverse=True)


def test_no_output_path_prints_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(db_mod, "load_transactions", lambda cfg: _make_df())
    exporter_mod.export_transactions({}, output=None)
    captured = capsys.readouterr()
    assert "Groceries" in captured.out
