"""Tests for src/commitment_detector.py — frequency detection, projection, persistence."""
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from src.commitment_detector import (
    _add_months,
    _classify_frequency,
    _next_due,
    get_upcoming,
    load_commitments,
    merge_commitments,
    monthly_committed_total,
    save_commitments,
)


# ── _classify_frequency ────────────────────────────────────────────────────────

def test_classify_weekly():
    assert _classify_frequency(7) == "weekly"


def test_classify_fortnightly():
    assert _classify_frequency(14) == "fortnightly"


def test_classify_monthly():
    assert _classify_frequency(30) == "monthly"


def test_classify_quarterly():
    assert _classify_frequency(91) == "quarterly"


def test_classify_annual():
    assert _classify_frequency(365) == "annual"


def test_classify_returns_none_for_irregular():
    assert _classify_frequency(50) is None
    assert _classify_frequency(200) is None


# ── _add_months ───────────────────────────────────────────────────────────────

def test_add_months_simple():
    assert _add_months(date(2025, 1, 15), 1) == date(2025, 2, 15)


def test_add_months_clamps_to_last_day():
    # Jan 31 + 1 month → Feb 28 (2025 is not leap)
    assert _add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)


def test_add_months_leap_year():
    # Jan 31 + 1 month → Feb 29 in a leap year
    assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_add_months_crosses_year():
    assert _add_months(date(2025, 11, 15), 2) == date(2026, 1, 15)


def test_add_months_twelve_is_one_year():
    assert _add_months(date(2025, 6, 15), 12) == date(2026, 6, 15)


# ── _next_due ─────────────────────────────────────────────────────────────────

def test_next_due_monthly_already_past():
    # Give a last_seen far in the past — result must be in the future
    last = date.today() - timedelta(days=60)
    result = _next_due(last, "monthly")
    assert result > date.today()


def test_next_due_weekly_is_in_future():
    last = date.today() - timedelta(days=3)
    result = _next_due(last, "weekly")
    assert result > date.today()


def test_next_due_annual_is_in_future():
    last = date.today() - timedelta(days=400)
    result = _next_due(last, "annual")
    assert result > date.today()


# ── monthly_committed_total ───────────────────────────────────────────────────

def _item(amount, frequency, active=True):
    return {"amount": amount, "frequency": frequency, "active": active}


def test_monthly_total_single_monthly():
    commitments = {"items": [_item(100.0, "monthly")]}
    assert monthly_committed_total(commitments) == 100.0


def test_monthly_total_weekly_normalised():
    commitments = {"items": [_item(100.0, "weekly")]}
    # 52/12 ≈ 4.333... per month
    result = monthly_committed_total(commitments)
    assert abs(result - round(100.0 * 52 / 12, 2)) < 0.01


def test_monthly_total_annual_normalised():
    commitments = {"items": [_item(1200.0, "annual")]}
    assert monthly_committed_total(commitments) == 100.0


def test_monthly_total_quarterly_normalised():
    commitments = {"items": [_item(300.0, "quarterly")]}
    assert monthly_committed_total(commitments) == 100.0


def test_monthly_total_skips_inactive():
    commitments = {"items": [_item(500.0, "monthly", active=False)]}
    assert monthly_committed_total(commitments) == 0.0


def test_monthly_total_mixed_frequencies():
    commitments = {"items": [
        _item(100.0, "monthly"),
        _item(1200.0, "annual"),   # → 100/month
        _item(50.0, "monthly", active=False),  # excluded
    ]}
    assert monthly_committed_total(commitments) == 200.0


def test_monthly_total_empty():
    assert monthly_committed_total({"items": []}) == 0.0


# ── get_upcoming ──────────────────────────────────────────────────────────────

def _commitment(amount, frequency, next_due_offset_days=1):
    next_due = (date.today() + timedelta(days=next_due_offset_days)).isoformat()
    return {
        "id": "abc",
        "name": "Test",
        "amount": amount,
        "frequency": frequency,
        "active": True,
        "next_due": next_due,
        "category": "Housing",
    }


def test_get_upcoming_monthly_appears_within_30_days():
    commitments = {"items": [_commitment(500.0, "monthly", next_due_offset_days=5)]}
    results = get_upcoming(commitments, days_ahead=30)
    assert len(results) >= 1
    assert results[0]["amount"] == 500.0


def test_get_upcoming_excludes_inactive():
    item = _commitment(500.0, "monthly", next_due_offset_days=5)
    item["active"] = False
    commitments = {"items": [item]}
    results = get_upcoming(commitments, days_ahead=30)
    assert results == []


def test_get_upcoming_weekly_generates_multiple_occurrences():
    commitments = {"items": [_commitment(50.0, "weekly", next_due_offset_days=1)]}
    results = get_upcoming(commitments, days_ahead=30)
    # Weekly over 30 days → ~4 occurrences
    assert len(results) >= 4


def test_get_upcoming_sorted_by_date():
    commitments = {"items": [
        _commitment(100.0, "monthly", next_due_offset_days=20),
        _commitment(50.0, "weekly", next_due_offset_days=2),
    ]}
    results = get_upcoming(commitments, days_ahead=30)
    dates = [r["projected_date"] for r in results]
    assert dates == sorted(dates)


def test_get_upcoming_empty_commitments():
    assert get_upcoming({"items": []}, days_ahead=90) == []


# ── load_commitments / save_commitments ───────────────────────────────────────

def test_load_commitments_returns_empty_when_file_absent(tmp_path):
    cfg = {"data": {"commitments_file": str(tmp_path / "commitments.json")}}
    result = load_commitments(cfg)
    assert result == {"items": []}


def test_save_and_load_round_trip(tmp_path):
    cfg = {"data": {"commitments_file": str(tmp_path / "commitments.json")}}
    data = {"items": [{"id": "abc123", "name": "Rent", "amount": 1500.0, "frequency": "monthly"}]}
    save_commitments(data, cfg)
    loaded = load_commitments(cfg)
    assert loaded["items"][0]["name"] == "Rent"
    assert loaded["items"][0]["amount"] == 1500.0


# ── merge_commitments ─────────────────────────────────────────────────────────

def test_merge_adds_new_items():
    existing = {"items": [{"id": "aaa", "name": "Rent"}]}
    detected = [{"id": "bbb", "name": "Electricity"}]
    merged = merge_commitments(existing, detected)
    assert len(merged["items"]) == 2


def test_merge_skips_existing_ids():
    existing = {"items": [{"id": "aaa", "name": "Rent"}]}
    detected = [{"id": "aaa", "name": "Rent Updated"}]
    merged = merge_commitments(existing, detected)
    assert len(merged["items"]) == 1
    assert merged["items"][0]["name"] == "Rent"  # original preserved
