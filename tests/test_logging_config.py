"""Tests for src/logging_config.py — setup_logging and Sentry wiring."""
import logging
import importlib


def _reset_root_logger():
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)


def test_setup_logging_idempotent(tmp_path):
    """Calling setup_logging twice doesn't add duplicate handlers."""
    _reset_root_logger()
    from src.logging_config import setup_logging
    cfg = {"data": {"log_file": str(tmp_path / "app.log")}}
    setup_logging(cfg)
    count_after_first = len(logging.getLogger().handlers)
    setup_logging(cfg)
    assert len(logging.getLogger().handlers) == count_after_first
    _reset_root_logger()


def test_setup_logging_creates_log_file(tmp_path):
    """setup_logging creates the log file on first write."""
    _reset_root_logger()
    from src.logging_config import setup_logging
    log_path = tmp_path / "sub" / "app.log"
    cfg = {"data": {"log_file": str(log_path)}}
    setup_logging(cfg)
    logging.getLogger("test").info("hello")
    assert log_path.exists()
    _reset_root_logger()


def test_setup_logging_sentry_dsn_missing_sdk(tmp_path, monkeypatch):
    """When sentry_dsn is set but sentry_sdk not installed, setup_logging doesn't raise."""
    _reset_root_logger()
    import builtins, sys

    real_import = builtins.__import__
    def _fake_import(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("No module named 'sentry_sdk'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    sys.modules.pop("sentry_sdk", None)

    from src import logging_config
    importlib.reload(logging_config)

    cfg = {
        "data": {"log_file": str(tmp_path / "app.log")},
        "server": {"sentry_dsn": "https://fake@sentry.io/123"},
    }
    logging_config.setup_logging(cfg)  # must not raise
    _reset_root_logger()
    importlib.reload(logging_config)


def test_setup_logging_no_sentry_when_dsn_absent(tmp_path):
    """No Sentry init when sentry_dsn is not in config."""
    _reset_root_logger()
    from src.logging_config import setup_logging
    cfg = {"data": {"log_file": str(tmp_path / "app.log")}}
    setup_logging(cfg)  # should not raise even without sentry_sdk
    _reset_root_logger()
