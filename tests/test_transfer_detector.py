"""Tests for src/transfer_detector.py — pair finding, merge, and persistence."""
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from src.transfer_detector import (
    find_transfer_pairs,
    detect_family_loan_pairs,
    merge_candidates,
    load_transfer_candidates,
    save_transfer_candidates,
    score_pairs_with_ai,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(txn_id, date, amount, category="Miscellaneous", description="ANZ MOBILE BANKING PAYMENT", account="ANZ"):
    return {
        "txn_id": txn_id,
        "date": pd.Timestamp(date),
        "amount": float(amount),
        "category": category,
        "description": description,
        "account": account,
    }


def _df(*rows):
    return pd.DataFrame(rows)


# ── find_transfer_pairs ───────────────────────────────────────────────────────

def test_finds_matching_debit_credit_pair():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Personal"),
        _row("credit1", "2025-10-05", 500.00, account="ANZ Plus Everyday"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert len(pairs) == 1
    assert pairs[0]["amount"] == 500.00
    assert pairs[0]["days_apart"] == 4


def test_no_pair_when_amounts_differ():
    df = _df(
        _row("debit1", "2025-10-01", -500.00),
        _row("credit1", "2025-10-05", 450.00),  # different amount
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_no_pair_when_outside_window():
    df = _df(
        _row("debit1", "2025-10-01", -500.00),
        _row("credit1", "2025-12-10", 500.00),  # 70 days — outside 60-day window
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_pair_within_window():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Personal"),
        _row("credit1", "2025-11-25", 500.00, account="ANZ Plus Everyday"),  # 55 days — inside 60-day window
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert len(pairs) == 1


def test_skips_income_category():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, category="Income"),
        _row("credit1", "2025-10-05", 500.00, category="Income"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_skips_transfers_category():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, category="Transfers"),
        _row("credit1", "2025-10-05", 500.00, category="Transfers"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_skips_amounts_below_ten():
    df = _df(
        _row("debit1", "2025-10-01", -9.00),
        _row("credit1", "2025-10-05", 9.00),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_no_duplicate_pairs():
    # Same pair should appear only once even if matched from both sides
    df = _df(
        _row("debit1", "2025-10-01", -200.00, account="ANZ Personal"),
        _row("credit1", "2025-10-03", 200.00, account="ANZ Plus Everyday"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert len(pairs) == 1


def test_pair_has_expected_structure():
    df = _df(
        _row("debit1", "2025-10-01", -300.00, description="Transfer out", account="ANZ Personal"),
        _row("credit1", "2025-10-02", 300.00, description="Transfer in", account="Other Bank"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    p = pairs[0]
    assert "pair_id" in p
    assert "txn_a" in p and "txn_b" in p
    assert p["status"] == "pending"
    assert p["ai_confidence"] is None


def test_no_pair_when_same_account():
    # Equal-and-opposite amounts in the same account must not be paired
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Personal"),
        _row("credit1", "2025-10-05", 500.00, account="ANZ Personal"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_empty_df_returns_empty():
    assert find_transfer_pairs(pd.DataFrame()) == []


def test_skips_gifts_received_category():
    df = _df(
        _row("debit1", "2025-10-01", -100.00, category="Miscellaneous", account="ANZ Personal"),
        _row("credit1", "2025-10-05", 100.00, category="Gifts Received", account="ANZ Plus Everyday"),
    )
    pairs = find_transfer_pairs(df, window_days=60)
    assert pairs == []


def test_payment_to_person_generates_no_pair():
    """Payment to an external person (TO MUM) generates no pair — MUM's account
    is not in own_accounts so her receipt never appears in the credit pool."""
    df = _df(
        _row("mum_payment", "2025-10-01", -3500.00,
             description="ANZ MOBILE BANKING PAYMENT 077543 TO MUM", account="ANZ Personal"),
    )
    own_cfg = {"own_accounts": ["ANZ Personal", "ANZ Plus Everyday"]}
    pairs = find_transfer_pairs(df, window_days=60, config=own_cfg)
    assert pairs == []


def test_credit_not_in_own_accounts_excluded():
    """A credit landing in an account not in own_accounts must not form a pair."""
    df = _df(
        _row("debit1",  "2025-10-01", -200.00, account="ANZ Personal"),
        _row("credit1", "2025-10-02",  200.00, account="External Bank"),
    )
    own_cfg = {"own_accounts": ["ANZ Personal", "ANZ Plus Everyday"]}
    pairs = find_transfer_pairs(df, window_days=60, config=own_cfg)
    assert pairs == []


def test_user_scenario_transfer_then_purchase():
    """Genuine transfer pairs correctly; same-amount purchase two days later generates
    no pair — the purchase debit is in own_accounts but its only candidate credit is
    in the same account (excluded by same-account rule)."""
    df = _df(
        _row("transfer_out", "2025-11-01", -200.00, account="ANZ Personal"),
        _row("transfer_in",  "2025-11-01",  200.00, account="ANZ Plus Everyday"),
        _row("purchase",     "2025-11-03", -200.00, description="WOOLWORTHS 1234",
             account="ANZ Plus Everyday"),
    )
    own_cfg = {"own_accounts": ["ANZ Personal", "ANZ Plus Everyday"]}
    pairs = find_transfer_pairs(df, window_days=60, config=own_cfg)
    # Only one pair: the transfer. The purchase debit finds no credit in a
    # *different* own account (transfer_in is in the same account as the purchase).
    assert len(pairs) == 1
    assert pairs[0]["txn_a"]["txn_id"] == "transfer_out"
    assert pairs[0]["txn_b"]["txn_id"] == "transfer_in"
    assert pairs[0]["status"] == "confirmed"


# ── Scenario 1: own-account auto-confirm ──────────────────────────────────────

_OWN_CONFIG = {"own_accounts": ["ANZ Personal", "ANZ Plus Everyday"]}


def test_own_account_pair_auto_confirmed():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Plus Everyday"),
        _row("credit1", "2025-10-01", 500.00, account="ANZ Personal"),
    )
    pairs = find_transfer_pairs(df, window_days=60, config=_OWN_CONFIG)
    assert len(pairs) == 1
    assert pairs[0]["status"] == "confirmed"
    assert pairs[0]["label"] == "Internal Transfer"
    assert pairs[0]["ai_confidence"] == 10


def test_own_account_far_apart_stays_pending():
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Plus Everyday"),
        _row("credit1", "2025-10-10", 500.00, account="ANZ Personal"),  # 9 days apart
    )
    pairs = find_transfer_pairs(df, window_days=60, config=_OWN_CONFIG)
    assert len(pairs) == 1
    assert pairs[0]["status"] == "pending"


def test_own_account_pair_auto_confirmed_despite_skip_cat():
    """A ≤3-day own-account pair is auto-confirmed even if one side carries a
    _SKIP_CATS category (e.g. miscategorised as Family Loan Repayment).
    Account membership + amount + date is a stronger signal than the category."""
    df = _df(
        _row("debit1",  "2025-10-01", -500.00, category="Family Loan Repayment", account="ANZ Personal"),
        _row("credit1", "2025-10-02",  500.00, category="Miscellaneous",         account="ANZ Plus Everyday"),
    )
    pairs = find_transfer_pairs(df, window_days=60, config=_OWN_CONFIG)
    assert len(pairs) == 1
    assert pairs[0]["status"] == "confirmed"
    assert pairs[0]["label"] == "Internal Transfer"


def test_already_confirmed_transfers_not_redetected():
    """A pair where either side is already category='Transfers' is skipped —
    it was processed in a previous run and must not be double-counted."""
    df = _df(
        _row("debit1",  "2025-10-01", -500.00, category="Transfers", account="ANZ Personal"),
        _row("credit1", "2025-10-01",  500.00, category="Transfers", account="ANZ Plus Everyday"),
    )
    pairs = find_transfer_pairs(df, window_days=60, config=_OWN_CONFIG)
    assert pairs == []


def test_cross_bank_credit_generates_no_pair():
    """A credit in an account outside own_accounts generates no pair at all —
    transfers are only between the user's own accounts."""
    df = _df(
        _row("debit1", "2025-10-01", -500.00, account="ANZ Personal"),
        _row("credit1", "2025-10-01", 500.00, account="Other Bank"),
    )
    pairs = find_transfer_pairs(df, window_days=60, config=_OWN_CONFIG)
    assert pairs == []


# ── Scenario 2: family loan detection ────────────────────────────────────────

_LOAN_CONFIG = {
    "family_loans": {
        "contacts": [{
            "name": "Test Lender",
            "in_keywords": ["PAYMENT FROM TEST LENDER"],
            "out_keywords": ["TO Test Lender"],
            "account": "ANZ Personal",
            "window_days": 90,
        }]
    }
}


def test_family_loan_balanced_auto_confirmed():
    df = _df(
        _row("loan_in",  "2025-10-01", 1000.00, description="PAYMENT FROM TEST LENDER", account="ANZ Personal"),
        _row("loan_out", "2025-11-01", -1000.00, description="Payment TO Test Lender",  account="ANZ Personal"),
    )
    pairs = detect_family_loan_pairs(df, _LOAN_CONFIG)
    assert len(pairs) == 1
    assert pairs[0]["status"] == "confirmed"
    assert pairs[0]["label"] == "Family Loan"


def test_family_loan_unbalanced_stays_pending():
    df = _df(
        _row("loan_in1", "2025-10-01", 1000.00, description="PAYMENT FROM TEST LENDER", account="ANZ Personal"),
        _row("loan_in2", "2025-10-02",  500.00, description="PAYMENT FROM TEST LENDER", account="ANZ Personal"),
        _row("loan_out", "2025-11-01", -500.00, description="Payment TO Test Lender",   account="ANZ Personal"),
    )
    pairs = detect_family_loan_pairs(df, _LOAN_CONFIG)
    assert all(p["status"] == "pending" for p in pairs)


def test_family_loan_no_out_transactions():
    df = _df(
        _row("loan_in", "2025-10-01", 1000.00, description="PAYMENT FROM TEST LENDER", account="ANZ Personal"),
    )
    pairs = detect_family_loan_pairs(df, _LOAN_CONFIG)
    assert pairs == []


def test_family_loan_outside_window():
    df = _df(
        _row("loan_in",  "2025-01-01", 1000.00, description="PAYMENT FROM TEST LENDER", account="ANZ Personal"),
        _row("loan_out", "2025-10-01", -1000.00, description="Payment TO Test Lender",  account="ANZ Personal"),
    )
    # 272 days apart, window is 90
    pairs = detect_family_loan_pairs(df, _LOAN_CONFIG)
    assert pairs == []


# ── merge_candidates ──────────────────────────────────────────────────────────

def test_merge_adds_new_pairs():
    existing = {"pairs": [{"pair_id": "aaa|bbb", "amount": 100.0}]}
    new = [{"pair_id": "ccc|ddd", "amount": 200.0}]
    result = merge_candidates(existing, new)
    assert len(result["pairs"]) == 2


def test_merge_skips_existing_pair_ids():
    existing = {"pairs": [{"pair_id": "aaa|bbb", "amount": 100.0, "status": "pending"}]}
    new = [{"pair_id": "aaa|bbb", "amount": 999.0, "status": "pending"}]  # same pair_id, still pending
    result = merge_candidates(existing, new)
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["amount"] == 100.0  # original amount preserved


def test_merge_upgrades_pending_to_confirmed():
    existing = {"pairs": [{"pair_id": "aaa|bbb", "amount": 100.0, "status": "pending", "label": "Family Loan", "ai_confidence": None, "ai_note": ""}]}
    new = [{"pair_id": "aaa|bbb", "amount": 100.0, "status": "confirmed", "label": "Internal Transfer", "ai_confidence": 10, "ai_note": "auto"}]
    result = merge_candidates(existing, new)
    assert result["pairs"][0]["status"] == "confirmed"
    assert result["pairs"][0]["label"] == "Internal Transfer"


def test_merge_empty_existing():
    result = merge_candidates({"pairs": []}, [{"pair_id": "x|y", "amount": 50.0}])
    assert len(result["pairs"]) == 1


# ── load / save ───────────────────────────────────────────────────────────────

def test_load_returns_empty_when_file_absent(tmp_path):
    cfg = {"data": {"transfer_candidates_file": str(tmp_path / "candidates.json")}}
    result = load_transfer_candidates(cfg)
    assert result == {"pairs": []}


def test_save_and_load_round_trip(tmp_path):
    cfg = {"data": {"transfer_candidates_file": str(tmp_path / "candidates.json")}}
    data = {"pairs": [{"pair_id": "aaa|bbb", "amount": 150.0, "status": "confirmed"}]}
    save_transfer_candidates(data, cfg)
    loaded = load_transfer_candidates(cfg)
    assert loaded["pairs"][0]["pair_id"] == "aaa|bbb"
    assert loaded["pairs"][0]["amount"] == 150.0


def test_score_pairs_with_ai():
    mock_content = MagicMock()
    mock_content.text = '{"confidence": 8, "note": "Looks like a reimbursement.", "label": "Reimbursement"}'
    mock_msg = MagicMock()
    mock_msg.content = [mock_content]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    pair = {
        "pair_id": "aaa|bbb",
        "status": "pending",
        "amount": 100.0,
        "days_apart": 5,
        "ai_confidence": None,
        "ai_note": "",
        "label": "Family Loan",
        "txn_a": {"txn_id": "aaa", "date": "2025-01-15", "description": "TRANSFER OUT",
                  "account": "ANZ Personal", "category": "Miscellaneous", "amount": -100.0},
        "txn_b": {"txn_id": "bbb", "date": "2025-01-20", "description": "TRANSFER IN",
                  "account": "ANZ ETrade", "category": "Miscellaneous", "amount": 100.0},
    }
    config = {"anthropic_api_key": "sk-test-fake"}

    with patch("anthropic.Anthropic", return_value=mock_client):
        result = score_pairs_with_ai([pair], config)

    assert result[0]["ai_confidence"] == 8
    assert result[0]["label"] == "Reimbursement"
    assert "reimbursement" in result[0]["ai_note"].lower()
