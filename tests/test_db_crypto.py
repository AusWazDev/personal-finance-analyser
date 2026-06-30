"""Tests for src/db_crypto.py."""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db_crypto import (
    _escape_passphrase,
    _key_file_path,
    delete_passphrase,
    encryption_status,
    get_connection,
    get_passphrase,
    set_passphrase,
    sqlcipher_available,
)


def _cfg(tmp_path: Path) -> dict:
    return {"data": {"enc_key_file": str(tmp_path / ".enc_key")}}


# ── sqlcipher_available ───────────────────────────────────────────────────────

def test_sqlcipher_available_returns_bool():
    result = sqlcipher_available()
    assert isinstance(result, bool)


def test_sqlcipher_not_available_when_package_missing():
    with patch("src.db_crypto._sqlcipher_module", return_value=None):
        assert sqlcipher_available() is False


def test_sqlcipher_available_when_package_present():
    mock_sc = MagicMock()
    with patch("src.db_crypto._sqlcipher_module", return_value=mock_sc):
        assert sqlcipher_available() is True


# ── passphrase — key file fallback ────────────────────────────────────────────

def test_get_passphrase_none_when_not_set(tmp_path):
    with patch("src.db_crypto._keyring_available", return_value=False):
        assert get_passphrase(_cfg(tmp_path)) is None


def test_set_passphrase_to_file_when_no_keyring(tmp_path):
    cfg = _cfg(tmp_path)
    with patch("src.db_crypto._keyring_available", return_value=False):
        saved_to = set_passphrase("secret123", cfg)
    assert saved_to == "file"
    kf = _key_file_path(cfg)
    assert kf.exists()
    assert kf.read_text().strip() == "secret123"


def test_get_passphrase_reads_from_file(tmp_path):
    cfg = _cfg(tmp_path)
    kf = _key_file_path(cfg)
    kf.write_text("mypassphrase\n", encoding="utf-8")
    with patch("src.db_crypto._keyring_available", return_value=False):
        assert get_passphrase(cfg) == "mypassphrase"


def test_delete_passphrase_removes_file(tmp_path):
    cfg = _cfg(tmp_path)
    kf = _key_file_path(cfg)
    kf.write_text("pass", encoding="utf-8")
    with patch("src.db_crypto._keyring_available", return_value=False):
        delete_passphrase(cfg)
    assert not kf.exists()


def test_delete_passphrase_noop_when_no_file(tmp_path):
    with patch("src.db_crypto._keyring_available", return_value=False):
        delete_passphrase(_cfg(tmp_path))  # should not raise


# ── passphrase — keyring path ─────────────────────────────────────────────────

def test_set_passphrase_uses_keyring_when_available(tmp_path):
    cfg = _cfg(tmp_path)
    mock_kr = MagicMock()
    with patch("src.db_crypto._keyring_available", return_value=True), \
         patch("src.db_crypto._keyring", mock_kr):
        saved_to = set_passphrase("secret", cfg)
    assert saved_to == "keyring"
    mock_kr.set_password.assert_called_once()


def test_get_passphrase_reads_from_keyring(tmp_path):
    cfg = _cfg(tmp_path)
    mock_kr = MagicMock()
    mock_kr.get_password.return_value = "from_keyring"
    with patch("src.db_crypto._keyring_available", return_value=True), \
         patch("src.db_crypto._keyring", mock_kr):
        val = get_passphrase(cfg)
    assert val == "from_keyring"


def test_get_passphrase_falls_back_to_file_if_keyring_empty(tmp_path):
    cfg = _cfg(tmp_path)
    kf = _key_file_path(cfg)
    kf.write_text("filephrase", encoding="utf-8")
    mock_kr = MagicMock()
    mock_kr.get_password.return_value = None  # keyring has nothing
    with patch("src.db_crypto._keyring_available", return_value=True), \
         patch("src.db_crypto._keyring", mock_kr):
        val = get_passphrase(cfg)
    assert val == "filephrase"


