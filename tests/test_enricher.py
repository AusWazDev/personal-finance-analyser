"""Tests for src/enricher.py — PayPal enrichment and hint detection."""
import pandas as pd
import pytest

from src.enricher import enrich_paypal_transactions, find_paypal_hints


# ── Helpers ───────────────────────────────────────────────────────────────────

def _anz_row(txn_id, date, amount, description="PAYMENT TO PAYPAL AUSTRALIA 123"):
    return {
        "txn_id": txn_id,
        "date": pd.Timestamp(date),
        "amount": amount,
        "description": description,
        "account_type": "transaction",
        "reference": "",
    }


def _paypal_row(date, amount, description):
    return {
        "txn_id": f"pp_{description[:4]}",
        "date": pd.Timestamp(date),
        "amount": amount,
        "description": description,
        "account_type": "paypal",
        "reference": "",
    }


def _make_df(*rows):
    return pd.DataFrame(rows)


# ── enrich_paypal_transactions ─────────────────────────────────────────────────

def test_enrich_matches_by_date_and_amount():
    df = _make_df(
        _anz_row("anz1", "2025-10-05", -49.99),
        _paypal_row("2025-10-04", -49.99, "Netflix"),
    )
    result = enrich_paypal_transactions(df, {})
    assert result.loc[result["txn_id"] == "anz1", "description"].iloc[0] == "PayPal: Netflix"


def test_enrich_within_5_day_window():
    df = _make_df(
        _anz_row("anz1", "2025-10-10", -25.00),
        _paypal_row("2025-10-05", -25.00, "Spotify"),
    )
    result = enrich_paypal_transactions(df, {})
    assert result.loc[result["txn_id"] == "anz1", "description"].iloc[0] == "PayPal: Spotify"


def test_enrich_no_match_beyond_window():
    df = _make_df(
        _anz_row("anz1", "2025-10-15", -25.00),
        _paypal_row("2025-10-01", -25.00, "Spotify"),  # 14 days apart — outside ±5
    )
    result = enrich_paypal_transactions(df, {})
    assert "PayPal:" not in result.loc[result["txn_id"] == "anz1", "description"].iloc[0]


def test_enrich_no_match_wrong_amount():
    df = _make_df(
        _anz_row("anz1", "2025-10-05", -49.99),
        _paypal_row("2025-10-04", -39.99, "Netflix"),  # amount mismatch
    )
    result = enrich_paypal_transactions(df, {})
    assert "PayPal:" not in result.loc[result["txn_id"] == "anz1", "description"].iloc[0]


def test_enrich_already_enriched_rows_skipped():
    df = _make_df(
        {
            "txn_id": "anz1",
            "date": pd.Timestamp("2025-10-05"),
            "amount": -49.99,
            "description": "PayPal: Netflix",  # already enriched
            "account_type": "transaction",
            "reference": "",
        },
        _paypal_row("2025-10-04", -49.99, "AnotherMerchant"),
    )
    result = enrich_paypal_transactions(df, {})
    # Should not be overwritten
    assert result.loc[result["txn_id"] == "anz1", "description"].iloc[0] == "PayPal: Netflix"


def test_enrich_no_paypal_export_returns_df_unchanged():
    df = _make_df(_anz_row("anz1", "2025-10-05", -49.99))
    result = enrich_paypal_transactions(df, {})
    assert result.loc[result["txn_id"] == "anz1", "description"].iloc[0] == "PAYMENT TO PAYPAL AUSTRALIA 123"


def test_enrich_empty_df_returns_empty():
    df = pd.DataFrame()
    result = enrich_paypal_transactions(df, {})
    assert result.empty


def test_enrich_stores_original_in_reference():
    df = _make_df(
        _anz_row("anz1", "2025-10-05", -49.99, "PAYMENT TO PAYPAL AUSTRALIA 999"),
        _paypal_row("2025-10-05", -49.99, "Adobe"),
    )
    result = enrich_paypal_transactions(df, {})
    assert result.loc[result["txn_id"] == "anz1", "reference"].iloc[0] == "PAYMENT TO PAYPAL AUSTRALIA 999"


# ── find_paypal_hints ─────────────────────────────────────────────────────────

def test_hints_returned_for_amount_match_within_window():
    df = _make_df(
        _anz_row("anz1", "2025-10-10", -30.00),
        _paypal_row("2025-10-15", -30.00, "Canva"),  # 5 days off — within 45-day window
    )
    hints = find_paypal_hints(df)
    assert "anz1" in hints
    assert hints["anz1"]["merchant"] == "Canva"


def test_hints_empty_when_no_paypal_export():
    df = _make_df(_anz_row("anz1", "2025-10-10", -30.00))
    hints = find_paypal_hints(df)
    assert hints == {}


def test_hints_excluded_for_already_enriched_rows():
    df = _make_df(
        {
            "txn_id": "anz1",
            "date": pd.Timestamp("2025-10-10"),
            "amount": -30.00,
            "description": "PayPal: Canva",
            "account_type": "transaction",
            "reference": "",
        },
        _paypal_row("2025-10-15", -30.00, "Canva"),
    )
    hints = find_paypal_hints(df)
    assert "anz1" not in hints


def test_hints_excluded_beyond_max_days():
    df = _make_df(
        _anz_row("anz1", "2025-10-10", -30.00),
        _paypal_row("2025-12-31", -30.00, "Canva"),  # 82 days — outside 45-day window
    )
    hints = find_paypal_hints(df, max_hint_days=45)
    assert "anz1" not in hints


def test_enrich_deduplicates_paypal_rows():
    """Two ANZ debits of the same amount on the same day should not both
    match the same PayPal row — the second one stays unenriched."""
    df = _make_df(
        _anz_row("anz1", "2025-11-01", -50.00),
        _anz_row("anz2", "2025-11-01", -50.00),
        _paypal_row("2025-11-01", -50.00, "MerchantA"),
        _paypal_row("2025-11-02", -50.00, "MerchantB"),
    )
    result = enrich_paypal_transactions(df, {})
    descs = {
        row["txn_id"]: row["description"]
        for _, row in result.iterrows()
        if row["txn_id"] in ("anz1", "anz2")
    }
    # Each ANZ row must map to a different merchant
    assert descs["anz1"] != descs["anz2"]
    assert descs["anz1"].startswith("PayPal:")
    assert descs["anz2"].startswith("PayPal:")


def test_enrich_second_anz_unmatched_when_only_one_paypal_row():
    """Two ANZ debits of the same amount but only one matching PayPal row —
    the second ANZ debit must remain unenriched (not steal the first's match)."""
    df = _make_df(
        _anz_row("anz1", "2025-11-01", -75.00),
        _anz_row("anz2", "2025-11-02", -75.00),
        _paypal_row("2025-11-01", -75.00, "OnlyMerchant"),
    )
    result = enrich_paypal_transactions(df, {})
    d1 = result.loc[result["txn_id"] == "anz1", "description"].iloc[0]
    d2 = result.loc[result["txn_id"] == "anz2", "description"].iloc[0]
    assert d1 == "PayPal: OnlyMerchant"
    assert "PayPal:" not in d2  # no match available — stays original
