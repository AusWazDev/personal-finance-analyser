"""
Post-run archiver.

Intended sequence for a normal run:
  1. upsert_transactions() (src/db.py) — store new transactions in SQLite
  2. generate reports             — read purely from SQLite
  3. create_zip_and_clear()       — zip Raw Data and remove originals

Storage engine: SQLite (data/finance.db). There is no CSV fallback.
"""
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from src import db as _db

logger = logging.getLogger(__name__)

# ── Zip naming ─────────────────────────────────────────────────────────────────

def _zip_name(archive_dir: Path) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    name = f"{today}_archive.zip"
    counter = 1
    while (archive_dir / name).exists():
        name = f"{today}_archive_{counter}.zip"
        counter += 1
    return name


def plan_zip_name(config: dict) -> str:
    """
    Determine the zip filename for this run without creating it.
    Stored in transaction rows as zip_source for traceability.
    """
    archive_dir = Path(config["data"].get("archive_dir", "Data/Archive"))
    archive_dir.mkdir(parents=True, exist_ok=True)
    return _zip_name(archive_dir)


# ── File archiving ─────────────────────────────────────────────────────────────

def create_zip_and_clear(config: dict, zip_name: str) -> None:
    """
    Zip all files in Raw Data into archive_dir/zip_name, then delete the originals.
    Call this AFTER reports have been generated.
    """
    input_dir = Path(config["data"]["input_dir"])
    archive_dir = Path(config["data"].get("archive_dir", "Data/Archive"))
    archive_dir.mkdir(parents=True, exist_ok=True)

    raw_files: list[Path] = []
    for suffix in ("*.csv", "*.CSV", "*.html", "*.HTML", "*.pdf", "*.PDF"):
        raw_files.extend(input_dir.glob(suffix))
    raw_files = sorted(set(raw_files))

    if not raw_files:
        logger.info("  No files to archive.")
        return

    zip_path = archive_dir / zip_name
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in raw_files:
                zf.write(f, f.name)
        logger.info(f"  Zipped {len(raw_files)} files -> {zip_path.name}")
    except OSError as exc:
        logger.error(f"  ERROR: could not create zip {zip_name}: {exc}")
        logger.error("  Raw Data files NOT deleted.")
        return

    failed: list[str] = []
    for f in raw_files:
        try:
            f.unlink()
        except OSError as exc:
            failed.append(f.name)
            logger.warning(f"  WARNING: could not delete {f.name}: {exc}")
    removed = len(raw_files) - len(failed)
    logger.info(f"  Raw Data cleared ({removed} files removed"
                f"{f', {len(failed)} skipped' if failed else ''}).")


def archive_specific_files(files: list[Path], config: dict, zip_name: str) -> None:
    """Zip a specific list of Path objects and delete them from Raw Data."""
    if not files:
        return
    archive_dir = Path(config["data"].get("archive_dir", "Data/Archive"))
    archive_dir.mkdir(parents=True, exist_ok=True)
    zip_path = archive_dir / zip_name
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.name)
        logger.info(f"  Zipped {len(files)} files -> {zip_path.name}")
    except OSError as exc:
        logger.error(f"  ERROR: could not create zip {zip_name}: {exc}")
        return
    for f in files:
        try:
            f.unlink()
        except OSError as exc:
            logger.warning(f"  WARNING: could not delete {f.name}: {exc}")


def archive_and_update_master(df: "pd.DataFrame", config: dict) -> str:
    """
    Convenience wrapper: plan zip name -> upsert to DB -> zip -> clear.
    Returns the zip filename, or "" if there were no Raw Data files to archive.
    """
    import pandas as pd  # noqa: F401 — kept for callers that pass a DataFrame
    input_dir = Path(config["data"]["input_dir"])
    raw_files: list[Path] = []
    for suffix in ("*.csv", "*.CSV", "*.html", "*.HTML", "*.pdf", "*.PDF"):
        raw_files.extend(input_dir.glob(suffix))

    if not raw_files:
        _db.upsert_transactions(df, config, zip_name="")
        logger.info("  No files to archive.")
        return ""

    zip_name = plan_zip_name(config)
    _db.upsert_transactions(df, config, zip_name)
    create_zip_and_clear(config, zip_name)
    return zip_name
