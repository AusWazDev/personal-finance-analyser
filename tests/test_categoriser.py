"""
Tests for src/categoriser.py — rule priority and override logic.

Run with:  python -m pytest tests/ -v
"""

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.categoriser import categorise_transactions, categorise_payin4_merchants


# ── Minimal config ────────────────────────────────────────────────────────────

BASE_CONFIG = {
    "data": {
        "cache_file": ":memory:",       # will fail to load (no file) → empty cache
        "overrides_file": ":memory:",
    },
    "income": {
        "account_holder_name": "LINDSAY WARWICK",
        "trust_keywords": ["WARWICK JOHN LINDSAY", "FAMILY TRUST"],
        "income_keywords": ["SALARY", "WAGES", "DISTRIBUTION"],
        "known_income_payers": [],
    },
    "board_income": {
        "payers": ["TODD JAMES ROBERT LINDSA"],
        "note_keywords": ["Board", "board"],
    },
    "business": {
        "company_name": "BEDLIN",
        "reimbursement_keywords": ["REIMBURSEMENT"],
        "expense_keywords": ["ASIC", "WEBCENTRAL"],
        "known_business_merchants": [],
    },
    "merchant_categories": {
        "WOOLWORTHS": "Groceries",
        "NETFLIX": "Subscriptions",
    },
    "models": {"categoriser": "claude-haiku-4-5-20251001"},
}


def _df(rows: list[dict]) -> pd.DataFrame:
    defaults = {
        "description": "TEST",
        "amount": -10.0,
        "account_type": "transaction",
        "note": "",
        "payee_name": "",
        "txn_id": "abc123",
    }
    records = [{**defaults, **r} for r in rows]
    df = pd.DataFrame(records)
    df["date"] = pd.Timestamp("2025-03-01")
    return df


# ── Account-type shortcuts ────────────────────────────────────────────────────

