"""
Tests for src/parsers.py

Run with:  python -m pytest tests/ -v
"""

import io
import logging
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.parsers import (
    parse_anz_csv,
    parse_anz_plus_pdf,
    parse_wise_pdf,
    parse_paypal_csv,
    parse_revolut_csv,
    parse_28degrees_pdf,
    _make_txn_id,
    _apply_exclusions,
    _warn_fuzzy_duplicates,
    load_all_transactions,
    _detect_file_type,
)


# ── ANZ CSV ───────────────────────────────────────────────────────────────────

ANZ_SAMPLE = textwrap.dedent("""\
    01/03/2025,-45.00,WOOLWORTHS 1234,,,,
    02/03/2025,1500.00,WARWICK JOHN LINDSAY,,,,
    03/03/2025,-12.50,NETFLIX.COM,,,,
""")


def _write_tmp(tmp_path: Path, content: str, name: str = "test.csv") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_anz_csv_basic(tmp_path):
    f = _write_tmp(tmp_path, ANZ_SAMPLE)
    df = parse_anz_csv(f, account_name="ANZ Personal", account_type="transaction")
    assert len(df) == 3
    assert df["account"].iloc[0] == "ANZ Personal"
    assert df["account_type"].iloc[0] == "transaction"
    assert df["amount"].iloc[0] == -45.0
    assert df["amount"].iloc[1] == 1500.0


def test_anz_csv_dates_parsed(tmp_path):
    f = _write_tmp(tmp_path, ANZ_SAMPLE)
    df = parse_anz_csv(f, account_name="ANZ Personal")
    assert df["date"].dtype == "datetime64[ns]"
    assert df["date"].iloc[0].day == 1
    assert df["date"].iloc[0].month == 3
    assert df["date"].iloc[0].year == 2025


def test_anz_csv_empty_file(tmp_path):
    f = _write_tmp(tmp_path, "")
    df = parse_anz_csv(f, account_name="ANZ Personal")
    assert df.empty


def test_anz_csv_skips_bad_rows(tmp_path):
    content = textwrap.dedent("""\
        not-a-date,abc,some description,,,,
        01/03/2025,-10.00,VALID MERCHANT,,,,
    """)
    f = _write_tmp(tmp_path, content)
    df = parse_anz_csv(f, account_name="ANZ Personal")
    assert len(df) == 1
    assert df["description"].iloc[0] == "VALID MERCHANT"


# ── PayPal CSV ────────────────────────────────────────────────────────────────

PAYPAL_SAMPLE = textwrap.dedent("""\
    Date,Time,Time zone,Name,Type,Status,Currency,Amount,Fees,Total,Exchange Rate,Receipt ID,Balance,Transaction ID,Item Title
    01/03/2025,10:00:00,AEST,Amazon Australia,General Payment,Completed,AUD,"-25.00","0.00","-25.00","","","100.00",TXN001,
    01/03/2025,11:00:00,AEST,PayPal,Transfer to PayPal account,Completed,AUD,"-25.00","0.00","-25.00","","","75.00",TXN002,
    02/03/2025,09:00:00,AEST,eBay,General Payment,Completed,AUD,"-15.00","0.00","-15.00","","","60.00",TXN003,
""")


def test_paypal_filters_internal_transfers(tmp_path):
    f = _write_tmp(tmp_path, PAYPAL_SAMPLE, "PayPal.csv")
    df = parse_paypal_csv(f)
    # "Transfer to PayPal account" row should be excluded
    assert len(df) == 2
    assert "Amazon Australia" in df["description"].values
    assert "eBay" in df["description"].values


def test_paypal_amounts(tmp_path):
    f = _write_tmp(tmp_path, PAYPAL_SAMPLE, "PayPal.csv")
    df = parse_paypal_csv(f)
    assert all(df["amount"] < 0)


def test_paypal_empty_returns_empty(tmp_path):
    # Header only
    f = _write_tmp(tmp_path, "Date,Time,Name,Type,Status,Currency,Amount\n", "PayPal.csv")
    df = parse_paypal_csv(f)
    assert df.empty


