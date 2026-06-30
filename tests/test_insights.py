"""Tests for src/insights.py — rule-based financial insight engine."""

import sqlite3
from datetime import date, timedelta

import pytest

from src.insights import (
    _spend_spikes,
    _new_merchants,
    _large_transaction,
    _weekend_pattern,
    compute_insights,
    _ym,
)
from src.db import init_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _insert(conn, date_str, amount, description="MERCHANT", category="Dining", account="ANZ"):
    conn.execute(
        """INSERT OR IGNORE INTO transactions
           (txn_id, date, amount, description, category, account)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (f"T{date_str}{description[:4]}{amount}", date_str, amount, description, category, account),
    )
    conn.commit()


def _month_date(months_back: int, day: int = 15) -> str:
    today = date.today()
    m, y = today.month - months_back, today.year
    while m <= 0:
        m += 12
        y -= 1
    import calendar
    day = min(day, calendar.monthrange(y, m)[1])
    return f"{y}-{m:02d}-{day:02d}"


# ── _spend_spikes ─────────────────────────────────────────────────────────────

def test_spend_spike_detected():
    conn = _fresh_conn()
    # Avg $100/mo over last 3 months
    for mo in range(1, 4):
        _insert(conn, _month_date(mo), -100.0, "SUPERMARKET", "Groceries")
    # Current month: $160 — 60% above average
    _insert(conn, _month_date(0), -160.0, "SUPERMARKET", "Groceries")
    insights = _spend_spikes(conn, _ym(0))
    assert len(insights) == 1
    assert insights[0]["type"] == "spend_spike"
    assert "Groceries" in insights[0]["title"]
    assert "60%" in insights[0]["title"]


def test_spend_spike_not_triggered_below_threshold():
    conn = _fresh_conn()
    for mo in range(1, 4):
        _insert(conn, _month_date(mo), -100.0, "SUPERMARKET", "Groceries")
    _insert(conn, _month_date(0), -125.0, "SUPERMARKET", "Groceries")  # 25% above — below 30%
    insights = _spend_spikes(conn, _ym(0))
    assert insights == []


def test_spend_spike_ignores_small_amounts():
    conn = _fresh_conn()
    for mo in range(1, 4):
        _insert(conn, _month_date(mo), -10.0, "COFFEE", "Dining")
    _insert(conn, _month_date(0), -20.0, "COFFEE", "Dining")  # 100% spike but only $20
    insights = _spend_spikes(conn, _ym(0))
    assert insights == []


def test_spend_spike_excludes_transfers():
    conn = _fresh_conn()
    for mo in range(1, 4):
        _insert(conn, _month_date(mo), -200.0, "TRANSFER", "Transfers")
    _insert(conn, _month_date(0), -600.0, "TRANSFER", "Transfers")
    insights = _spend_spikes(conn, _ym(0))
    assert insights == []


def test_spend_spike_capped_at_three():
    conn = _fresh_conn()
    cats = ["Dining", "Groceries", "Entertainment", "Health", "Utilities"]
    for cat in cats:
        for mo in range(1, 4):
            _insert(conn, _month_date(mo), -100.0, cat.upper(), cat)
        _insert(conn, _month_date(0), -300.0, cat.upper(), cat)
    insights = _spend_spikes(conn, _ym(0))
    assert len(insights) <= 3


# ── _new_merchants ────────────────────────────────────────────────────────────

def test_new_merchant_detected():
    conn = _fresh_conn()
    _insert(conn, _month_date(0), -50.0, "BRAND NEW PLACE", "Dining")
    insights = _new_merchants(conn, _ym(0))
    assert len(insights) == 1
    assert insights[0]["type"] == "new_merchant"
    assert "BRAND NEW PLACE" in insights[0]["title"]


def test_new_merchant_not_if_seen_before():
    conn = _fresh_conn()
    _insert(conn, _month_date(1), -50.0, "OLD CAFE", "Dining")
    _insert(conn, _month_date(0), -55.0, "OLD CAFE", "Dining")
    insights = _new_merchants(conn, _ym(0))
    assert insights == []


def test_new_merchant_excludes_transfers():
    conn = _fresh_conn()
    _insert(conn, _month_date(0), -100.0, "INTERNAL TRANSFER", "Transfers")
    insights = _new_merchants(conn, _ym(0))
    assert insights == []


# ── _large_transaction ────────────────────────────────────────────────────────

def test_large_transaction_detected():
    conn = _fresh_conn()
    _insert(conn, _month_date(0), -500.0, "DENTIST", "Health")
    insights = _large_transaction(conn, _ym(0))
    assert len(insights) == 1
    assert insights[0]["type"] == "large_txn"
    assert "500" in insights[0]["title"]


def test_large_transaction_below_threshold():
    conn = _fresh_conn()
    _insert(conn, _month_date(0), -50.0, "COFFEE", "Dining")
    insights = _large_transaction(conn, _ym(0))
    assert insights == []


def test_large_transaction_excludes_transfers():
    conn = _fresh_conn()
    _insert(conn, _month_date(0), -5000.0, "RENT TRANSFER", "Transfers")
    _insert(conn, _month_date(0), -200.0, "DENTIST", "Health")
    insights = _large_transaction(conn, _ym(0))
    assert len(insights) == 1
    assert "200" in insights[0]["title"]


# ── _weekend_pattern ──────────────────────────────────────────────────────────

def _next_weekday_in_month(months_back: int, target_weekday: int) -> str:
    """Return first date in target month matching target_weekday (0=Mon, 5=Sat)."""
    today = date.today()
    m, y = today.month - months_back, today.year
    while m <= 0:
        m += 12
        y -= 1
    d = date(y, m, 1)
    import calendar
    last_day = calendar.monthrange(y, m)[1]
    for day in range(1, last_day + 1):
        candidate = date(y, m, day)
        if candidate.weekday() == target_weekday:
            return candidate.isoformat()
    return date(y, m, 1).isoformat()


def test_weekend_pattern_detected():
    conn = _fresh_conn()
    sat = _next_weekday_in_month(0, 5)
    mon = _next_weekday_in_month(0, 0)
    tue = _next_weekday_in_month(0, 1)
    # Weekend: $200 in one day; weekdays: $30 each over 2 days
    _insert(conn, sat, -200.0, "BAR", "Dining")
    _insert(conn, mon, -30.0, "LUNCH", "Dining")
    _insert(conn, tue, -30.0, "LUNCH", "Dining")
    insights = _weekend_pattern(conn, _ym(0))
    assert len(insights) == 1
    assert insights[0]["type"] == "weekend_pattern"


def test_weekend_pattern_not_triggered_below_ratio():
    conn = _fresh_conn()
    sat = _next_weekday_in_month(0, 5)
    mon = _next_weekday_in_month(0, 0)
    _insert(conn, sat, -100.0, "SHOP", "Shopping")
    _insert(conn, mon, -90.0, "SHOP", "Shopping")  # ratio ~1.1 — below 2×
    insights = _weekend_pattern(conn, _ym(0))
    assert insights == []


# ── compute_insights ──────────────────────────────────────────────────────────

def test_compute_insights_empty_db():
    conn = _fresh_conn()
    results = compute_insights(conn, {})
    assert results == []


def test_compute_insights_capped_at_ten():
    conn = _fresh_conn()
    # Create 15 different categories with spend spikes
    cats = [f"Cat{i}" for i in range(15)]
    for cat in cats:
        for mo in range(1, 4):
            _insert(conn, _month_date(mo), -100.0, cat.upper(), cat)
        _insert(conn, _month_date(0), -300.0, cat.upper(), cat)
    results = compute_insights(conn, {})
    assert len(results) <= 10
