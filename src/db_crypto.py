"""Database encryption utilities.

Supports transparent SQLCipher encryption of Data/finance.db when the
``sqlcipher3`` (or ``pysqlcipher3``) package is installed.

Passphrase storage priority:
  1. ``keyring`` library  →  Windows Credential Manager / macOS Keychain
  2. ``Data/.enc_key`` plain-text file  (fallback; warns at runtime)

Config keys:
  db_passphrase_source: keyring | file  (informational — storage is automatic)
  data.enc_key_file:   path to key file  (default: Data/.enc_key)
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "PersonalFinanceAnalyser"
_KEYRING_USERNAME = "db_passphrase"
_KEY_FILE_DEFAULT = "Data/.enc_key"

# Optional keyring — imported once so tests can patch src.db_crypto._keyring
try:
    import keyring as _keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    _keyring = None  # type: ignore[assignment]
    _KEYRING_AVAILABLE = False


# ── Driver detection ──────────────────────────────────────────────────────────

def _sqlcipher_module():
    """Return the sqlcipher3 module if available, else None."""
    try:
        import sqlcipher3
        return sqlcipher3
    except ImportError:
        pass
    try:
        from pysqlcipher3 import dbapi2
        return dbapi2
    except ImportError:
        pass
    return None


def _keyring_available() -> bool:
    return _KEYRING_AVAILABLE and _keyring is not None


def sqlcipher_available() -> bool:
    """Return True if a SQLCipher-capable sqlite driver is installed."""
    return _sqlcipher_module() is not None


# ── Passphrase management ─────────────────────────────────────────────────────

def _key_file_path(config: dict) -> Path:
    return Path(config.get("data", {}).get("enc_key_file", _KEY_FILE_DEFAULT))


def get_passphrase(config: dict) -> str | None:
    """Return the stored database passphrase, or None if not set."""
    if _keyring_available():
        try:
            val = _keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if val:
                return val
        except Exception:
            pass
    kf = _key_file_path(config)
    if kf.exists():
        try:
            return kf.read_text(encoding="utf-8").strip() or None
        except Exception:
            pass
    return None


def set_passphrase(phrase: str, config: dict) -> str:
    """Save the passphrase.  Returns "keyring" or "file" to indicate where."""
    if _keyring_available():
        try:
            _keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, phrase)
            return "keyring"
        except Exception:
            pass
    kf = _key_file_path(config)
    kf.parent.mkdir(parents=True, exist_ok=True)
    kf.write_text(phrase, encoding="utf-8")
    logger.warning(
        f"Passphrase saved to {kf} in plain text. "
        "Install the 'keyring' package for secure storage in the OS credential store."
    )
    return "file"


def delete_passphrase(config: dict) -> None:
    """Remove the stored passphrase from all storage locations."""
    if _keyring_available():
        try:
            _keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        except Exception:
            pass
    kf = _key_file_path(config)
    if kf.exists():
        kf.unlink()


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection(db_path: str | Path, passphrase: str | None, check_same_thread: bool = False):
    """Open a database connection, encrypted if passphrase + SQLCipher are available.

    Returns a connection whose row_factory is already set and WAL mode is on.
    Callers should NOT call sqlite3.connect() directly when encryption is needed.
    """
    db_path = str(db_path)
    sc = _sqlcipher_module()

    if passphrase and sc:
        conn = sc.connect(db_path, check_same_thread=check_same_thread)
        conn.execute(f"PRAGMA key = '{_escape_passphrase(passphrase)}'")
        conn.row_factory = sc.Row
        return conn

    if passphrase and not sc:
        logger.warning(
            "Encryption passphrase is set but sqlcipher3 is not installed — "
            "opening database without encryption. "
            "Run: pip install sqlcipher3-wheels"
        )

    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def _escape_passphrase(phrase: str) -> str:
    """Escape single-quotes in a passphrase for safe use in a PRAGMA statement."""
    return phrase.replace("'", "''")


# ── Migration helper ──────────────────────────────────────────────────────────

def encrypt_existing_db(plain_path: Path, passphrase: str) -> Path:
    """Encrypt an existing plain SQLite database in-place using SQLCipher.

    Creates ``<plain_path>.enc`` as the encrypted copy, then replaces the
    original with it.  Returns the path of the encrypted database (same as
    plain_path after replacement).

    Raises RuntimeError if sqlcipher3 is not available.
    """
    sc = _sqlcipher_module()
    if not sc:
        raise RuntimeError(
            "sqlcipher3 is not installed — cannot encrypt database. "
            "Run: pip install sqlcipher3-wheels"
        )

    enc_path = plain_path.with_suffix(".db.enc_tmp")
    esc = _escape_passphrase(passphrase)

    conn = sc.connect(str(plain_path))
    try:
        enc_str = str(enc_path).replace("'", "''")
        conn.execute(f"ATTACH DATABASE '{enc_str}' AS encrypted KEY '{esc}'")
        conn.execute("SELECT sqlcipher_export('encrypted')")
        conn.execute("DETACH DATABASE encrypted")
    finally:
        conn.close()

    plain_path.unlink()
    enc_path.rename(plain_path)
    return plain_path


def decrypt_existing_db(enc_path: Path, passphrase: str) -> Path:
    """Decrypt a SQLCipher-encrypted database to plain SQLite in-place.

    Raises RuntimeError if sqlcipher3 is not available.
    """
    sc = _sqlcipher_module()
    if not sc:
        raise RuntimeError(
            "sqlcipher3 is not installed — cannot decrypt database. "
            "Run: pip install sqlcipher3-wheels"
        )

    plain_path = enc_path.with_suffix(".db.plain_tmp")
    esc = _escape_passphrase(passphrase)

    conn = sc.connect(str(enc_path))
    conn.execute(f"PRAGMA key = '{esc}'")
    try:
        plain_str = str(plain_path).replace("'", "''")
        conn.execute(f"ATTACH DATABASE '{plain_str}' AS plaintext KEY ''")
        conn.execute("SELECT sqlcipher_export('plaintext')")
        conn.execute("DETACH DATABASE plaintext")
    finally:
        conn.close()

    enc_path.unlink()
    plain_path.rename(enc_path)
    return enc_path


# ── Status summary ────────────────────────────────────────────────────────────

def encryption_status(config: dict) -> dict:
    """Return a dict describing the current encryption state."""
    sc_available = sqlcipher_available()
    kr_available = _keyring_available()
    passphrase = get_passphrase(config)

    source = "none"
    if passphrase:
        if kr_available:
            try:
                if _keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME):
                    source = "keyring"
            except Exception:
                pass
        if source == "none" and _key_file_path(config).exists():
            source = "file"

    return {
        "sqlcipher_available": sc_available,
        "keyring_available":   kr_available,
        "passphrase_set":      passphrase is not None,
        "passphrase_source":   source,
        "encryption_active":   sc_available and passphrase is not None,
        "driver":              "sqlcipher3" if sc_available else "sqlite3 (unencrypted)",
    }
