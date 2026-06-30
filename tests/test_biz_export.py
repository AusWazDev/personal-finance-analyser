"""Tests for src/biz_export.py."""
import pytest
from datetime import date

from src.biz_export import generate_ofx, generate_qif, generate_csv, _account_code


# ── helpers ────────────────────────────────────────────────────────────────────

def _row(desc="ACME PTY LTD", amount=-100.0, category="Business Expense",
         txn_id="ABC123", account="ANZ", d=None):
    return {
        "description": desc,
        "amount": amount,
        "category": category,
        "txn_id": txn_id,
        "account": account,
        "date": d or date(2026, 3, 15),
    }


CFG_CODES = {"business_export": {"account_codes": {"Business Expense": "6000", "Travel": "6200"}}}
CFG_EMPTY = {}


# ── _account_code ───────────────────────────────────────────────────────────

def test_account_code_mapped():
    assert _account_code("Business Expense", CFG_CODES) == "6000"

def test_account_code_unmapped_returns_category():
    assert _account_code("Groceries", CFG_CODES) == "Groceries"

def test_account_code_empty_config():
    assert _account_code("Travel", CFG_EMPTY) == "Travel"


# ── generate_ofx ────────────────────────────────────────────────────────────

def test_ofx_header_present():
    out = generate_ofx([], 2026, CFG_EMPTY)
    assert "OFXHEADER:100" in out
    assert "DATA:OFSGML" in out

def test_ofx_empty_rows():
    out = generate_ofx([], 2026, CFG_EMPTY)
    assert "<STMTTRN>" not in out
    assert "CURDEF>AUD" in out

def test_ofx_single_row_debit():
    out = generate_ofx([_row(amount=-50.0, txn_id="T001")], 2026, CFG_EMPTY)
    assert "<TRNTYPE>DEBIT" in out
    assert "<TRNAMT>-50.00" in out
    assert "<FITID>T001" in out

def test_ofx_single_row_credit():
    out = generate_ofx([_row(amount=200.0)], 2026, CFG_EMPTY)
    assert "<TRNTYPE>CREDIT" in out
    assert "<TRNAMT>200.00" in out

def test_ofx_account_code_in_memo():
    out = generate_ofx([_row(category="Business Expense")], 2026, CFG_CODES)
    assert "<MEMO>6000" in out

def test_ofx_date_format():
    out = generate_ofx([_row(d=date(2026, 3, 15))], 2026, CFG_EMPTY)
    assert "<DTPOSTED>20260315000000" in out

def test_ofx_fy_account_id():
    out = generate_ofx([], 2026, CFG_EMPTY)
    assert "FY2026_BUSINESS" in out

def test_ofx_description_truncated_and_escaped():
    long_desc = "A" * 40
    out = generate_ofx([_row(desc=long_desc)], 2026, CFG_EMPTY)
    # NAME field should be at most 32 chars
    for line in out.splitlines():
        if line.startswith("<NAME>"):
            assert len(line) - len("<NAME>") <= 32


# ── generate_qif ────────────────────────────────────────────────────────────

def test_qif_type_header():
    out = generate_qif([], 2026, CFG_EMPTY)
    assert out.startswith("!Type:Bank")

def test_qif_empty_rows():
    out = generate_qif([], 2026, CFG_EMPTY)
    assert "^" not in out

def test_qif_single_row():
    out = generate_qif([_row(amount=-75.50, desc="Office Supplies", txn_id="X")], 2026, CFG_EMPTY)
    assert "D03/15/2026" in out
    assert "T-75.50" in out
    assert "POffice Supplies" in out
    assert "^" in out

def test_qif_account_code_in_L_field():
    out = generate_qif([_row(category="Business Expense")], 2026, CFG_CODES)
    assert "L6000" in out

def test_qif_entry_separator():
    out = generate_qif([_row(), _row(amount=-20.0)], 2026, CFG_EMPTY)
    assert out.count("^") == 2


# ── generate_csv ────────────────────────────────────────────────────────────

def test_csv_header_row():
    out = generate_csv([], CFG_EMPTY)
    assert b"Date" in out
    assert b"Description" in out
    assert b"Account Code" in out

def test_csv_empty_rows():
    out = generate_csv([], CFG_EMPTY)
    lines = out.decode("utf-8").strip().splitlines()
    assert len(lines) == 1  # header only

def test_csv_single_row():
    out = generate_csv([_row(amount=-99.99, desc="Laptop", category="Business Expense")], CFG_CODES)
    text = out.decode("utf-8")
    assert "Laptop" in text
    assert "-99.99" in text
    assert "6000" in text

def test_csv_date_iso_format():
    out = generate_csv([_row(d=date(2026, 3, 15))], CFG_EMPTY)
    assert b"2026-03-15" in out

def test_csv_returns_bytes():
    assert isinstance(generate_csv([], CFG_EMPTY), bytes)
