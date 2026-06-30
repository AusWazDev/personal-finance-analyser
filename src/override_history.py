"""
Override audit trail and undo support.

History is stored in the SQLite override_history table.
A legacy JSON file (data/override_history.json) is read for backward
compatibility but no longer written.
"""

from __future__ import annotations

import json
from pathlib import Path

from src import db as _db


def append_batch(batch: dict, config: dict) -> None:
    """Persist a change batch to SQLite override_history."""
    _db.append_override_batch(batch, config)


def get_history(config: dict, limit: int = 20) -> list[dict]:
    """Return recent non-undone batches from SQLite, most recent first."""
    return _db.get_override_history(config, limit=limit)


def undo_batch(batch_id: str, config: dict) -> dict:
    """
    Reverse all changes recorded in a batch.

    SQLite transactions table is restored via db.undo_override_batch.
    Also cleans up categorisation_cache.json and merchant_rules.json
    for any apply_to_all entries (these remain as JSON side-files).
    """
    changes = _db.undo_override_batch(batch_id, config)
    if changes is None:
        return {"ok": False, "error": "batch not found or already undone"}

    undone = len([c for c in changes if c.get("field") in {
        "category", "sub_category", "is_business", "user_note"
    }])

    # Clean up apply_to_all entries from JSON cache files
    _cleanup_cache(changes, config)
    _cleanup_merchant_rules(changes, config)

    return {"ok": True, "undone": undone}


def _cleanup_cache(changes: list[dict], config: dict) -> None:
    cache_path = Path(config.get("data", {}).get(
        "cache_file", "data/categorisation_cache.json"
    ))
    if not cache_path.exists():
        return
    cache = json.loads(cache_path.read_text("utf-8"))
    changed = False
    for chg in changes:
        if chg.get("apply_to_all") and chg.get("cache_key"):
            if chg["cache_key"] in cache:
                del cache[chg["cache_key"]]
                changed = True
    if changed:
        cache_path.write_text(json.dumps(cache, indent=2), "utf-8")


def _cleanup_merchant_rules(changes: list[dict], config: dict) -> None:
    rules_path = Path(config.get("data", {}).get(
        "merchant_rules_file", "data/merchant_rules.json"
    ))
    if not rules_path.exists():
        return
    rules = json.loads(rules_path.read_text("utf-8"))
    changed = False
    for chg in changes:
        if chg.get("apply_to_all") and chg.get("merchant_key"):
            if chg["merchant_key"] in rules:
                del rules[chg["merchant_key"]]
                changed = True
    if changed:
        rules_path.write_text(json.dumps(rules, indent=2, sort_keys=True), "utf-8")
