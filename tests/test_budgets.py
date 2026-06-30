"""Tests for src/budgets.py — load, save, migrate, suggest."""
import json
import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.budgets import (
    load_budgets, save_budgets, suggest_budgets,
    load_rollover_settings, get_effective_budget,
    load_period_settings, save_period_settings, current_fortnight_window,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(tmp_path, budgets_file=None, legacy_budgets=None):
    d = {"data": {}}
    if budgets_file:
        d["data"]["budgets_file"] = str(budgets_file)
    if legacy_budgets:
        d["budgets"] = legacy_budgets
    return d


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions ("
        "txn_id TEXT, date TEXT, description TEXT, amount REAL, "
        "account TEXT, category TEXT, sub_category TEXT)"
    )
    conn.commit()
    return conn


def _insert(conn, txn_id, date, amount, category):
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?)",
        (txn_id, date, "desc", amount, "ANZ", category, ""),
    )
    conn.commit()


# ── load_budgets ──────────────────────────────────────────────────────────────

def test_load_returns_empty_when_no_file_and_no_config(tmp_path):
    cfg = _cfg(tmp_path, budgets_file=tmp_path / "budgets.json")
    assert load_budgets(cfg) == {}


def test_load_reads_existing_json(tmp_path):
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"budgets": {"Groceries": 400.0, "Dining Out": 200.0}}))
    cfg = _cfg(tmp_path, budgets_file=p)
    result = load_budgets(cfg)
    assert result == {"Groceries": 400.0, "Dining Out": 200.0}


def test_load_skips_zero_values_in_json(tmp_path):
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"budgets": {"Groceries": 400.0, "Transport": 0}}))
    cfg = _cfg(tmp_path, budgets_file=p)
    result = load_budgets(cfg)
    assert "Transport" not in result
    assert result["Groceries"] == 400.0


def test_load_migrates_from_config_yaml_when_no_json(tmp_path):
    p = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=p, legacy_budgets={"Groceries": 350.0})
    result = load_budgets(cfg)
    assert result == {"Groceries": 350.0}
    # File should now exist
    assert p.exists()
    saved = json.loads(p.read_text())
    assert saved["budgets"]["Groceries"] == 350.0


def test_load_prefers_json_over_config_yaml(tmp_path):
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"budgets": {"Groceries": 500.0}}))
    cfg = _cfg(tmp_path, budgets_file=p, legacy_budgets={"Groceries": 999.0})
    assert load_budgets(cfg)["Groceries"] == 500.0


# ── save_budgets ──────────────────────────────────────────────────────────────

def test_save_writes_json(tmp_path):
    p = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=p)
    save_budgets({"Groceries": 400.0, "Transport": 150.0}, cfg)
    data = json.loads(p.read_text())
    assert data["budgets"]["Groceries"] == 400.0
    assert data["budgets"]["Transport"] == 150.0


def test_save_strips_zero_and_none(tmp_path):
    p = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=p)
    save_budgets({"Groceries": 400.0, "Transport": 0, "Dining Out": None}, cfg)
    data = json.loads(p.read_text())
    assert "Transport" not in data["budgets"]
    assert "Dining Out" not in data["budgets"]
    assert "Groceries" in data["budgets"]


def test_save_includes_updated_at(tmp_path):
    p = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=p)
    save_budgets({"Groceries": 300.0}, cfg)
    data = json.loads(p.read_text())
    assert "updated_at" in data


# ── suggest_budgets ───────────────────────────────────────────────────────────

def test_suggest_returns_categories_with_spend():
    conn = _conn()
    _insert(conn, "t1", "2026-03-01", -150.0, "Groceries")
    _insert(conn, "t2", "2026-04-01", -200.0, "Groceries")
    _insert(conn, "t3", "2026-03-15",  100.0, "Income")     # credit — ignored
    result = suggest_budgets(conn, months=6)
    assert "Groceries" in result
    assert "Income" not in result  # not in debit data


def test_suggest_rounds_up_to_nearest_25():
    conn = _conn()
    _insert(conn, "t1", "2026-05-01", -137.0, "Transport")
    result = suggest_budgets(conn, months=6)
    # avg = 137, round up to nearest 25 = 150
    assert result["Transport"]["suggested"] == 150.0