def test_paypal_fx_amount_resolved_to_aud(tmp_path):
    # A USD purchase generates 4 rows: the USD debit, an AUD funding row, and two
    # currency conversion rows. The parser should swap the USD amount for the AUD
    # equivalent so the enricher can match it against the ANZ bank debit.
    fx_sample = textwrap.dedent("""\
        Date,Time,Time zone,Name,Type,Status,Currency,Amount,Fees,Total,Balance
        01/11/2023,15:54:50,AEDT,Goli Nutrition Inc.,Pre-approved Payment Bill User Payment,Completed,USD,-79.32,0,-79.32,-79.32
        01/11/2023,15:54:50,AEDT,,Transfer to PayPal account,Pending,AUD,130.90,0,130.90,130.90
        01/11/2023,15:54:50,AEDT,,General Currency Conversion,Completed,AUD,-130.90,0,-130.90,0
        01/11/2023,15:54:50,AEDT,,General Currency Conversion,Completed,USD,79.32,0,79.32,0
    """)
    f = _write_tmp(tmp_path, fx_sample, "PayPal_FX.csv")
    df = parse_paypal_csv(f)
    # Only the Goli row should survive (funding + conversions filtered out)
    assert len(df) == 1
    assert df.iloc[0]["description"] == "Goli Nutrition Inc."
    # Amount should be the AUD equivalent, not the USD amount
    assert abs(df.iloc[0]["amount"] - (-130.90)) < 0.01


# ── Revolut CSV ───────────────────────────────────────────────────────────────

REVOLUT_SAMPLE = textwrap.dedent("""\
    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    CARD_PAYMENT,Current,2025-03-01 10:00:00,2025-03-01 10:05:00,Netflix,-14.99,0,AUD,COMPLETED,85.01
    CARD_PAYMENT,Current,2025-03-01 11:00:00,2025-03-01 11:05:00,Spotify,-10.99,0,EUR,COMPLETED,74.02
    TOPUP,Current,2025-03-02 09:00:00,2025-03-02 09:01:00,Apple Pay top-up by Lindsay,100.00,0,AUD,COMPLETED,174.02
    CARD_PAYMENT,Current,2025-03-03 14:00:00,,Pending purchase,-5.00,0,AUD,PENDING,169.02
""")