def test_savings_account_becomes_transfers():
    df = _df([{"description": "SAVINGS DEPOSIT", "amount": 500.0, "account_type": "savings"}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Transfers"


def test_investment_account_becomes_investment():
    df = _df([{"description": "CMC MARKETS", "amount": -1000.0, "account_type": "investment"}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Investment"


# ── Income rules ──────────────────────────────────────────────────────────────

def test_trust_keyword_credit_is_income():
    df = _df([{"description": "WARWICK JOHN LINDSAY DISTRIBUTION", "amount": 5000.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Income"


def test_income_keyword_in_description():
    df = _df([{"description": "SALARY PAYMENT", "amount": 3000.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Income"


def test_debit_with_trust_keyword_not_income():
    """Trust keyword in a debit should NOT become Income."""
    df = _df([{"description": "WARWICK JOHN LINDSAY", "amount": -100.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] != "Income"


# ── Board income ──────────────────────────────────────────────────────────────

def test_board_income_payer_with_note():
    df = _df([{
        "description": "TODD JAMES ROBERT LINDSA PAYMENT",
        "amount": 800.0,
        "note": "Board payment March",
    }])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Board & Lodging"


def test_board_income_payer_wrong_note_still_matches():
    """Note keywords are a filter; if no note_keywords configured it matches on name alone."""
    cfg = {**BASE_CONFIG, "board_income": {"payers": ["TODD JAMES ROBERT LINDSA"], "note_keywords": []}}
    df = _df([{"description": "TODD JAMES ROBERT LINDSA", "amount": 800.0, "note": ""}])
    result = categorise_transactions(df, cfg, use_api=False)
    assert result["category"].iloc[0] == "Board & Lodging"


# ── Merchant override ─────────────────────────────────────────────────────────

def test_config_merchant_override():
    df = _df([{"description": "WOOLWORTHS SUPERMARKET", "amount": -85.50}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Groceries"


def test_config_merchant_override_case_insensitive():
    df = _df([{"description": "netflix.com annual subscription", "amount": -99.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Subscriptions"


# ── Business expense override ─────────────────────────────────────────────────

def test_business_keyword_sets_is_business():
    df = _df([{"description": "ASIC ANNUAL FEE", "amount": -290.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["is_business"].iloc[0] == True
    assert result["category"].iloc[0] == "Business Expense"


# ── txn_id override (highest priority) ───────────────────────────────────────

def test_txn_override_beats_merchant_config(tmp_path):
    """A txn_id override should win over the merchant_categories config."""
    import json, os
    overrides = {"TXN-001": {"category": "Gifts"}}
    override_file = tmp_path / "overrides.json"
    override_file.write_text(json.dumps(overrides))

    cfg = {**BASE_CONFIG, "data": {
        **BASE_CONFIG["data"],
        "overrides_file": str(override_file),
    }}
    df = _df([{"description": "WOOLWORTHS SUPERMARKET", "amount": -85.50, "txn_id": "TXN-001"}])
    result = categorise_transactions(df, cfg, use_api=False)
    # Override should beat the Groceries merchant_categories entry
    assert result["category"].iloc[0] == "Gifts"


def test_txn_override_only_applies_to_matching_id(tmp_path):
    import json
    overrides = {"TXN-001": {"category": "Gifts"}}
    override_file = tmp_path / "overrides.json"
    override_file.write_text(json.dumps(overrides))

    cfg = {**BASE_CONFIG, "data": {
        **BASE_CONFIG["data"],
        "overrides_file": str(override_file),
    }}
    df = _df([
        {"description": "WOOLWORTHS SUPERMARKET", "amount": -85.50, "txn_id": "TXN-001"},
        {"description": "WOOLWORTHS SUPERMARKET", "amount": -62.00, "txn_id": "TXN-002"},
    ])
    result = categorise_transactions(df, cfg, use_api=False)
    assert result["category"].iloc[0] == "Gifts"
    assert result["category"].iloc[1] == "Groceries"


# ── No API fallback ───────────────────────────────────────────────────────────

def test_no_api_uncached_becomes_miscellaneous():
    df = _df([{"description": "UNKNOWN MERCHANT XYZ 999", "amount": -55.0}])
    result = categorise_transactions(df, BASE_CONFIG, use_api=False)
    assert result["category"].iloc[0] == "Miscellaneous"


# ── categorise_payin4_merchants ───────────────────────────────────────────────

def _p4_group(group_id: str, merchant: str, anz_txn_ids: list[str], merchant_category: str = "") -> dict:
    return {
        "group_id": group_id,
        "merchant": merchant,
        "total_amount": 100.0,
        "purchase_date": "2025-03-01",
        "purchase_txn_id": f"pp-{group_id}",
        "merchant_category": merchant_category,
        "instalments": [
            {
                "sequence": i + 1,
                "date": "2025-03-01",
                "amount": 25.0,
                "paypal_txn_id": f"pp-inst-{group_id}-{i}",
                "anz_txn_id": tid,
                "anz_account": "ANZ Personal",
                "anz_description": "PAYMENT TO PAYPAL AUSTRALIA",
            }
            for i, tid in enumerate(anz_txn_ids)
        ],
        "anz_matched": len(anz_txn_ids),
        "status": "complete" if len(anz_txn_ids) == 4 else "partial",
    }


def _anz_df(txn_ids: list[str]) -> pd.DataFrame:
    rows = [
        {
            "txn_id": tid,
            "date": pd.Timestamp("2025-03-01"),
            "description": "PAYMENT TO PAYPAL AUSTRALIA",
            "amount": -25.0,
            "account": "ANZ Personal",
            "account_type": "transaction",
            "category": "Miscellaneous",
            "sub_category": "",
            "is_business": False,
            "note": "",
            "payee_name": "",
        }
        for tid in txn_ids
    ]
    return pd.DataFrame(rows)


def test_payin4_config_rule_applied():
    """Merchant matching a config rule gets that category applied to ANZ rows."""
    groups = [_p4_group("g1", "WOOLWORTHS SUPERMARKET", ["anz-1", "anz-2", "anz-3", "anz-4"])]
    df = _anz_df(["anz-1", "anz-2", "anz-3", "anz-4"])
    updated_groups, updated_df, changed = categorise_payin4_merchants(groups, df, BASE_CONFIG, use_api=False)
    assert updated_groups[0]["merchant_category"] == "Groceries"
    assert set(changed) == {"anz-1", "anz-2", "anz-3", "anz-4"}
    assert (updated_df["category"] == "Groceries").all()
    assert (updated_df["sub_category"] == "Pay-in-4").all()


def test_payin4_already_categorised_not_reprocessed():
    """Groups with existing merchant_category are skipped (no API call needed)."""
    groups = [_p4_group("g1", "WOOLWORTHS SUPERMARKET", ["anz-1"], merchant_category="Travel")]
    df = _anz_df(["anz-1"])
    updated_groups, updated_df, changed = categorise_payin4_merchants(groups, df, BASE_CONFIG, use_api=False)
    # Keeps existing category, not re-derived from config rule
    assert updated_groups[0]["merchant_category"] == "Travel"
    assert updated_df.loc[updated_df["txn_id"] == "anz-1", "category"].iloc[0] == "Travel"
    assert "anz-1" in changed


def test_payin4_no_anz_match_returns_empty_changed():
    """Group with no linked ANZ txn_ids doesn't crash and returns no changed ids."""
    groups = [_p4_group("g1", "WOOLWORTHS SUPERMARKET", [])]
    df = _anz_df(["anz-99"])  # unrelated row
    updated_groups, updated_df, changed = categorise_payin4_merchants(groups, df, BASE_CONFIG, use_api=False)
    assert changed == set()
    assert updated_df.loc[0, "category"] == "Miscellaneous"  # untouched


def test_payin4_uncached_no_api_leaves_merchant_category_empty():
    """Unknown merchant with use_api=False leaves merchant_category unset."""
    groups = [_p4_group("g1", "COMPLETELY UNKNOWN STORE XYZ", ["anz-1", "anz-2"])]
    df = _anz_df(["anz-1", "anz-2"])
    updated_groups, updated_df, changed = categorise_payin4_merchants(groups, df, BASE_CONFIG, use_api=False)
    assert updated_groups[0].get("merchant_category", "") == ""
    assert changed == set()
    assert (updated_df["category"] == "Miscellaneous").all()


def test_payin4_api_called_for_uncached_merchant(tmp_path):
    """Unknown merchant triggers an API call and the result is applied to ANZ rows."""
    import json

    cache_file = tmp_path / "cache.json"
    cfg = {**BASE_CONFIG, "data": {**BASE_CONFIG["data"], "cache_file": str(cache_file)}}

    groups = [_p4_group("g1", "VIRGIN AUSTRALIA", ["anz-1", "anz-2"])]
    df = _anz_df(["anz-1", "anz-2"])

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='[{"id": 0, "category": "Travel", "business": false}]')]

    mock_backend = MagicMock()
    mock_backend.messages.create.return_value = mock_response
    with patch("src.categoriser._get_backend", return_value=mock_backend):
        updated_groups, updated_df, changed = categorise_payin4_merchants(
            groups, df, cfg, use_api=True
        )

    assert updated_groups[0]["merchant_category"] == "Travel"
    assert changed == {"anz-1", "anz-2"}
    assert (updated_df["category"] == "Travel").all()
    assert (updated_df["sub_category"] == "Pay-in-4").all()
    # Result should be cached
    saved = json.loads(cache_file.read_text())
    assert any("category" in v and v["category"] == "Travel" for v in saved.values())


def test_payin4_empty_groups_returns_unchanged():
    df = _anz_df(["anz-1"])
    groups, result_df, changed = categorise_payin4_merchants([], df, BASE_CONFIG, use_api=False)
    assert groups == []
    assert changed == set()
    assert result_df["category"].iloc[0] == "Miscellaneous"


# ── Sub-category validation pass ──────────────────────────────────────────────

def test_invalid_subcat_cleared_by_categoriser():
    """Stale sub_category from cache that doesn't match new category must be cleared."""
    import json, tempfile, os
    with tempfile.TemporaryDirectory() as td:
        # Seed cache: "PAYPAL" debit → category Transport, sub_category Fuel
        cache = {"PAYPAL|dr": {"category": "Transport", "sub_category": "Fuel", "business": False}}
        cache_path = os.path.join(td, "cache.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f)

        # But we want this ANZ PayPal row to become Miscellaneous (via hardcoded branch)
        # sub_category Fuel must be cleared (Miscellaneous only allows "Other")
        df = _df([{"description": "PAYMENT TO PAYPAL AUST", "amount": -25.0, "txn_id": "t1"}])
        cfg = {**BASE_CONFIG, "data": {**BASE_CONFIG["data"], "cache_file": cache_path}}
        result = categorise_transactions(df, cfg, use_api=False)
        assert result["sub_category"].iloc[0] == ""


def test_valid_subcat_preserved_by_categoriser():
    """A valid (category, sub_category) pair must not be cleared."""
    import json, tempfile
    with tempfile.TemporaryDirectory() as td:
        cache = {"CALTEX STAR SHOP|dr": {"category": "Transport", "sub_category": "Fuel", "business": False}}
        cache_path = td + "/cache.json"
        with open(cache_path, "w") as f:
            json.dump(cache, f)

        df = _df([{"description": "CALTEX STAR SHOP", "amount": -80.0, "txn_id": "t1"}])
        cfg = {**BASE_CONFIG, "data": {**BASE_CONFIG["data"], "cache_file": cache_path}}
        result = categorise_transactions(df, cfg, use_api=False)
        assert result["category"].iloc[0] == "Transport"
        assert result["sub_category"].iloc[0] == "Fuel"
