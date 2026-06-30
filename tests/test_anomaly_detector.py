"""Tests for src/anomaly_detector.py."""
import sqlite3
from datetime import date, timedelta

import pytest

import src.db as _db_mod
from src.anomaly_detector import detect_anomalies, anomaly_summary, IQR_MULTIPLIER


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _db_mod.init_db(conn)
    return conn


def _insert(conn, txn_id, dt, amount, category="Groceries"):
    conn.execute(
        "INSERT INTO transactions (txn_id, date, amount, account, category) VALUES (?,?,?,?,?)",
        (txn_id, dt, amount, "ANZ", category),
    )
    conn.commit()


# ── Baseline building ─────────────────────────────────────────────────────────

def test_flags_outlier_above_threshold():
    """A transaction 4× the median should be flagged."""
    conn = _conn()
    today = date.today()
    # 11 historical transactions at ~$50 each (baseline)
    prev_month = (today.replace(day=1) - timedelta(days=1))
    for i in range(11):
        _insert(conn, f"H{i}", prev_month.strftime("%Y-%m-%d"), -50.0)
    # One big transaction this month: $250 (5× the median of $50)
    _insert(conn, "BIG", today.strftime("%Y-%m-%d"), -250.0)
    newly = detect_anomalies(conn)
    assert "BIG" in newly


def test_normal_spend_not_flagged():
    """A transaction near the median should not be flagged."""
    conn = _conn()
    today = date.today()
    prev_month = (today.replace(day=1) - timedelta(days=1))
    for i in range(11):
        _insert(conn, f"H{i}", prev_month.strftime("%Y-%m-%d"), -50.0)
    _insert(conn, "NORM", today.strftime("%Y-%m-%d"), -55.0)
    newly = detect_anomalies(conn)
    assert "NORM" not in newly


def test_category_with_insufficient_history_never_flagged():
    """Category with < 3 historical transactions gets no threshold — no false positives."""
    conn = _conn()
    today = date.today()
    prev = (today.replace(day=1) - timedelta(days=1))
    # Only 2 historical transactions — insufficient baseline
    _insert(conn, "H1", prev.strftime("%Y-%m-%d"), -100.0, "Travel")
    _insert(conn, "H2", prev.strftime("%Y-%m-%d"), -110.0, "Travel")
    _insert(conn, "BIG", today.strftime("%Y-%m-%d"), -9000.0, "Travel")
    newly = detect_anomalies(conn)
    assert "BIG" not in newly


def test_transfers_excluded():
    """Transfers category is never flagged regardless of amount."""
    conn = _conn()
    today = date.today()
    prev = (today.replace(day=1) - timedelta(days=1))
    for i in range(11):
        _insert(conn, f"H{i}", prev.strftime("%Y-%m-%d"), -50.0, "Transfers")
    _insert(conn, "TRF", today.strftime("%Y-%m-%d"), -9000.0, "Transfers")
    newly = detect_anomalies(conn)
    assert "TRF" not in newly


def test_credits_excluded():
    """Credit transactions (positive amounts) are never flagged."""
    conn = _conn()
    today = date.today()
    prev = (today.replace(day=1) - timedelta(days=1))
    for i in range(11):
        _insert(conn, f"H{i}", prev.strftime("%Y-%m-%d"), -50.0)
    _insert(conn, "CRED", today.strftime("%Y-%m-%d"), 9000.0)  # credit
    newly = detect_anomalies(conn)
    assert "CRED" not in newly


def test_previously_flagged_cleared_when_below_threshold():
    """An already-flagged transaction is cleared if its amount drops below threshold."""
    conn = _conn()
    today = date.today()
    prev = (today.replace(day=1) - timedelta(days=1))
    for i in range(11):
        _insert(conn, f"H{i}", prev.strftime("%Y-%m-%d"), -50.0)
    # Mark a transaction as anomalous manually
    _insert(conn, "WAS_BIG", today.strftime("%Y-%m-%d"), -52.0)
    conn.execute("UPDATE transactions SET is_anomaly = 1 WHERE txn_id = 'WAS_BIG'")
    conn.commit()
    detect_anomalies(conn)
    row = conn.execute("SELECT is_anomaly FROM transactions WHERE txn_id = 'WAS_BIG'").fetchone()
    assert row["is_anomaly"] == 0


def test_anomaly_summary_returns_flagged():
    """anomaly_summary returns transactions with is_anomaly=1."""
    conn = _conn()
    today = date.today()
    _insert(conn, "A1", today.strftime("%Y-%m-%d"), -500.0)
    _insert(conn, "A2", today.strftime("%Y-%m-%d"), -10.0)
    conn.execute("UPDATE transactions SET is_anomaly = 1 WHERE txn_id = 'A1'")
    conn.commit()
    result = anomaly_summary(conn)
    txn_ids = [r["txn_id"] for r in result]
    assert "A1" in txn_ids
    assert "A2" not in txn_ids


def test_detect_anomalies_returns_list():
    """detect_anomalies always returns a list."""
    conn = _conn()
    result = detect_anomalies(conn)
    assert isinstance(result, list)