def test_revolut_filters_non_aud(tmp_path):
    f = _write_tmp(tmp_path, REVOLUT_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    # PENDING row is still excluded
    assert "Pending purchase" not in df["description"].values
    # EUR Spotify row is now INCLUDED (FX transactions kept, AUD amount from balance delta)
    assert "Spotify" in df["description"].values


def test_revolut_keeps_aud_completed(tmp_path):
    f = _write_tmp(tmp_path, REVOLUT_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    assert "Netflix" in df["description"].values
    assert df["account"].iloc[0] == "Revolut"


# ── Transaction ID & deduplication ───────────────────────────────────────────

def test_txn_id_stable():
    """Same input always produces same txn_id."""
    row = pd.Series({
        "date": pd.Timestamp("2025-03-01"),
        "amount": -45.0,
        "description": "WOOLWORTHS 1234",
        "account": "ANZ Personal",
    })
    id1 = _make_txn_id(row)
    id2 = _make_txn_id(row)
    assert id1 == id2
    assert len(id1) == 12


def test_txn_id_differs_for_different_amounts():
    base = {"date": pd.Timestamp("2025-03-01"), "description": "WOOLWORTHS", "account": "ANZ Personal"}
    r1 = pd.Series({**base, "amount": -45.0})
    r2 = pd.Series({**base, "amount": -46.0})
    assert _make_txn_id(r1) != _make_txn_id(r2)


# ── Exclusion filter ──────────────────────────────────────────────────────────

def test_exclusion_by_description():
    df = pd.DataFrame({
        "description": ["PAYMENT TO 28 DEGREES", "WOOLWORTHS", "NETFLIX"],
        "account": ["ANZ Personal", "ANZ Personal", "ANZ Personal"],
        "amount": [-500.0, -45.0, -14.99],
    })
    config = {
        "exclude_from_analysis": [
            {"description_contains": "28 DEGREES"},
        ]
    }
    result = _apply_exclusions(df, config)
    assert len(result) == 2
    assert "WOOLWORTHS" in result["description"].values


def test_exclusion_account_scoped():
    """description_contains + account only excludes matching account."""
    df = pd.DataFrame({
        "description": ["REVOLUT top-up", "REVOLUT top-up"],
        "account": ["ANZ Personal", "Revolut"],
        "amount": [-100.0, 100.0],
    })
    config = {
        "exclude_from_analysis": [
            {"description_contains": "REVOLUT", "account": "ANZ Personal"},
        ]
    }
    result = _apply_exclusions(df, config)
    assert len(result) == 1
    assert result["account"].iloc[0] == "Revolut"


# ── File type detection ───────────────────────────────────────────────────────

def test_detect_html_is_latitude(tmp_path):
    f = tmp_path / "Latitude Statement.html"
    f.write_text("<html></html>")
    config = {"accounts": {}}
    ftype, _ = _detect_file_type(f, config)
    assert ftype == "latitude_html"


# ── ANZ Plus PDF ─────────────────────────────────────────────────────────────

ANZ_PLUS_PDF_TEXT = """\
1 January 2025 - 31 January 2025
Date Description Credit Debit Balance
Account Statement
02 Jan NETFLIX.COM $15.99 $939.01
01 Jan WOOLWORTHS METRO $45.00 $955.00
Opening Balance $1000.00
"""


def _mock_pdf(text: str):
    """Return a pdfplumber-compatible context manager mock."""
    page = MagicMock()
    page.extract_text.return_value = text
    pdf = MagicMock()
    pdf.__enter__ = lambda s: s
    pdf.__exit__ = MagicMock(return_value=False)
    pdf.pages = [page]
    return pdf


def test_anz_plus_pdf_basic(tmp_path):
    f = tmp_path / "2025-01-31 - Everyday x7893 Statement.pdf"
    f.write_bytes(b"%PDF-1.4")  # minimal placeholder (pdfplumber is mocked)

    with patch("pdfplumber.open", return_value=_mock_pdf(ANZ_PLUS_PDF_TEXT)):
        df = parse_anz_plus_pdf(f, account_name="ANZ Plus Everyday")

    assert len(df) == 2
    assert df["account"].iloc[0] == "ANZ Plus Everyday"


def test_anz_plus_pdf_amounts_sign(tmp_path):
    """Both transactions are debits — balance decreases each time."""
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(ANZ_PLUS_PDF_TEXT)):
        df = parse_anz_plus_pdf(f, account_name="ANZ Plus Everyday")

    # Both Woolworths and Netflix are outgoing (negative)
    assert all(df["amount"] < 0), f"Expected negatives, got {df['amount'].tolist()}"


def test_anz_plus_pdf_dates_inferred(tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(ANZ_PLUS_PDF_TEXT)):
        df = parse_anz_plus_pdf(f, account_name="ANZ Plus Everyday")

    assert df["date"].dt.year.unique().tolist() == [2025]
    assert sorted(df["date"].dt.month.unique().tolist()) == [1]


def test_anz_plus_pdf_empty_statement(tmp_path):
    empty_text = "1 January 2025 - 31 January 2025\nThere are no transactions to display.\n"
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(empty_text)):
        df = parse_anz_plus_pdf(f, account_name="ANZ Plus Everyday")

    assert df.empty


# ── Wise PDF ──────────────────────────────────────────────────────────────────

WISE_PDF_TEXT = """\
Card transaction of 25.00 USD issued by AMAZON US -20.50 980.00
14 January 2025 purchase at Amazon
Card transaction of 15.99 AUD issued by NETFLIX -15.99 964.01
16 January 2025 Netflix subscription
AUD Assets service fee -0.15 963.86
17 January 2025 monthly fee
"""


def test_wise_pdf_basic(tmp_path):
    f = tmp_path / "Wise - statement_2025.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(WISE_PDF_TEXT)):
        df = parse_wise_pdf(f, account_name="Wise")

    assert len(df) == 3
    assert df["account"].iloc[0] == "Wise"


def test_wise_pdf_amounts(tmp_path):
    f = tmp_path / "Wise - statement_2025.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(WISE_PDF_TEXT)):
        df = parse_wise_pdf(f, account_name="Wise")

    amounts = df["amount"].tolist()
    assert -20.50 in amounts   # FX card transaction AUD cost
    assert -15.99 in amounts   # AUD Netflix
    assert -0.15 in amounts    # service fee


def test_wise_pdf_merchant_extracted(tmp_path):
    f = tmp_path / "Wise - statement_2025.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf(WISE_PDF_TEXT)):
        df = parse_wise_pdf(f, account_name="Wise")

    # Card transaction description should be cleaned to merchant name
    assert "AMAZON US" in df["description"].values


