"""Tests for src/loans.py — load/save, position calculation, unlinked detection."""
import json
import sqlite3

import pytest

from src.loans import (
    load_loans,
    save_loans,
    calculate_loan_position,
    find_unlinked_loan_transactions,
    get_loan_candidates,
    auto_link_transfer_pair,
    payoff_months,
    payoff_schedule,
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE transactions (
            txn_id      TEXT PRIMARY KEY,
            date        TEXT NOT NULL,
            description TEXT,
            amount      REAL,
            category    TEXT,
            account     TEXT
        )
    """)
    conn.commit()
    return conn


def _insert(conn, txn_id, date, description, amount, category, account="ANZ Personal"):
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
        (txn_id, date, description, amount, category, account),
    )
    conn.commit()


# ── load_loans / save_loans ───────────────────────────────────────────────────

def _cfg(tmp_path) -> dict:
    return {"data": {"loans_file": str(tmp_path / "loans.json")}}


def test_load_missing_file_returns_empty(tmp_path):
    result = load_loans(_cfg(tmp_path))
    assert result == {"loans": []}


def test_load_corrupt_file_returns_empty(tmp_path):
    (tmp_path / "loans.json").write_text("not json", encoding="utf-8")
    result = load_loans(_cfg(tmp_path))
    assert result == {"loans": []}


def test_save_and_load_roundtrip(tmp_path):
    data = {"loans": [{"id": "L1", "name": "Test Loan", "principal": 5000.0}]}
    save_loans(data, _cfg(tmp_path))
    loaded = load_loans(_cfg(tmp_path))
    assert loaded["loans"][0]["id"] == "L1"
    assert loaded["loans"][0]["principal"] == 5000.0


def test_save_creates_parent_directories(tmp_path):
    cfg = {"data": {"loans_file": str(tmp_path / "nested" / "dir" / "loans.json")}}
    save_loans({"loans": []}, cfg)
    assert (tmp_path / "nested" / "dir" / "loans.json").exists()


# ── calculate_loan_position ───────────────────────────────────────────────────

def test_no_category_filter_returns_principal_as_outstanding():
    conn = _make_conn()
    loan = {"principal": 1000.0, "category_filter": "", "description_filter": "", "start_date": "2024-01-01"}
    pos = calculate_loan_position(loan, conn)
    assert pos["outstanding"] == 1000.0
    assert pos["total_repaid"] == 0.0
    assert pos["pct"] == 0.0
    assert pos["status"] == "active"
    assert pos["repayments"] == []


def test_partial_repayment():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "Todd repayment", -300.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "Todd repayment", -200.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "todd",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 500.0
    assert pos["outstanding"] == 500.0
    assert pos["pct"] == 50.0
    assert pos["status"] == "active"
    assert pos["completed_date"] is None


def test_full_repayment_marks_complete():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "Todd repayment", -600.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-04-01", "Todd repayment", -400.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "todd",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["status"] == "complete"
    assert pos["outstanding"] == 0.0
    assert pos["pct"] == 100.0
    assert pos["completed_date"] == "2024-04-01"


def test_completed_date_is_last_repayment():
    conn = _make_conn()
    _insert(conn, "r1", "2024-03-01", "loan repay", -500.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-06-15", "loan repay", -500.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "loan repay",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["completed_date"] == "2024-06-15"


def test_overpayment_capped():
    """Outstanding never goes below 0 and pct never exceeds 100."""
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "repay", -1500.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "repay",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["outstanding"] == 0.0
    assert pos["pct"] == 100.0


def test_keyword_filter_excludes_non_matching_transactions():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "Todd repayment", -300.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "Sarah repayment", -200.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "todd",   # only matches Todd's row
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 300.0
    assert len(pos["repayments"]) == 1


def test_start_date_excludes_old_transactions():
    conn = _make_conn()
    _insert(conn, "r1", "2023-06-01", "repay", -200.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-02-01", "repay", -300.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "repay",
        "start_date": "2024-01-01",    # 2023 row excluded
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 300.0
    assert len(pos["repayments"]) == 1


def test_running_balance_decreases_per_repayment():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "repay", -250.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "repay", -250.0, "Family Loan Repayment")
    _insert(conn, "r3", "2024-04-01", "repay", -250.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "repay",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    balances = [r["balance"] for r in pos["repayments"]]
    assert balances == [750.0, 500.0, 250.0]


def test_zero_principal_no_crash():
    conn = _make_conn()
    loan = {"principal": 0, "category_filter": "Family Loan Repayment",
            "description_filter": "", "start_date": "2024-01-01"}
    pos = calculate_loan_position(loan, conn)
    assert pos["pct"] == 0.0
    assert pos["outstanding"] == 0.0


def test_no_description_filter_matches_all_in_category():
    """Empty description_filter matches all rows in the category."""
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "Todd repay",  -300.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "Sarah repay", -200.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "category_filter": "Family Loan Repayment",
        "description_filter": "",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 500.0
    assert len(pos["repayments"]) == 2


# ── find_unlinked_loan_transactions ──────────────────────────────────────────

def test_no_loans_all_unlinked():
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "TODD TRANSFER", -500.0, "Family Loan Repayment")
    _insert(conn, "t2", "2024-04-01", "SARAH TRANSFER", 2000.0, "Family Loan Received")
    result = find_unlinked_loan_transactions(conn, [])
    assert len(result) == 2


def test_description_filter_links_matching_transactions():
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "TODD JAMES REPAYMENT", -500.0, "Family Loan Repayment")
    _insert(conn, "t2", "2024-04-01", "SARAH LOAN", 2000.0, "Family Loan Received")
    loans = [{"description_filter": "todd james", "receipt_filter": "", "category_filter": "Family Loan Repayment"}]
    result = find_unlinked_loan_transactions(conn, loans)
    # Todd's row is linked, Sarah's is not
    txn_ids = [r["txn_id"] for r in result]
    assert "t1" not in txn_ids
    assert "t2" in txn_ids


def test_receipt_filter_links_transactions():
    conn = _make_conn()
    _insert(conn, "t1", "2024-04-01", "LOAN FROM MUM", 3000.0, "Family Loan Received")
    loans = [{"description_filter": "", "receipt_filter": "loan from mum", "category_filter": ""}]
    result = find_unlinked_loan_transactions(conn, loans)
    assert all(r["txn_id"] != "t1" for r in result)


def test_no_keyword_with_category_filter_links_by_category():
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "ANYTHING", -500.0, "Family Loan Repayment")
    loans = [{"description_filter": "", "receipt_filter": "", "category_filter": "Family Loan Repayment"}]
    result = find_unlinked_loan_transactions(conn, loans)
    assert all(r["txn_id"] != "t1" for r in result)


def test_non_loan_categories_not_returned():
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "WOOLWORTHS",  -85.0,  "Groceries")
    _insert(conn, "t2", "2024-03-02", "LOAN REPAY",  -200.0, "Family Loan Repayment")
    result = find_unlinked_loan_transactions(conn, [])
    txn_ids = [r["txn_id"] for r in result]
    assert "t1" not in txn_ids   # not a loan category
    assert "t2" in txn_ids


def test_unlinked_returns_dict_rows():
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "UNKNOWN LOAN", -500.0, "Family Loan Repayment")
    result = find_unlinked_loan_transactions(conn, [])
    assert result[0]["txn_id"] == "t1"
    assert result[0]["description"] == "UNKNOWN LOAN"
    assert result[0]["amount"] == -500.0


# ── calculate_loan_position — linked txn_id path ─────────────────────────────

def test_linked_ids_used_when_present():
    """linked_repayment_txn_ids takes precedence over keyword filter."""
    conn = _make_conn()
    _insert(conn, "linked1", "2024-02-01", "repay mum",  -300.0, "Family Loan Repayment")
    _insert(conn, "other1",  "2024-03-01", "repay mum",  -200.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "linked_repayment_txn_ids": ["linked1"],  # only this one is linked
        "category_filter": "Family Loan Repayment",
        "description_filter": "mum",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 300.0   # only linked1, not other1
    assert len(pos["repayments"]) == 1


def test_linked_ids_partial_repayment():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "pay mum", -400.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "pay mum", -400.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "linked_repayment_txn_ids": ["r1", "r2"],
        "category_filter": "",
        "description_filter": "",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 800.0
    assert pos["outstanding"] == 200.0
    assert pos["status"] == "active"


def test_linked_ids_full_repayment_complete():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "pay", -500.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "pay", -500.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "linked_repayment_txn_ids": ["r1", "r2"],
        "category_filter": "",
        "description_filter": "",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["status"] == "complete"
    assert pos["pct"] == 100.0
    assert pos["completed_date"] == "2024-03-01"


def test_empty_linked_ids_falls_back_to_keyword():
    """Empty list falls back to keyword filter (backward compat)."""
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "keyword match", -250.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "linked_repayment_txn_ids": [],
        "category_filter": "Family Loan Repayment",
        "description_filter": "keyword match",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert pos["total_repaid"] == 250.0


def test_running_balance_with_linked_ids():
    conn = _make_conn()
    _insert(conn, "r1", "2024-02-01", "pay", -250.0, "Family Loan Repayment")
    _insert(conn, "r2", "2024-03-01", "pay", -250.0, "Family Loan Repayment")
    loan = {
        "principal": 1000.0,
        "linked_repayment_txn_ids": ["r1", "r2"],
        "category_filter": "",
        "description_filter": "",
        "start_date": "2024-01-01",
    }
    pos = calculate_loan_position(loan, conn)
    assert [r["balance"] for r in pos["repayments"]] == [750.0, 500.0]


# ── get_loan_candidates ───────────────────────────────────────────────────────

_CONTACT_CONFIG = {
    "family_loans": {
        "contacts": [{
            "name": "Lesley Lindsay",
            "in_keywords":  ["LESLEY LINDSAY"],
            "out_keywords": ["TO MUM", "MUM"],
            "account": "ANZ Personal",
        }]
    }
}


def test_candidates_receipts_match_in_keywords():
    conn = _make_conn()
    _insert(conn, "r1", "2024-01-10",  "PAYMENT FROM LESLEY LINDSAY", 5000.0, "Family Loan Received")
    _insert(conn, "r2", "2024-01-12",  "WOOLWORTHS SUPERMARKETS",    -85.0,  "Groceries")
    result = get_loan_candidates("Lesley Lindsay", _CONTACT_CONFIG, conn)
    ids = [r["txn_id"] for r in result["receipts"]]
    assert "r1" in ids
    assert "r2" not in ids


def test_candidates_repayments_match_out_keywords():
    conn = _make_conn()
    _insert(conn, "p1", "2024-02-01", "ANZ PAYMENT TO MUM",    -500.0, "Family Loan Repayment")
    _insert(conn, "p2", "2024-02-15", "ANZ PAYMENT TO JOHN",   -100.0, "Miscellaneous")
    result = get_loan_candidates("Lesley Lindsay", _CONTACT_CONFIG, conn)
    ids = [r["txn_id"] for r in result["repayments"]]
    assert "p1" in ids
    assert "p2" not in ids


def test_candidates_positive_amounts_are_receipts_not_repayments():
    conn = _make_conn()
    _insert(conn, "r1", "2024-01-10", "LESLEY LINDSAY TRANSFER", 1000.0, "Family Loan Received")
    result = get_loan_candidates("Lesley Lindsay", _CONTACT_CONFIG, conn)
    assert any(r["txn_id"] == "r1" for r in result["receipts"])
    assert not any(r["txn_id"] == "r1" for r in result["repayments"])


def test_candidates_unknown_contact_falls_back_to_loan_categories():
    """Contact not in config → return all Family Loan Received/Repayment rows."""
    conn = _make_conn()
    _insert(conn, "t1", "2024-03-01", "SOME RANDOM PERSON", 2000.0, "Family Loan Received")
    _insert(conn, "t2", "2024-03-05", "SOME RANDOM PERSON", -500.0, "Family Loan Repayment")
    _insert(conn, "t3", "2024-03-06", "WOOLWORTHS",         -80.0,  "Groceries")
    result = get_loan_candidates("Unknown Person", _CONTACT_CONFIG, conn)
    all_ids = [r["txn_id"] for r in result["receipts"]] + [r["txn_id"] for r in result["repayments"]]
    assert "t1" in all_ids
    assert "t2" in all_ids
    assert "t3" not in all_ids


def test_candidates_empty_db_returns_empty_lists():
    conn = _make_conn()
    result = get_loan_candidates("Lesley Lindsay", _CONTACT_CONFIG, conn)
    assert result["receipts"] == []
    assert result["repayments"] == []


def test_limit_50_most_recent(tmp_path):
    """find_unlinked returns at most 50 rows, ordered most-recent first."""
    conn = _make_conn()
    for i in range(60):
        _insert(conn, f"t{i:03d}", f"2024-{(i % 12) + 1:02d}-01",
                "LOAN TXN", -10.0, "Family Loan Repayment")
    result = find_unlinked_loan_transactions(conn, [])
    assert len(result) <= 50


# ── auto_link_transfer_pair ───────────────────────────────────────────────────

def _loan_cfg(tmp_path):
    return {"data": {"loans_file": str(tmp_path / "loans.json")}}


def _write_loans(tmp_path, loans):
    import json
    (tmp_path / "loans.json").write_text(json.dumps({"loans": loans}), "utf-8")


def test_auto_link_no_loans_returns_false(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [])
    assert auto_link_transfer_pair("a1", "pay mum", "b1", "recv mum", cfg) is False


def test_auto_link_matches_description_filter(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [{
        "id": "L1", "principal": 1000,
        "description_filter": "mum",
        "receipt_filter": "",
        "linked_repayment_txn_ids": [],
        "linked_receipt_txn_ids": [],
    }])
    result = auto_link_transfer_pair("rep1", "PAY MUM REPAY", "rec1", "RECEIVED FROM MUM", cfg)
    assert result is True
    saved = load_loans(cfg)["loans"][0]
    assert "rep1" in saved["linked_repayment_txn_ids"]
    assert "rec1" in saved["linked_receipt_txn_ids"]


def test_auto_link_matches_receipt_filter(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [{
        "id": "L1", "principal": 500,
        "description_filter": "",
        "receipt_filter": "family transfer",
        "linked_repayment_txn_ids": [],
        "linked_receipt_txn_ids": [],
    }])
    result = auto_link_transfer_pair("a1", "DEBIT TRANSFER", "b1", "FAMILY TRANSFER IN", cfg)
    assert result is True
    saved = load_loans(cfg)["loans"][0]
    assert "a1" in saved["linked_repayment_txn_ids"]
    assert "b1" in saved["linked_receipt_txn_ids"]


def test_auto_link_no_keyword_match_returns_false(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [{
        "id": "L1", "principal": 1000,
        "description_filter": "mum",
        "receipt_filter": "",
        "linked_repayment_txn_ids": [],
        "linked_receipt_txn_ids": [],
    }])
    result = auto_link_transfer_pair("a1", "RENT PAYMENT", "b1", "RENT RECEIVED", cfg)
    assert result is False


def test_auto_link_ambiguous_multiple_matches_returns_false(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [
        {"id": "L1", "principal": 1000, "description_filter": "family", "receipt_filter": "",
         "linked_repayment_txn_ids": [], "linked_receipt_txn_ids": []},
        {"id": "L2", "principal": 500,  "description_filter": "family", "receipt_filter": "",
         "linked_repayment_txn_ids": [], "linked_receipt_txn_ids": []},
    ])
    result = auto_link_transfer_pair("a1", "FAMILY LOAN", "b1", "FAMILY LOAN RCV", cfg)
    assert result is False


def test_auto_link_deduplicates_existing_ids(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [{
        "id": "L1", "principal": 1000,
        "description_filter": "mum",
        "receipt_filter": "",
        "linked_repayment_txn_ids": ["rep1"],
        "linked_receipt_txn_ids": ["rec1"],
    }])
    result = auto_link_transfer_pair("rep1", "pay mum", "rec1", "recv mum", cfg)
    assert result is True
    saved = load_loans(cfg)["loans"][0]
    assert saved["linked_repayment_txn_ids"].count("rep1") == 1
    assert saved["linked_receipt_txn_ids"].count("rec1") == 1


def test_auto_link_no_keywords_on_loan_returns_false(tmp_path):
    cfg = _loan_cfg(tmp_path)
    _write_loans(tmp_path, [{
        "id": "L1", "principal": 1000,
        "description_filter": "",
        "receipt_filter": "",
        "linked_repayment_txn_ids": [],
        "linked_receipt_txn_ids": [],
    }])
    result = auto_link_transfer_pair("a1", "anything", "b1", "anything else", cfg)
    assert result is False


# ── payoff_months / payoff_schedule ──────────────────────────────────────────

def test_payoff_months_no_interest_exact():
    assert payoff_months(1200.0, 400.0, 0) == 3


def test_payoff_months_no_interest_rounds_up():
    assert payoff_months(1000.0, 300.0, 0) == 4  # 1000/300 = 3.33…


def test_payoff_months_with_interest():
    # $10 000 at 6% p.a., $300/month → should payoff in ~38 months
    months = payoff_months(10000.0, 300.0, 6.0)
    assert 36 <= months <= 40


def test_payoff_months_already_paid():
    assert payoff_months(0.0, 200.0, 5.0) == 0


def test_payoff_months_payment_too_small():
    # $10 000 at 12% p.a. = $100/month interest; paying $50 can't cover it
    assert payoff_months(10000.0, 50.0, 12.0) == -1


def test_payoff_schedule_zero_interest_length():
    schedule = payoff_schedule(900.0, 300.0, 0)
    assert len(schedule) == 3
    assert schedule[-1]["balance"] == 0.0


def test_payoff_schedule_with_interest_decreases():
    schedule = payoff_schedule(5000.0, 200.0, 5.0)
    balances = [s["balance"] for s in schedule]
    assert balances == sorted(balances, reverse=True)  # strictly decreasing
    assert balances[-1] == 0.0
