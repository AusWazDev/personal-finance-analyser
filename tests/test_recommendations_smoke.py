"""Smoke tests for src/recommendations.py — _build_summary (pure) and API mock."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.recommendations import _build_summary, generate_recommendations_html


# ── Fixtures ──────────────────────────────────────────────────────────────────

_CONFIG = {"business": {"full_name": "Test Co Pty Ltd"}}


def _make_df():
    return pd.DataFrame([
        {
            "date": pd.Timestamp("2025-09-15"), "amount": 5000.0,
            "category": "Income", "description": "Trust distribution",
            "account": "ANZ Personal", "is_business": False, "sub_category": "",
        },
        {
            "date": pd.Timestamp("2025-10-01"), "amount": -120.0,
            "category": "Groceries", "description": "WOOLWORTHS",
            "account": "ANZ Personal", "is_business": False, "sub_category": "",
        },
        {
            "date": pd.Timestamp("2025-10-15"), "amount": -80.0,
            "category": "Dining Out", "description": "MCDONALDS",
            "account": "ANZ Personal", "is_business": False, "sub_category": "",
        },
        {
            "date": pd.Timestamp("2025-11-01"), "amount": -120.0,
            "category": "Groceries", "description": "WOOLWORTHS",
            "account": "ANZ Personal", "is_business": False, "sub_category": "",
        },
        {
            "date": pd.Timestamp("2025-11-05"), "amount": -200.0,
            "category": "Business Expense", "description": "WEBCENTRAL",
            "account": "ANZ Personal", "is_business": True, "sub_category": "",
        },
    ])


# ── _build_summary (pure — no API) ────────────────────────────────────────────

def test_build_summary_returns_dict():
    summary = _build_summary(_make_df(), _CONFIG)
    assert isinstance(summary, dict)


def test_build_summary_contains_required_keys():
    summary = _build_summary(_make_df(), _CONFIG)
    assert "spend_by_category_aud" in summary
    assert "average_monthly_spend" in summary
    assert "date_range" in summary
    assert "top_recurring_merchants" in summary


def test_build_summary_date_range_correct():
    summary = _build_summary(_make_df(), _CONFIG)
    assert summary["date_range"]["from"] == "2025-09-15"
    assert summary["date_range"]["to"] == "2025-11-05"


def test_build_summary_spend_excludes_income():
    summary = _build_summary(_make_df(), _CONFIG)
    assert "Income" not in summary["spend_by_category_aud"]


def test_build_summary_groceries_total_correct():
    summary = _build_summary(_make_df(), _CONFIG)
    assert abs(summary["spend_by_category_aud"].get("Groceries", 0) - 240.0) < 0.01


def test_build_summary_dining_to_groceries_ratio():
    summary = _build_summary(_make_df(), _CONFIG)
    # Dining = 80, Groceries = 240 → ratio ≈ 0.33
    ratio = summary.get("dining_to_groceries_ratio")
    assert ratio is not None
    assert abs(ratio - round(80 / 240, 2)) < 0.01


def test_build_summary_business_expenses_list():
    summary = _build_summary(_make_df(), _CONFIG)
    biz = summary["business_expenses"]
    assert len(biz) == 1
    assert any("WEBCENTRAL" in str(b.get("description", "")) for b in biz)


def test_build_summary_recurring_merchants():
    # WOOLWORTHS appears in Oct + Nov → should be detected as recurring
    summary = _build_summary(_make_df(), _CONFIG)
    merchants = [r["merchant_clean"] for r in summary["top_recurring_merchants"]]
    assert "WOOLWORTHS" in merchants


# ── generate_recommendations_html (mocked API) ────────────────────────────────

def _mock_anthropic(response_text: str):
    """Return a mock anthropic.Anthropic instance whose messages.create returns response_text."""
    mock_content = SimpleNamespace(text=response_text)
    mock_response = SimpleNamespace(content=[mock_content])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


def test_generate_recommendations_html_returns_string(monkeypatch):
    mock_client = _mock_anthropic(
        "## Subscription Audit\n- No subscriptions detected.\n\n"
        "## Quick Wins\n1. Save $50 on groceries.\n"
    )
    with patch("src.recommendations._get_backend", return_value=mock_client):
        result = generate_recommendations_html(_make_df(), _CONFIG)
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_recommendations_html_contains_section_headings(monkeypatch):
    mock_client = _mock_anthropic(
        "## Subscription Audit\n- Check subscriptions.\n\n"
        "## Quick Wins\n1. Cut coffee.\n"
    )
    with patch("src.recommendations._get_backend", return_value=mock_client):
        result = generate_recommendations_html(_make_df(), _CONFIG)
    assert "Subscription Audit" in result
    assert "Quick Wins" in result


def test_generate_recommendations_html_empty_df_returns_no_data_message(monkeypatch):
    result = generate_recommendations_html(pd.DataFrame(), _CONFIG)
    assert "No transaction data" in result


def test_generate_recommendations_html_api_error_returns_error_message(monkeypatch):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("connection timeout")
    with patch("src.recommendations._get_backend", return_value=mock_client):
        result = generate_recommendations_html(_make_df(), _CONFIG)
    assert "unavailable" in result.lower() or "error" in result.lower()
