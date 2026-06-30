"""Smoke tests for src/reporter.py — generate_fy_summary."""
import pandas as pd
import pytest

from src.reporter import generate_fy_summary


_CONFIG = {"business": {"full_name": "Test Company Pty Ltd"}}


def _make_df():
    return pd.DataFrame([
        {
            "date": pd.Timestamp("2025-09-15"), "amount": 5000.0,
            "category": "Income", "is_business": False, "is_tax_deductible": False,
            "is_gst_claimable": False,
            "description": "Trust distribution", "account": "ANZ Personal",
        },
        {
            "date": pd.Timestamp("2025-10-01"), "amount": -800.0,
            "category": "Groceries", "is_business": False, "is_tax_deductible": False,
            "is_gst_claimable": False, "description": "Woolworths", "account": "ANZ Personal",
        },
        {
            "date": pd.Timestamp("2025-11-20"), "amount": -150.0,
            "category": "Transport", "is_business": False, "is_tax_deductible": False,
            "is_gst_claimable": False, "description": "Fuel", "account": "ANZ Personal",
        },
        {
            "date": pd.Timestamp("2025-08-05"), "amount": -200.0,
            "category": "Business Expense", "is_business": True, "is_tax_deductible": False,
            "is_gst_claimable": False, "description": "WEBCENTRAL hosting", "account": "ANZ Personal",
        },
        {
            "date": pd.Timestamp("2025-09-01"), "amount": -120.0,
            "category": "Professional Development", "is_business": False, "is_tax_deductible": True,
            "is_gst_claimable": True, "description": "Professional membership fee", "account": "ANZ Personal",
        },
    ])


# ── File creation ─────────────────────────────────────────────────────────────

def test_generate_fy_summary_creates_file(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    assert (tmp_path / "fy_summary.html").exists()


def test_generate_fy_summary_empty_df_creates_file(tmp_path):
    empty = pd.DataFrame(columns=["date", "amount", "category", "is_business"])
    generate_fy_summary(empty, _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "No data" in content


# ── KPI banner (new sections) ─────────────────────────────────────────────────

def test_fy_summary_kpi_banner_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Taxable Income" in content
    assert "Total Expenditure" in content
    assert "Net Savings" in content
    assert "Savings Rate" in content


def test_fy_summary_kpi_income_value(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "5,000" in content


def test_fy_summary_kpi_net_savings_positive(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    # expenditure = 800 (Groceries) + 150 (Transport) + 200 (Business Expense) + 120 (Prof Dev) = 1270
    # net = 5000 - 1270 = 3730 — positive, so prefix should be +
    assert "+$3,730" in content


# ── Expenditure breakdown (new section) ───────────────────────────────────────

def test_fy_summary_expenditure_table_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Expenditure by Category" in content


def test_fy_summary_expenditure_categories_listed(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Groceries" in content
    assert "Transport" in content


def test_fy_summary_business_expense_in_expenditure_table(tmp_path):
    # "Business Expense" is NOT in _EXCLUDE_FROM_SPEND so it correctly appears
    # in both the expenditure breakdown AND the business expenses reimbursable table.
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    exp_section_start = content.find(">Expenditure by Category</h3>")
    exp_section = content[exp_section_start:exp_section_start + 2000]
    assert "Business Expense" in exp_section


# ── Tax-deductible expenses (new section) ─────────────────────────────────────

def test_fy_summary_tax_deductible_section_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Tax-Deductible Expenses" in content
    assert "Total Tax Deductible" in content


def test_fy_summary_tax_deductible_transaction_listed(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Professional membership fee" in content


def test_fy_summary_tax_deductible_empty_when_none_flagged(tmp_path):
    df_no_tax = _make_df().copy()
    df_no_tax["is_tax_deductible"] = False
    generate_fy_summary(df_no_tax, _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "No tax-deductible expenses recorded" in content


# ── Existing sections still present ───────────────────────────────────────────

def test_fy_summary_income_table_still_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "Income" in content
    assert "Taxable Income" in content


def test_fy_summary_business_expenses_table_still_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "WEBCENTRAL hosting" in content
    assert "Total Reimbursable" in content


# ── GST claimable section ─────────────────────────────────────────────────────

def test_fy_summary_gst_section_present(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "GST Claimable" in content
    assert "ATO Input Tax Credits" in content


def test_fy_summary_gst_total_correct(tmp_path):
    generate_fy_summary(_make_df(), _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    # The GST-flagged row is -120.0, so 1/11th = ~10.91
    assert "10.91" in content


def test_fy_summary_gst_empty_when_none_flagged(tmp_path):
    df_no_gst = _make_df().copy()
    df_no_gst["is_gst_claimable"] = False
    generate_fy_summary(df_no_gst, _CONFIG, tmp_path)
    content = (tmp_path / "fy_summary.html").read_text(encoding="utf-8")
    assert "No GST-claimable expenses flagged" in content