def test_delete_passphrase_clears_keyring(tmp_path):
    cfg = _cfg(tmp_path)
    mock_kr = MagicMock()
    with patch("src.db_crypto._keyring_available", return_value=True), \
         patch("src.db_crypto._keyring", mock_kr):
        delete_passphrase(cfg)
    mock_kr.delete_password.assert_called_once()


# ── _escape_passphrase ────────────────────────────────────────────────────────

def test_escape_passphrase_no_quotes():
    assert _escape_passphrase("hello") == "hello"


def test_escape_passphrase_escapes_single_quotes():
    assert _escape_passphrase("it's") == "it''s"


def test_escape_passphrase_multiple_quotes():
    assert _escape_passphrase("a'b'c") == "a''b''c"


# ── get_connection ────────────────────────────────────────────────────────────

def test_get_connection_plain_no_passphrase(tmp_path):
    db = tmp_path / "test.db"
    conn = get_connection(db, passphrase=None)
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_get_connection_plain_no_sqlcipher(tmp_path):
    db = tmp_path / "test.db"
    with patch("src.db_crypto._sqlcipher_module", return_value=None):
        conn = get_connection(db, passphrase="secret")
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_get_connection_with_sqlcipher_and_passphrase(tmp_path):
    db = tmp_path / "test.db"
    mock_conn = MagicMock()
    mock_sc = MagicMock()
    mock_sc.connect.return_value = mock_conn
    mock_sc.Row = sqlite3.Row

    with patch("src.db_crypto._sqlcipher_module", return_value=mock_sc):
        conn = get_connection(db, passphrase="mysecret")

    mock_sc.connect.assert_called_once_with(str(db), check_same_thread=False)
    pragma_call = mock_conn.execute.call_args_list[0]
    assert "PRAGMA key" in pragma_call[0][0]
    assert "mysecret" in pragma_call[0][0]
    assert conn is mock_conn


def test_get_connection_with_sqlcipher_escapes_passphrase(tmp_path):
    db = tmp_path / "test.db"
    mock_conn = MagicMock()
    mock_sc = MagicMock()
    mock_sc.connect.return_value = mock_conn
    mock_sc.Row = sqlite3.Row

    with patch("src.db_crypto._sqlcipher_module", return_value=mock_sc):
        get_connection(db, passphrase="it's")

    pragma_call = mock_conn.execute.call_args_list[0][0][0]
    assert "it''s" in pragma_call


# ── encryption_status ─────────────────────────────────────────────────────────

def test_encryption_status_inactive_no_sqlcipher(tmp_path):
    with patch("src.db_crypto._sqlcipher_module", return_value=None), \
         patch("src.db_crypto._keyring_available", return_value=False):
        st = encryption_status(_cfg(tmp_path))
    assert st["encryption_active"] is False
    assert st["sqlcipher_available"] is False


def test_encryption_status_active_when_sqlcipher_and_passphrase(tmp_path):
    cfg = _cfg(tmp_path)
    _key_file_path(cfg).write_text("pass", encoding="utf-8")
    mock_sc = MagicMock()
    with patch("src.db_crypto._sqlcipher_module", return_value=mock_sc), \
         patch("src.db_crypto._keyring_available", return_value=False):
        st = encryption_status(cfg)
    assert st["encryption_active"] is True
    assert st["sqlcipher_available"] is True
    assert st["passphrase_set"] is True


def test_encryption_status_passphrase_source_file(tmp_path):
    cfg = _cfg(tmp_path)
    _key_file_path(cfg).write_text("pass", encoding="utf-8")
    with patch("src.db_crypto._sqlcipher_module", return_value=None), \
         patch("src.db_crypto._keyring_available", return_value=False):
        st = encryption_status(cfg)
    assert st["passphrase_source"] == "file"
    assert st["passphrase_set"] is True
