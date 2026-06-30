"""Centralised logging setup — call setup_logging() once at startup."""
import logging
import logging.handlers
from pathlib import Path


def setup_logging(config: dict | None = None) -> None:
    """Configure root logger with stdout (plain) and rotating file handlers.

    Safe to call multiple times — exits early if handlers already attached.
    Log path comes from config['data']['log_file'] or defaults to Data/app.log.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)

    # stdout — plain message only so SSE / terminal output looks identical to print()
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(sh)

    # rotating file — timestamped with module name for debugging
    log_path = Path((config or {}).get("data", {}).get("log_file", "Data/app.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Sentry error reporting (opt-in via server.sentry_dsn in config.yaml)
    _dsn = ((config or {}).get("server") or {}).get("sentry_dsn", "")
    if _dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.logging import LoggingIntegration
            sentry_sdk.init(
                dsn=_dsn,
                integrations=[LoggingIntegration(level=logging.ERROR, event_level=logging.ERROR)],
                traces_sample_rate=0.0,
            )
            logging.getLogger(__name__).info("Sentry error reporting active.")
        except ImportError:
            logging.getLogger(__name__).warning(
                "sentry_dsn configured but sentry-sdk is not installed — "
                "run: pip install sentry-sdk"
            )
