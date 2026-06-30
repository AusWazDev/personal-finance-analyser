"""Tests for B7 bank parsers: CommBank CSV, Westpac CSV, NAB CSV, OFX."""
import textwrap
from pathlib import Path

import pandas as pd
import pytest

from src.parsers import (
    parse_commbank_csv,
    parse_westpac_csv,
    parse_nab_csv,
    parse_ofx,
    _detect_file_type,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write(tmp_path, filename, content):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ── CommBank CSV ──────────────────────────────────────────────────────────────

CBA_CSV = """\
    Date,Amount,Description,Balance
    09/01/2024,-500.00,EFTPOS WOOLWORTHS SYDNEY NS,1234.56
    09/01/2024,2000.00,SALARY CREDIT ACME PTY LTD,1734.56
    15/01/2024,-45.50,NETFLIX.COM,1689.06
    """


def test_commbank_parses_rows(tmp_path):
    p = _write(tmp_path, "transactions.csv", CBA_CSV)
    df = parse_commbank_csv(p, "CBA Everyday")
    assert len(df) == 3


def test_commbank_amounts(tmp_path):
    p = _write(tmp_path, "transactions.csv", CBA_CSV)
    df = parse_commbank_csv(p, "CBA Everyday")
    assert df.iloc[0]["amount"] == pytest.approx(-500.00)
    assert df.iloc[1]["amount"] == pytest.approx(2000.00)


def test_commbank_dates(tmp_path):
    p = _write(tmp_path, "transactions.csv", CBA_CSV)
    df = parse_commbank_csv(p, "CBA Everyday")
    assert df.iloc[0]["date"].date().isoformat() == "2024-01-09"


def test_commbank_account_name(tmp_path):
    p = _write(tmp_path, "transactions.csv", CBA_CSV)
    df = parse_commbank_csv(p, "My CBA", "transaction")
    assert (df["account"] == "My CBA").all()


def test_commbank_description(tmp_path):
    p = _write(tmp_path, "transactions.csv", CBA_CSV)
    df = parse_commbank_csv(p, "CBA")
    assert df.iloc[0]["description"] == "EFTPOS WOOLWORTHS SYDNEY NS"


def test_commbank_empty_file(tmp_path):
    p = _write(tmp_path, "empty.csv", "Date,Amount,Description,Balance\n")
    df = parse_commbank_csv(p, "CBA")
    assert df.empty


def test_commbank_missing_columns(tmp_path):
    p = _write(tmp_path, "bad.csv", "Col1,Col2\n1,2\n")
    df = parse_commbank_csv(p, "CBA")
    assert df.empty


# ── Westpac CSV ───────────────────────────────────────────────────────────────

WESTPAC_CSV = """\
    Transaction Date,Description,Debit,Credit,Balance
    09/01/2024,VISA PURCHASE WOOLWORTHS,500.00,,1234.56
    09/01/2024,SALARY CREDIT,,2000.00,3234.56
    15/01/2024,NETFLIX SUBSCRIPTION,15.99,,3218.57
    """

WESTPAC_LEGACY_CSV = """\
    Transaction Date,Narration,Cheque Number,Amount,Credit/Debit,Balance
    09/01/2024,EFTPOS WOOLWORTHS,,"-500.00",DR,1234.56
    09/01/2024,SALARY CREDIT,,"2000.00",CR,1734.56
    """


def test_westpac_parses_rows(tmp_path):
    p = _write(tmp_path, "westpac.csv", WESTPAC_CSV)
    df = parse_westpac_csv(p, "Westpac")
    assert len(df) == 3


def test_westpac_debit_is_negative(tmp_path):
    p = _write(tmp_path, "westpac.csv", WESTPAC_CSV)
    df = parse_westpac_csv(p, "Westpac")
    assert df.iloc[0]["amount"] == pytest.approx(-500.00)


def test_westpac_credit_is_positive(tmp_path):
    p = _write(tmp_path, "westpac.csv", WESTPAC_CSV)
    df = parse_westpac_csv(p, "Westpac")
    assert df.iloc[1]["amount"] == pytest.approx(2000.00)


def test_westpac_date(tmp_path):
    p = _write(tmp_path, "westpac.csv", WESTPAC_CSV)
    df = parse_westpac_csv(p, "Westpac")
    assert df.iloc[0]["date"].date().isoformat() == "2024-01-09"


def test_westpac_account_name(tmp_path):
    p = _write(tmp_path, "westpac.csv", WESTPAC_CSV)
    df = parse_westpac_csv(p, "My Westpac")
    assert (df["account"] == "My Westpac").all()


def test_westpac_legacy_format(tmp_path):
    """Legacy format with Amount column (signed) is also accepted."""
    p = _write(tmp_path, "westpac_legacy.csv", WESTPAC_LEGACY_CSV)
    df = parse_westpac_csv(p, "Westpac")
    assert len(df) == 2
    assert df.iloc[0]["amount"] == pytest.approx(-500.00)


def test_westpac_unknown_layout_returns_empty(tmp_path):
    p = _write(tmp_path, "bad.csv", "Col1,Col2\n1,2\n")
    df = parse_westpac_csv(p, "Westpac")
    assert df.empty


# ── NAB CSV ───────────────────────────────────────────────────────────────────

NAB_CSV = """\
    Date,Amount,Description,Balance
    09-Jan-2024,-500.00,EFTPOS PURCHASE - WOOLWORTHS,1234.56
    09-Jan-2024,2000.00,SALARY CREDIT - ACME,1734.56
    15-Jan-2024,-45.50,NETFLIX,1689.06
    """


def test_nab_parses_rows(tmp_path):
    p = _write(tmp_path, "nab.csv", NAB_CSV)
    df = parse_nab_csv(p, "NAB")
    assert len(df) == 3


def test_nab_amounts(tmp_path):
    p = _write(tmp_path, "nab.csv", NAB_CSV)
    df = parse_nab_csv(p, "NAB")
    assert df.iloc[0]["amount"] == pytest.approx(-500.00)
    assert df.iloc[1]["amount"] == pytest.approx(2000.00)


def test_nab_date_format(tmp_path):
    """DD-Mon-YYYY dates parse correctly."""
    p = _write(tmp_path, "nab.csv", NAB_CSV)
    df = parse_nab_csv(p, "NAB")
    assert df.iloc[0]["date"].date().isoformat() == "2024-01-09"


def test_nab_account_name(tmp_path):
    p = _write(tmp_path, "nab.csv", NAB_CSV)
    df = parse_nab_csv(p, "NAB Everyday")
    assert (df["account"] == "NAB Everyday").all()


def test_nab_missing_columns(tmp_path):
    p = _write(tmp_path, "bad.csv", "Col1,Col2\n1,2\n")
    df = parse_nab_csv(p, "NAB")
    assert df.empty


# ── OFX ───────────────────────────────────────────────────────────────────────

OFX_CONTENT = """\
    OFXHEADER:100
    DATA:OFXSGML
    VERSION:102
    SECURITY:NONE
    ENCODING:UTF-8
    CHARSET:1252
    COMPRESSION:NONE
    OLDFILEUID:NONE
    NEWFILEUID:NONE

    <OFX>
    <SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
    <DTSERVER>20240115120000[+10:00]</SONRS></SIGNONMSGSRSV1>
    <BANKMSGSRSV1><STMTTRNRS><STMTRS>
    <CURDEF>AUD
    <BANKTRANLIST>
    <DTSTART>20240101000000[+10:00]
    <DTEND>20240131000000[+10:00]
    <STMTTRN>
    <TRNTYPE>DEBIT
    <DTPOSTED>20240109120000[+10:00]
    <TRNAMT>-500.00
    <FITID>20240109EFTPOS0001
    <MEMO>EFTPOS WOOLWORTHS SYDNEY NS
    </STMTTRN>
    <STMTTRN>
    <TRNTYPE>CREDIT
    <DTPOSTED>20240109120000[+10:00]
    <TRNAMT>2000.00
    <FITID>20240109SALARY0001
    <MEMO>SALARY CREDIT ACME PTY LTD
    </STMTTRN>
    <STMTTRN>
    <TRNTYPE>DEBIT
    <DTPOSTED>20240115120000[+10:00]
    <TRNAMT>-45.50
    <FITID>20240115DEBIT0001
    <MEMO>NETFLIX.COM
    </STMTTRN>
    </BANKTRANLIST>
    </STMTRS></STMTTRNRS></BANKMSGSRSV1>
    </OFX>
    """


def test_ofx_parses_rows(tmp_path):
    p = _write(tmp_path, "transactions.ofx", OFX_CONTENT)
    df = parse_ofx(p, "CommBank")
    assert len(df) == 3


def test_ofx_amounts(tmp_path):
    p = _write(tmp_path, "transactions.ofx", OFX_CONTENT)
    df = parse_ofx(p, "CommBank")
    assert df.iloc[0]["amount"] == pytest.approx(-500.00)
    assert df.iloc[1]["amount"] == pytest.approx(2000.00)


def test_ofx_date(tmp_path):
    p = _write(tmp_path, "transactions.ofx", OFX_CONTENT)
    df = parse_ofx(p, "CommBank")
    assert df.iloc[0]["date"].date().isoformat() == "2024-01-09"


def test_ofx_description(tmp_path):
    p = _write(tmp_path, "transactions.ofx", OFX_CONTENT)
    df = parse_ofx(p, "CommBank")
    assert "WOOLWORTHS" in df.iloc[0]["description"]


def test_ofx_account_name(tmp_path):
    p = _write(tmp_path, "transactions.ofx", OFX_CONTENT)
    df = parse_ofx(p, "My CommBank", "transaction")
    assert (df["account"] == "My CommBank").all()


def test_ofx_empty_file(tmp_path):
    p = _write(tmp_path, "empty.ofx", "OFXHEADER:100\n")
    df = parse_ofx(p, "Bank")
    assert df.empty


# ── _detect_file_type sniffing ────────────────────────────────────────────────

def test_detect_commbank_csv(tmp_path):
    p = _write(tmp_path, "export.csv", CBA_CSV)
    ft, _ = _detect_file_type(p, {})
    assert ft == "commbank_csv"


def test_detect_westpac_csv(tmp_path):
    p = _write(tmp_path, "export.csv", WESTPAC_CSV)
    ft, _ = _detect_file_type(p, {})
    assert ft == "westpac_csv"


def test_detect_nab_csv(tmp_path):
    p = _write(tmp_path, "export.csv", NAB_CSV)
    ft, _ = _detect_file_type(p, {})
    assert ft == "nab_csv"


def test_detect_ofx_extension(tmp_path):
    p = _write(tmp_path, "export.ofx", OFX_CONTENT)
    ft, _ = _detect_file_type(p, {})
    assert ft == "ofx"


def test_detect_qfx_extension(tmp_path):
    """Quicken .qfx is the same format as OFX."""
    p = _write(tmp_path, "export.qfx", OFX_CONTENT)
    ft, _ = _detect_file_type(p, {})
    assert ft == "ofx"


def test_detect_config_type_commbank(tmp_path):
    """Explicit type in config overrides content sniffing."""
    p = _write(tmp_path, "myfile.csv", CBA_CSV)
    cfg = {"accounts": {"cba": {"type": "commbank_csv", "display_name": "CBA", "file_pattern": "myfile.csv"}}}
    ft, acct = _detect_file_type(p, cfg)
    assert ft == "commbank_csv"
    assert acct["display_name"] == "CBA"


def test_detect_config_type_westpac(tmp_path):
    p = _write(tmp_path, "wp.csv", WESTPAC_CSV)
    cfg = {"accounts": {"wp": {"type": "westpac_csv", "display_name": "Westpac", "file_pattern": "wp.csv"}}}
    ft, _ = _detect_file_type(p, cfg)
    assert ft == "westpac_csv"


def test_detect_config_type_nab(tmp_path):
    p = _write(tmp_path, "nab.csv", NAB_CSV)
    cfg = {"accounts": {"nab": {"type": "nab_csv", "display_name": "NAB", "file_pattern": "nab.csv"}}}
    ft, _ = _detect_file_type(p, cfg)
    assert ft == "nab_csv"


def test_detect_config_type_ofx(tmp_path):
    p = _write(tmp_path, "bank.csv", OFX_CONTENT)
    cfg = {"accounts": {"ofx": {"type": "ofx", "display_name": "Bank", "file_pattern": "bank.csv"}}}
    ft, _ = _detect_file_type(p, cfg)
    assert ft == "ofx"