def test_wise_pdf_empty(tmp_path):
    f = tmp_path / "Wise - statement_2025.pdf"
    f.write_bytes(b"%PDF")

    with patch("pdfplumber.open", return_value=_mock_pdf("No transactions.\n")):
        df = parse_wise_pdf(f, account_name="Wise")

    assert df.empty


# ── Revolut FX ────────────────────────────────────────────────────────────────

REVOLUT_FX_SAMPLE = textwrap.dedent("""\
    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    CARD_PAYMENT,Current,2025-03-01 10:00:00,2025-03-01 10:05:00,Netflix,-14.99,0,AUD,COMPLETED,985.01
    CARD_PAYMENT,Current,2025-03-02 11:00:00,2025-03-02 11:05:00,Amazon,-25.00,0,USD,COMPLETED,947.51
""")


def test_revolut_includes_fx_transactions(tmp_path):
    f = _write_tmp(tmp_path, REVOLUT_FX_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    # Both AUD and USD transactions should be present
    assert len(df) == 2
    assert "Amazon" in df["description"].values


def test_revolut_fx_note_contains_currency(tmp_path):
    f = _write_tmp(tmp_path, REVOLUT_FX_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    amazon_row = df[df["description"] == "Amazon"].iloc[0]
    assert "USD" in str(amazon_row["note"])


def test_revolut_fx_amount_from_balance_delta(tmp_path):
    f = _write_tmp(tmp_path, REVOLUT_FX_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    # AUD row: use Amount directly (-14.99)
    netflix = df[df["description"] == "Netflix"].iloc[0]
    assert netflix["amount"] == pytest.approx(-14.99)
    # FX row: balance went from 985.01 to 947.51 = -37.50 AUD cost
    amazon = df[df["description"] == "Amazon"].iloc[0]
    assert amazon["amount"] == pytest.approx(-37.50)


REVOLUT_FX_FIRST_SAMPLE = textwrap.dedent("""\
    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    CARD_PAYMENT,Current,2025-03-01 10:00:00,2025-03-01 10:05:00,Amazon,-25.00,0,USD,COMPLETED,962.50
    CARD_PAYMENT,Current,2025-03-02 11:00:00,2025-03-02 11:05:00,Netflix,-14.99,0,AUD,COMPLETED,947.51
""")


def test_revolut_fx_first_row_not_dropped(tmp_path):
    # Regression: the first FX row previously got NaN from diff() and was silently dropped
    f = _write_tmp(tmp_path, REVOLUT_FX_FIRST_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    assert len(df) == 2
    assert "Amazon" in df["description"].values


def test_revolut_fx_first_row_uses_balance_as_amount(tmp_path):
    # First FX row: no prior balance → use Balance column directly (opening = 0)
    f = _write_tmp(tmp_path, REVOLUT_FX_FIRST_SAMPLE, "Revolut.csv")
    df = parse_revolut_csv(f, account_name="Revolut")
    amazon = df[df["description"] == "Amazon"].iloc[0]
    assert amazon["amount"] == pytest.approx(962.50)  # Balance of first row


# ── Fuzzy duplicate detection ─────────────────────────────────────────────────

def test_fuzzy_duplicate_warning(caplog):
    df = pd.DataFrame({
        "txn_id": ["aaa", "bbb"],
        "date": [pd.Timestamp("2025-03-01"), pd.Timestamp("2025-03-01")],
        "amount": [-45.0, -45.0],
        "description": ["WOOLWORTHS METRO", "WOOLWORTHS METRO"],
        "account": ["ANZ Personal", "ANZ Personal"],
    })
    with caplog.at_level(logging.WARNING, logger="src.parsers"):
        _warn_fuzzy_duplicates(df)
    assert "potential near-duplicate" in caplog.text


def test_fuzzy_no_false_positive_different_accounts(caplog):
    """Same amount/description on same day but different accounts is NOT a duplicate."""
    df = pd.DataFrame({
        "txn_id": ["aaa", "bbb"],
        "date": [pd.Timestamp("2025-03-01"), pd.Timestamp("2025-03-01")],
        "amount": [-45.0, -45.0],
        "description": ["WOOLWORTHS", "WOOLWORTHS"],
        "account": ["ANZ Personal", "28 Degrees Credit Card"],
    })
    with caplog.at_level(logging.WARNING, logger="src.parsers"):
        _warn_fuzzy_duplicates(df)
    assert "potential near-duplicate" not in caplog.text


# ── 28 Degrees PDF ───────────────────────────────────────────────────────────

def _w(text, top, x0):
    """Build a minimal pdfplumber word dict."""
    return {"text": text, "top": float(top), "x0": float(x0), "x1": float(x0) + 50.0}


def _mock_pdf_words(pages_words):
    """Return a pdfplumber context-manager mock whose pages yield extract_words() lists."""
    pages = []
    for words in pages_words:
        page = MagicMock()
        page.extract_words.return_value = words
        page.extract_text.return_value = ""
        pages.append(page)
    pdf = MagicMock()
    pdf.__enter__ = lambda s: s
    pdf.__exit__ = MagicMock(return_value=False)
    pdf.pages = pages
    return pdf


# One page: one debit row, one credit row
_LAT_PAGE_BASIC = [
    _w("08/04/2026", 100, 50),   _w("xxxx", 100, 130), _w("WOOLWORTHS", 100, 210), _w("$45.00", 100, 450),
    _w("07/04/2026", 200, 50),   _w("xxxx", 200, 130), _w("OnlineAccountPayment", 200, 210), _w("$250.00", 200, 560),
]

# Two pages: one transaction each — tests that last-txn of page 1 is saved
_LAT_PAGE2A = [
    _w("01/04/2026", 100, 50), _w("xxxx", 100, 130), _w("NETFLIX", 100, 210), _w("$15.99", 100, 450),
]
_LAT_PAGE2B = [
    _w("02/04/2026", 100, 50), _w("xxxx", 100, 130), _w("SPOTIFY", 100, 210), _w("$9.99", 100, 450),
]

# FX row: transaction line + continuation line with currency/rate token
_LAT_PAGE_FX = [
    _w("04/04/2026", 100, 50), _w("xxxx", 100, 130), _w("Elevenlabs.Io", 100, 210), _w("$7.26", 100, 450),
    _w("5.00USDRate:1.452000", 200, 210),   # continuation line — different y
]


def test_28degrees_pdf_basic(tmp_path):
    f = tmp_path / "01_Jan_2026_statement.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([_LAT_PAGE_BASIC])):
        df = parse_28degrees_pdf(f, account_name="28 Degrees Credit Card")
    assert len(df) == 2
    assert df["account"].iloc[0] == "28 Degrees Credit Card"
    assert "WOOLWORTHS" in df["description"].values
    assert "OnlineAccountPayment" in df["description"].values


def test_28degrees_pdf_debit_is_negative(tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([_LAT_PAGE_BASIC])):
        df = parse_28degrees_pdf(f)
    woolies = df[df["description"] == "WOOLWORTHS"].iloc[0]
    assert woolies["amount"] == pytest.approx(-45.00)


def test_28degrees_pdf_credit_is_positive(tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([_LAT_PAGE_BASIC])):
        df = parse_28degrees_pdf(f)
    payment = df[df["description"] == "OnlineAccountPayment"].iloc[0]
    assert payment["amount"] == pytest.approx(250.00)


def test_28degrees_pdf_last_txn_per_page_saved(tmp_path):
    """Each page's last transaction must not be dropped."""
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([_LAT_PAGE2A, _LAT_PAGE2B])):
        df = parse_28degrees_pdf(f)
    assert len(df) == 2
    assert "NETFLIX" in df["description"].values
    assert "SPOTIFY" in df["description"].values


def test_28degrees_pdf_fx_note(tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([_LAT_PAGE_FX])):
        df = parse_28degrees_pdf(f)
    assert len(df) == 1
    assert "USD" in df["note"].iloc[0]
    assert "1.452000" in df["note"].iloc[0]


def test_28degrees_pdf_empty(tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF")
    with patch("pdfplumber.open", return_value=_mock_pdf_words([[]])):
        df = parse_28degrees_pdf(f)
    assert df.empty


# ── File type detection ───────────────────────────────────────────────────────

def test_detect_paypal_from_pattern(tmp_path):
    f = tmp_path / "PayPal 2025-01 to 2025-03.csv"
    f.write_text("Date,Amount\n")
    config = {
        "accounts": {
            "paypal": {
                "file_pattern": "PayPal*.csv",
                "type": "paypal",
                "display_name": "PayPal",
            }
        }
    }
    ftype, acct = _detect_file_type(f, config)
    assert ftype == "paypal_csv"


# ── file_account_overrides.json support ─────────────────────────────────────

def test_detect_pdf_with_override_uses_config_account(tmp_path):
    """When a PDF filename is in file_account_overrides.json, use the mapped config account."""
    from src.parsers import _detect_file_type
    import json

    db_file = tmp_path / "finance.db"
    db_file.write_bytes(b"")
    overrides_path = tmp_path / "file_account_overrides.json"
    overrides_path.write_text(json.dumps({"mystatement.pdf": "anz_savings"}), encoding="utf-8")

    config = {
        "data": {"database": str(db_file)},
        "accounts": {
            "anz_savings": {
                "display_name": "ANZ Savings Account",
                "type": "transaction",
                "file_pattern": "ANZ_Savings_*.pdf",
            }
        },
    }
    pdf_file = tmp_path / "mystatement.pdf"
    pdf_file.write_bytes(b"%PDF")

    ftype, acct = _detect_file_type(pdf_file, config)
    assert ftype == "anz_plus_pdf"
    assert acct.get("display_name") == "ANZ Savings Account"


def test_detect_html_with_override_uses_config_account(tmp_path):
    """Same override mechanism works for HTML files."""
    from src.parsers import _detect_file_type
    import json

    db_file = tmp_path / "finance.db"
    db_file.write_bytes(b"")
    overrides_path = tmp_path / "file_account_overrides.json"
    overrides_path.write_text(json.dumps({"card_statement.html": "latitude_card"}), encoding="utf-8")

    config = {
        "data": {"database": str(db_file)},
        "accounts": {
            "latitude_card": {
                "display_name": "Latitude 28 Degrees",
                "type": "credit_card",
            }
        },
    }
    html_file = tmp_path / "card_statement.html"
    html_file.write_text("<html></html>")

    ftype, acct = _detect_file_type(html_file, config)
    assert ftype == "latitude_html"
    assert acct.get("display_name") == "Latitude 28 Degrees"


def test_detect_pdf_without_override_falls_through_to_inference(tmp_path):
    """When no override exists, inference still works normally."""
    from src.parsers import _detect_file_type
    import json

    db_file = tmp_path / "finance.db"
    db_file.write_bytes(b"")
    overrides_path = tmp_path / "file_account_overrides.json"
    overrides_path.write_text(json.dumps({}), encoding="utf-8")

    config = {"data": {"database": str(db_file)}, "accounts": {}}
    pdf_file = tmp_path / "Everyday x1234.pdf"
    pdf_file.write_bytes(b"%PDF")

    ftype, acct = _detect_file_type(pdf_file, config)
    assert ftype == "anz_plus_pdf"
    assert "everyday" in acct.get("display_name", "").lower()


def test_detect_pdf_override_unknown_account_key_falls_through(tmp_path):
    """If the override references a non-existent account key, inference still runs."""
    from src.parsers import _detect_file_type
    import json

    db_file = tmp_path / "finance.db"
    db_file.write_bytes(b"")
    overrides_path = tmp_path / "file_account_overrides.json"
    overrides_path.write_text(json.dumps({"mystatement.pdf": "nonexistent_key"}), encoding="utf-8")

    config = {"data": {"database": str(db_file)}, "accounts": {}}
    pdf_file = tmp_path / "mystatement.pdf"
    pdf_file.write_bytes(b"%PDF")

    ftype, acct = _detect_file_type(pdf_file, config)
    # Falls through to inference — returns anz_plus_pdf with inferred display_name
    assert ftype == "anz_plus_pdf"
    assert acct.get("display_name") == "mystatement"


def test_detect_credit_card_pdf_override_returns_latitude_parser(tmp_path):
    """A credit_card type account override returns latitude_pdf parser."""
    from src.parsers import _detect_file_type
    import json

    db_file = tmp_path / "finance.db"
    db_file.write_bytes(b"")
    overrides_path = tmp_path / "file_account_overrides.json"
    overrides_path.write_text(json.dumps({"28degrees_jan.pdf": "my_28deg"}), encoding="utf-8")

    config = {
        "data": {"database": str(db_file)},
        "accounts": {
            "my_28deg": {
                "display_name": "28 Degrees",
                "type": "credit_card",
            }
        },
    }
    pdf_file = tmp_path / "28degrees_jan.pdf"
    pdf_file.write_bytes(b"%PDF")

    ftype, acct = _detect_file_type(pdf_file, config)
    assert ftype == "latitude_pdf"
    assert acct.get("display_name") == "28 Degrees"