def test_suggest_minimum_25():
    conn = _conn()
    _insert(conn, "t1", "2026-05-01", -5.0, "Dining Out")
    result = suggest_budgets(conn, months=6)
    assert result["Dining Out"]["suggested"] == 25.0


def test_suggest_skips_transfers_and_investment():
    conn = _conn()
    _insert(conn, "t1", "2026-05-01", -500.0, "Transfers")
    _insert(conn, "t2", "2026-05-01", -300.0, "Investment")
    result = suggest_budgets(conn, months=6)
    assert "Transfers" not in result
    assert "Investment" not in result


# ── budget burn rate (via _load_budget_status) ────────────────────────────────

def _make_burn_rate_client(tmp_path, monkeypatch, budget_limit, actual_spend):
    """Helper: sets up a controlled _load_budget_status environment."""
    import calendar
    from datetime import date
    import server
    import src.db as _db_mod

    data_dir = tmp_path / "Data"
    data_dir.mkdir(exist_ok=True)
    budgets_file = data_dir / "budgets.json"
    budgets_file.write_text(json.dumps({"budgets": {"Groceries": budget_limit}}), "utf-8")
    (data_dir / "modules.json").write_text('{"modules": {}}', "utf-8")

    cfg = {
        "data": {"budgets_file": str(budgets_file)},
        "modules": {},
    }
    monkeypatch.setattr(server, "_load_config", lambda: cfg)

    today = date.today()

    def _mock_db(_cfg):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        conn.execute(
            "INSERT INTO transactions (txn_id, date, amount, account, category) VALUES (?,?,?,?,?)",
            ("T1", f"{today.year}-{today.month:02d}-01", -actual_spend, "ANZ", "Groceries"),
        )
        conn.commit()
        return conn

    monkeypatch.setattr(_db_mod, "get_db", _mock_db)
    return today, calendar.monthrange(today.year, today.month)[1]


def test_burn_rate_projected_calculation(tmp_path, monkeypatch):
    """_load_budget_status should compute projected EoM spend from daily rate."""
    import server
    today, days_in_month = _make_burn_rate_client(tmp_path, monkeypatch, 400, 200.0)

    status = server._load_budget_status()
    all_cats = {e["category"]: e for e in status.get("all", [])}
    assert "Groceries" in all_cats
    entry = all_cats["Groceries"]

    days_elapsed = today.day
    expected_projected = round(200.0 / (days_elapsed / days_in_month), 2)
    assert entry["projected"] == pytest.approx(expected_projected, rel=0.01)
    assert entry["days_in_month"] == days_in_month
    assert entry["days_elapsed"] == days_elapsed


def test_burn_rate_days_to_over(tmp_path, monkeypatch):
    """days_to_over should reflect remaining budget at current daily rate."""
    import server
    today, _ = _make_burn_rate_client(tmp_path, monkeypatch, 300, 150.0)

    status = server._load_budget_status()
    all_cats = {e["category"]: e for e in status.get("all", [])}
    entry = all_cats.get("Groceries")
    assert entry is not None
    # $150 spent, $300 limit → $150 remaining; daily rate = $150 / days_elapsed
    daily_rate = 150.0 / today.day
    expected_days_to_over = round(150.0 / daily_rate, 1)
    assert entry["days_to_over"] == pytest.approx(expected_days_to_over, rel=0.05)


# ── Rollover settings ─────────────────────────────────────────────────────────

def test_load_rollover_settings_empty_when_no_file(tmp_path):
    cfg = _cfg(tmp_path, budgets_file=tmp_path / "budgets.json")
    assert load_rollover_settings(cfg) == {}


def test_save_budgets_preserves_rollover(tmp_path):
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    # First write with rollover
    save_budgets({"Groceries": 500}, cfg, rollover={"Groceries": True})
    data = json.loads(path.read_text())
    assert data["rollover"] == {"Groceries": True}
    # Second write (limits only) must not erase rollover
    save_budgets({"Groceries": 600}, cfg)
    data2 = json.loads(path.read_text())
    assert data2["rollover"] == {"Groceries": True}
    assert data2["budgets"]["Groceries"] == 600.0


def test_load_rollover_settings_round_trip(tmp_path):
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_budgets({"Groceries": 400, "Dining Out": 200}, cfg, rollover={"Groceries": True})
    loaded = load_rollover_settings(cfg)
    assert loaded.get("Groceries") is True
    assert "Dining Out" not in loaded  # not set, so absent


