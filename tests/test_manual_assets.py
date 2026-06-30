"""Tests for src/manual_assets.py."""
import json

import pytest

from src.manual_assets import (
    load_manual_assets,
    save_manual_assets,
    latest_value,
    latest_liability_balance,
    total_assets_value,
    total_liabilities_balance,
    super_projected_balance,
    compute_net_worth_history,
    new_asset_id,
    new_liability_id,
    ASSET_TYPES,
    LIABILITY_TYPES,
)


def _cfg(tmp_path):
    return {"data": {"manual_assets_file": str(tmp_path / "manual_assets.json")}}


def _write(tmp_path, data):
    (tmp_path / "manual_assets.json").write_text(json.dumps(data), "utf-8")


# ── load / save ───────────────────────────────────────────────────────────────

def test_load_missing_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    data = load_manual_assets(cfg)
    assert data == {"assets": [], "liabilities": []}


def test_load_corrupt_returns_empty(tmp_path):
    (tmp_path / "manual_assets.json").write_text("NOT JSON", "utf-8")
    assert load_manual_assets(_cfg(tmp_path)) == {"assets": [], "liabilities": []}


def test_save_and_load_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    data = {
        "assets": [{"asset_id": "A1", "name": "Home", "type": "property",
                    "snapshots": [{"date": "2026-01-01", "value": 600000}]}],
        "liabilities": [],
    }
    save_manual_assets(data, cfg)
    assert load_manual_assets(cfg)["assets"][0]["name"] == "Home"


# ── latest_value ─────────────────────────────────────────────────────────────

def test_latest_value_no_snapshots():
    assert latest_value({"snapshots": []}) == 0.0


def test_latest_value_single_snapshot():
    assert latest_value({"snapshots": [{"date": "2026-01-01", "value": 500}]}) == 500.0


def test_latest_value_multiple_snapshots_picks_last_by_date():
    asset = {"snapshots": [
        {"date": "2025-01-01", "value": 400},
        {"date": "2026-06-01", "value": 650000},
        {"date": "2026-01-01", "value": 600000},
    ]}
    assert latest_value(asset) == 650000.0


def test_latest_liability_balance():
    liab = {"snapshots": [{"date": "2026-06-01", "balance": 380000}]}
    assert latest_liability_balance(liab) == 380000.0


def test_latest_liability_balance_empty():
    assert latest_liability_balance({"snapshots": []}) == 0.0


# ── totals ────────────────────────────────────────────────────────────────────

def test_total_assets_value():
    data = {
        "assets": [
            {"snapshots": [{"date": "2026-01-01", "value": 600000}]},
            {"snapshots": [{"date": "2026-01-01", "value": 50000}]},
        ],
        "liabilities": [],
    }
    assert total_assets_value(data) == 650000.0


def test_total_liabilities_balance():
    data = {
        "assets": [],
        "liabilities": [
            {"snapshots": [{"date": "2026-01-01", "balance": 380000}]},
            {"snapshots": [{"date": "2026-01-01", "balance": 42000}]},
        ],
    }
    assert total_liabilities_balance(data) == 422000.0


# ── super projection ──────────────────────────────────────────────────────────

def test_super_projected_no_snapshots():
    assert super_projected_balance({"snapshots": []}) is None


def test_super_projected_already_retired():
    asset = {
        "snapshots": [{"date": "2026-01-01", "value": 800000}],
        "birth_year": 1959,
        "retirement_age": 67,
        "expected_return_pct": 7,
        "annual_contribution": 0,
    }
    # born 1959, age 2026−1959=67, at retirement already
    result = super_projected_balance(asset)
    assert result == 800000.0


def test_super_projected_grows():
    asset = {
        "snapshots": [{"date": "2026-01-01", "value": 100000}],
        "birth_year": 1988,   # ~38 years old in 2026, 29 years to 67
        "retirement_age": 67,
        "expected_return_pct": 7,
        "annual_contribution": 0,
    }
    result = super_projected_balance(asset)
    assert result is not None
    assert result > 100000  # must grow


def test_super_projected_with_contributions():
    asset = {
        "snapshots": [{"date": "2026-01-01", "value": 50000}],
        "years_to_retire": 20,
        "expected_return_pct": 7,
        "annual_contribution": 10000,
    }
    result = super_projected_balance(asset)
    assert result is not None
    assert result > 50000 * (1.07 ** 20)  # must exceed balance-only projection


# ── new IDs ───────────────────────────────────────────────────────────────────

def test_new_asset_id_unique():
    ids = {new_asset_id() for _ in range(20)}
    assert len(ids) == 20


def test_new_liability_id_unique():
    ids = {new_liability_id() for _ in range(20)}
    assert len(ids) == 20


# ── compute_net_worth_history ────────────────────────────────────────────────

def test_nw_history_empty():
    import pandas as pd
    result = compute_net_worth_history(pd.DataFrame(), {"assets": [], "liabilities": []})
    assert result.empty


def test_nw_history_bank_only():
    import pandas as pd
    balances = pd.DataFrame([
        {"date": pd.Timestamp("2026-01-31"), "account": "ANZ", "balance": 10000.0},
        {"date": pd.Timestamp("2026-06-30"), "account": "ANZ", "balance": 15000.0},
    ])
    result = compute_net_worth_history(balances, {"assets": [], "liabilities": []})
    assert not result.empty
    assert result["net_worth"].iloc[-1] == pytest.approx(15000.0)


def test_nw_history_includes_manual_assets():
    import pandas as pd
    balances = pd.DataFrame([
        {"date": pd.Timestamp("2026-01-31"), "account": "ANZ", "balance": 10000.0},
    ])
    manual = {
        "assets": [{"snapshots": [{"date": "2026-01-01", "value": 600000}]}],
        "liabilities": [{"snapshots": [{"date": "2026-01-01", "balance": 400000}]}],
    }
    result = compute_net_worth_history(balances, manual)
    # net worth = 10000 + 600000 - 400000 = 210000
    last = result.iloc[-1]
    assert last["net_worth"] == pytest.approx(210000.0, abs=1.0)
