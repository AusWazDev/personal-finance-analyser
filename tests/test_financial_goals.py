"""Tests for src/financial_goals.py — goal balance auto-calculation."""
import sqlite3
import pytest
from src.financial_goals import calculate_goal_balance


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions ("
        "txn_id TEXT, date TEXT, description TEXT, amount REAL, account TEXT, category TEXT"
        ")"
    )
    conn.commit()
    return conn


def _insert(conn, txn_id, date, amount, account, category="Transfers"):
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
        (txn_id, date, "desc", amount, account, category),
    )
    conn.commit()


def test_no_account_returns_none():
    conn = _make_conn()
    goal = {"id": "g1", "created_date": "2025-01-01"}
    assert calculate_goal_balance(goal, conn) is None
    conn.close()


def test_returns_sum_of_credits_since_creation():
    conn = _make_conn()
    goal = {"id": "g1", "account": "ANZ Savings", "created_date": "2025-01-01"}
    _insert(conn, "t1", "2025-02-01",  500.00, "ANZ Savings")
    _insert(conn, "t2", "2025-03-01", 1000.00, "ANZ Savings")
    _insert(conn, "t3", "2025-03-15", -200.00, "ANZ Savings")  # debit — excluded
    result = calculate_goal_balance(goal, conn)
    assert result == 1500.00
    conn.close()


def test_excludes_transactions_before_creation_date():
    conn = _make_conn()
    goal = {"id": "g1", "account": "ANZ Savings", "created_date": "2025-06-01"}
    _insert(conn, "t1", "2025-01-01", 999.00, "ANZ Savings")  # before start — excluded
    _insert(conn, "t2", "2025-07-01", 300.00, "ANZ Savings")
    result = calculate_goal_balance(goal, conn)
    assert result == 300.00
    conn.close()


def test_empty_account_returns_zero():
    conn = _make_conn()
    goal = {"id": "g1", "account": "ANZ Savings", "created_date": "2025-01-01"}
    result = calculate_goal_balance(goal, conn)
    assert result == 0.0
    conn.close()


def test_different_accounts_not_included():
    conn = _make_conn()
    goal = {"id": "g1", "account": "ANZ Savings", "created_date": "2025-01-01"}
    _insert(conn, "t1", "2025-02-01", 500.00, "ANZ Savings")
    _insert(conn, "t2", "2025-02-01", 999.00, "ANZ Personal")  # different account — excluded
    result = calculate_goal_balance(goal, conn)
    assert result == 500.00
    conn.close()