def test_get_effective_budget_no_rollover(tmp_path):
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_budgets({"Groceries": 500}, cfg)
    conn = _conn()
    result = get_effective_budget(conn, "Groceries", "2026-06", cfg)
    assert result == {"base": 500.0, "rollover_amount": 0.0, "effective": 500.0}


def test_get_effective_budget_with_rollover_unspent(tmp_path):
    """Unspent $100 in previous month carries forward to current month."""
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_budgets({"Groceries": 500}, cfg, rollover={"Groceries": True})
    conn = _conn()
    # Previous month (2026-05): spent $400 out of $500 limit → $100 unspent
    _insert(conn, "t1", "2026-05-10", -400.0, "Groceries")
    result = get_effective_budget(conn, "Groceries", "2026-06", cfg)
    assert result["base"] == 500.0
    assert result["rollover_amount"] == 100.0
    assert result["effective"] == 600.0


def test_get_effective_budget_with_rollover_overspent(tmp_path):
    """Overspent previous month yields zero rollover."""
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_budgets({"Dining Out": 200}, cfg, rollover={"Dining Out": True})
    conn = _conn()
    # Previous month: spent $250 — over limit, nothing to carry
    _insert(conn, "t2", "2026-05-15", -250.0, "Dining Out")
    result = get_effective_budget(conn, "Dining Out", "2026-06", cfg)
    assert result["rollover_amount"] == 0.0
    assert result["effective"] == 200.0


def test_get_effective_budget_rollover_capped_at_base(tmp_path):
    """Rollover is capped at 1× base even if nothing was spent last month."""
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_budgets({"Groceries": 300}, cfg, rollover={"Groceries": True})
    conn = _conn()
    # Nothing spent last month — full $300 unspent, but cap is 1× = $300
    result = get_effective_budget(conn, "Groceries", "2026-06", cfg)
    assert result["rollover_amount"] == 300.0  # capped at base
    assert result["effective"] == 600.0


def test_suggest_avg_across_multiple_months():
    conn = _conn()
    _insert(conn, "t1", "2026-03-01", -100.0, "Health")
    _insert(conn, "t2", "2026-04-01", -200.0, "Health")
    result = suggest_budgets(conn, months=6)
    # avg = (100 + 200) / 2 = 150, rounded to nearest 25 = 150
    assert result["Health"]["avg"] == 150.0
    assert result["Health"]["months_with_data"] == 2


def test_suggest_empty_db_returns_empty():
    conn = _conn()
    result = suggest_budgets(conn, months=3)
    assert result == {}


# ── Fortnightly period settings ───────────────────────────────────────────────

def test_load_period_settings_defaults_empty(tmp_path):
    """No periods.json → empty dict (all monthly by default)."""
    cfg = _cfg(tmp_path, budgets_file=tmp_path / "budgets.json")
    assert load_period_settings(cfg) == {}


def test_save_and_load_period_settings(tmp_path):
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_period_settings({"Groceries": "fortnightly", "Transport": "monthly"}, cfg)
    loaded = load_period_settings(cfg)
    assert loaded.get("Groceries") == "fortnightly"
    assert "Transport" not in loaded  # monthly is the default, not persisted


def test_period_settings_only_stores_fortnightly(tmp_path):
    """Monthly is the default — only fortnightly entries are persisted."""
    path = tmp_path / "budgets.json"
    cfg = _cfg(tmp_path, budgets_file=path)
    save_period_settings({"A": "monthly", "B": "fortnightly"}, cfg)
    raw = json.loads(path.read_text())
    assert "A" not in raw.get("periods", {})
    assert "B" in raw.get("periods", {})


def test_current_fortnight_window_returns_14_day_range():
    start, end = current_fortnight_window()
    assert (end - start).days == 13  # inclusive


def test_current_fortnight_window_anchored_to_monday():
    from datetime import date
    start, _ = current_fortnight_window()
    assert start.weekday() == 0  # Monday


def test_current_fortnight_window_contains_today():
    from datetime import date
    start, end = current_fortnight_window()
    assert start <= date.today() <= end


def test_fortnight_window_consistent_across_calls():
    """Two calls on the same day return the same window."""
    from datetime import date
    a = current_fortnight_window(date.today())
    b = current_fortnight_window(date.today())
    assert a == b
