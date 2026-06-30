"""Tests for src/reimbursement_tracker.py — batch status and JSON persistence."""
import pytest

from src.reimbursement_tracker import batch_status, load_batches, save_batches, suggest_reimbursement_batches


# ── batch_status ──────────────────────────────────────────────────────────────

def test_status_submitted_when_no_reimbursement_txn():
    batch = {"expense_total": 500.0}
    assert batch_status(batch) == "submitted"


def test_status_submitted_when_txn_id_is_empty_string():
    batch = {"reimbursement_txn_id": "", "expense_total": 500.0}
    assert batch_status(batch) == "submitted"


def test_status_reconciled_when_amounts_match_exactly():
    batch = {"reimbursement_txn_id": "txn123", "expense_total": 500.0, "received_amount": 500.0}
    assert batch_status(batch) == "reconciled"


def test_status_reconciled_within_two_cent_tolerance():
    batch = {"reimbursement_txn_id": "txn123", "expense_total": 100.00, "received_amount": 100.01}
    assert batch_status(batch) == "reconciled"


def test_status_shortfall_when_received_less_than_expected():
    batch = {"reimbursement_txn_id": "txn123", "expense_total": 500.0, "received_amount": 450.0}
    assert batch_status(batch) == "shortfall"


def test_status_overpaid_when_received_more_than_expected():
    batch = {"reimbursement_txn_id": "txn123", "expense_total": 200.0, "received_amount": 210.0}
    assert batch_status(batch) == "overpaid"


def test_status_handles_zero_amounts():
    batch = {"reimbursement_txn_id": "txn123", "expense_total": 0.0, "received_amount": 0.0}
    assert batch_status(batch) == "reconciled"


def test_status_handles_missing_amounts():
    batch = {"reimbursement_txn_id": "txn123"}
    assert batch_status(batch) == "reconciled"  # both default to 0 → match


# ── load_batches / save_batches ───────────────────────────────────────────────

def test_load_batches_returns_empty_when_file_absent(tmp_path):
    cfg = {"data": {"reimbursement_batches_file": str(tmp_path / "batches.json")}}
    result = load_batches(cfg)
    assert result == {"batches": []}


def test_save_and_load_round_trip(tmp_path):
    cfg = {"data": {"reimbursement_batches_file": str(tmp_path / "batches.json")}}
    data = {
        "batches": [
            {
                "id": "batch001",
                "expense_txn_ids": ["txn1", "txn2"],
                "expense_total": 350.0,
                "submitted_date": "2025-10-01",
                "reimbursement_txn_id": None,
            }
        ]
    }
    save_batches(data, cfg)
    loaded = load_batches(cfg)
    assert len(loaded["batches"]) == 1
    assert loaded["batches"][0]["id"] == "batch001"
    assert loaded["batches"][0]["expense_total"] == 350.0


def test_save_creates_parent_directories(tmp_path):
    nested = tmp_path / "deep" / "nested"
    cfg = {"data": {"reimbursement_batches_file": str(nested / "batches.json")}}
    save_batches({"batches": []}, cfg)
    assert (nested / "batches.json").exists()


def test_load_batches_uses_default_key_when_missing_from_config(tmp_path, monkeypatch):
    """load_batches falls back to 'data/reimbursement_batches.json' when key absent."""
    # Just confirm it doesn't crash with an empty config
    result = load_batches({})
    assert "batches" in result


# ── suggest_reimbursement_batches ─────────────────────────────────────────────

def _exp(txn_id, date, amount=-100.0):
    return {"txn_id": txn_id, "date": date, "description": f"EXP {txn_id}", "amount": amount}


def _cred(txn_id, date, amount=100.0):
    return {"txn_id": txn_id, "date": date, "description": "EMPLOYER PAY", "amount": amount}


def test_suggest_empty_expenses_returns_empty():
    result = suggest_reimbursement_batches([], [_cred("C1", "2025-10-31")])
    assert result == []


def test_suggest_no_credits_returns_unmatched():
    expenses = [_exp("E1", "2025-10-01")]
    result = suggest_reimbursement_batches(expenses, [])
    assert len(result) == 1
    assert result[0]["match_quality"] == "unmatched"
    assert result[0]["payment_txn_id"] is None


def test_suggest_exact_match():
    expenses = [_exp("E1", "2025-10-01", -100.0)]
    credits  = [_cred("C1", "2025-10-31", 100.0)]
    result = suggest_reimbursement_batches(expenses, credits)
    assert len(result) == 1
    s = result[0]
    assert s["match_quality"] == "exact"
    assert s["payment_txn_id"] == "C1"
    assert s["expense_txn_ids"] == ["E1"]
    assert s["expense_total"] == 100.0


def test_suggest_near_match():
    expenses = [_exp("E1", "2025-10-01", -100.0)]
    credits  = [_cred("C1", "2025-10-31", 96.0)]  # diff=4 > tol, but 4/96=4.2% < near_pct=5%
    result = suggest_reimbursement_batches(expenses, credits, exact_tol=1.0, near_pct=0.05)
    assert result[0]["match_quality"] == "near"


def test_suggest_partial_match():
    expenses = [_exp("E1", "2025-10-01", -100.0)]
    credits  = [_cred("C1", "2025-10-31", 50.0)]  # diff=50, way outside tolerances
    result = suggest_reimbursement_batches(expenses, credits)
    assert result[0]["match_quality"] == "partial"


def test_suggest_period_based_multiple_credits():
    """Each credit claims only expenses within its own period."""
    expenses = [
        _exp("E1", "2025-09-15", -50.0),
        _exp("E2", "2025-10-20", -60.0),
    ]
    credits = [
        _cred("C1", "2025-09-30", 50.0),
        _cred("C2", "2025-10-31", 60.0),
    ]
    result = suggest_reimbursement_batches(expenses, credits)
    assert len(result) == 2
    assert result[0]["payment_txn_id"] == "C1"
    assert result[0]["expense_txn_ids"] == ["E1"]
    assert result[1]["payment_txn_id"] == "C2"
    assert result[1]["expense_txn_ids"] == ["E2"]


def test_suggest_unmatched_remainder_appended():
    """Expenses after the last credit appear as an unmatched entry."""
    expenses = [
        _exp("E1", "2025-10-01", -100.0),
        _exp("E2", "2025-11-15", -80.0),  # after the only credit
    ]
    credits = [_cred("C1", "2025-10-31", 100.0)]
    result = suggest_reimbursement_batches(expenses, credits)
    assert len(result) == 2
    assert result[0]["match_quality"] in ("exact", "near", "partial")
    assert result[1]["match_quality"] == "unmatched"
    assert result[1]["expense_txn_ids"] == ["E2"]
    assert result[1]["expense_total"] == 80.0
