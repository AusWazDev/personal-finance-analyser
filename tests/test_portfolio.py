"""Tests for src/portfolio.py."""
import json

import pytest

from src.portfolio import (
    load_portfolio,
    save_portfolio,
    new_lot_id,
    holdings_summary,
)


def _cfg(tmp_path):
    return {"data": {"portfolio_file": str(tmp_path / "portfolio.json")}}


# ── load / save ───────────────────────────────────────────────────────────────

def test_load_missing_returns_empty(tmp_path):
    assert load_portfolio(_cfg(tmp_path)) == {"lots": []}


def test_load_corrupt_returns_empty(tmp_path):
    (tmp_path / "portfolio.json").write_text("NOTJSON", "utf-8")
    assert load_portfolio(_cfg(tmp_path)) == {"lots": []}


def test_save_and_load_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    data = {"lots": [{"lot_id": "P1", "ticker": "VAS", "units": 100, "cost_per_unit": 98.50}]}
    save_portfolio(data, cfg)
    assert load_portfolio(cfg)["lots"][0]["ticker"] == "VAS"


# ── new_lot_id ───────────────────────────────────────────────────────────────

def test_new_lot_id_unique():
    ids = {new_lot_id() for _ in range(20)}
    assert len(ids) == 20


# ── holdings_summary ─────────────────────────────────────────────────────────

def test_holdings_summary_empty():
    assert holdings_summary([]) == []


def test_holdings_summary_single_lot():
    lots = [{"ticker": "VAS", "name": "Vanguard AU", "units": 100, "cost_per_unit": 98.5}]
    result = holdings_summary(lots)
    assert len(result) == 1
    h = result[0]
    assert h["ticker"] == "VAS"
    assert h["units"] == pytest.approx(100.0)
    assert h["cost_basis"] == pytest.approx(9850.0)
    assert h["avg_cost"] == pytest.approx(98.5)
    assert h["current_value"] is None  # no price


def test_holdings_summary_multiple_lots_same_ticker():
    lots = [
        {"ticker": "NDQ", "name": "BetaShares Nasdaq", "units": 50, "cost_per_unit": 40.0},
        {"ticker": "NDQ", "name": "BetaShares Nasdaq", "units": 50, "cost_per_unit": 44.0},
    ]
    result = holdings_summary(lots)
    assert len(result) == 1
    h = result[0]
    assert h["units"] == pytest.approx(100.0)
    assert h["cost_basis"] == pytest.approx(4200.0)
    assert h["avg_cost"] == pytest.approx(42.0)


def test_holdings_summary_with_price_pl_positive():
    lots = [{"ticker": "A200", "name": "A200", "units": 100, "cost_per_unit": 100.0}]
    prices = {"A200": 120.0}
    result = holdings_summary(lots, prices)
    h = result[0]
    assert h["current_value"] == pytest.approx(12000.0)
    assert h["unrealised_pl"] == pytest.approx(2000.0)
    assert h["pl_pct"] == pytest.approx(20.0)


def test_holdings_summary_with_price_pl_negative():
    lots = [{"ticker": "XYZ", "name": "XYZ", "units": 200, "cost_per_unit": 50.0}]
    prices = {"XYZ": 45.0}
    result = holdings_summary(lots, prices)
    h = result[0]
    assert h["unrealised_pl"] == pytest.approx(-1000.0)
    assert h["pl_pct"] < 0


def test_holdings_summary_sorted_by_cost_desc():
    lots = [
        {"ticker": "A", "name": "A", "units": 1, "cost_per_unit": 100},
        {"ticker": "B", "name": "B", "units": 1, "cost_per_unit": 500},
        {"ticker": "C", "name": "C", "units": 1, "cost_per_unit": 250},
    ]
    result = holdings_summary(lots)
    assert [h["ticker"] for h in result] == ["B", "C", "A"]


def test_holdings_summary_skips_no_ticker():
    lots = [
        {"ticker": "", "name": "Unnamed", "units": 100, "cost_per_unit": 10},
        {"ticker": "VAS", "name": "VAS", "units": 10, "cost_per_unit": 100},
    ]
    result = holdings_summary(lots)
    assert len(result) == 1
    assert result[0]["ticker"] == "VAS"


# ── CAGR / annualised return ──────────────────────────────────────────────────

def test_cagr_positive_gain():
    """Holding bought 2 years ago at $100, now $150 → CAGR ≈ 22.5% p.a."""
    from datetime import date, timedelta
    purchase = (date.today() - timedelta(days=730)).isoformat()
    lots = [{"ticker": "VAS", "name": "VAS", "units": 100, "cost_per_unit": 1.0, "date": purchase}]
    prices = {"VAS": 1.5}  # 50% total gain over ~2 years
    h = holdings_summary(lots, prices)[0]
    assert h["cagr"] is not None
    assert h["cagr"] == pytest.approx(22.5, abs=1.0)


def test_cagr_negative_loss():
    """Loss case — CAGR should be negative."""
    from datetime import date, timedelta
    purchase = (date.today() - timedelta(days=365)).isoformat()
    lots = [{"ticker": "XYZ", "name": "XYZ", "units": 1, "cost_per_unit": 100.0, "date": purchase}]
    prices = {"XYZ": 80.0}
    h = holdings_summary(lots, prices)[0]
    assert h["cagr"] is not None
    assert h["cagr"] < 0


def test_cagr_suppressed_under_3_months():
    """CAGR must be None when holding is < 3 months old."""
    from datetime import date, timedelta
    purchase = (date.today() - timedelta(days=45)).isoformat()
    lots = [{"ticker": "NEW", "name": "NEW", "units": 10, "cost_per_unit": 50.0, "date": purchase}]
    prices = {"NEW": 60.0}
    h = holdings_summary(lots, prices)[0]
    assert h["cagr"] is None


def test_cagr_none_without_price():
    """Without a price, cagr must be None."""
    from datetime import date, timedelta
    purchase = (date.today() - timedelta(days=500)).isoformat()
    lots = [{"ticker": "VAS", "name": "VAS", "units": 100, "cost_per_unit": 1.0, "date": purchase}]
    h = holdings_summary(lots)[0]  # no prices dict
    assert h["cagr"] is None


def test_cagr_picks_earliest_lot_date():
    """CAGR should use the earliest lot date across multi-lot holdings."""
    from datetime import date, timedelta
    early = (date.today() - timedelta(days=730)).isoformat()
    late  = (date.today() - timedelta(days=365)).isoformat()
    lots = [
        {"ticker": "VAS", "name": "VAS", "units": 50, "cost_per_unit": 1.0, "date": late},
        {"ticker": "VAS", "name": "VAS", "units": 50, "cost_per_unit": 1.0, "date": early},
    ]
    prices = {"VAS": 1.5}
    h = holdings_summary(lots, prices)[0]
    # 2-year CAGR ≈ 22.5% (not ~41% which would be 1-year CAGR)
    assert h["cagr"] == pytest.approx(22.5, abs=1.5)
