"""
Applies overrides from a review JSON file (downloaded from reports/review.html).

Review JSON format:
[
  {
    "txn_id": "abc123",
    "category": "Groceries",
    "apply_to_all": true,       // update description-level cache too
    "description": "KMART ...", // original description (for cache key)
    "amount": -45.00            // sign for cache key (cr/dr)
  },
  ...
]
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from src import db as _db

logger = logging.getLogger(__name__)


def apply_entries(entries: list[dict], config: dict) -> dict[str, int]:
    """Apply category overrides from a list of entry dicts.

    Updates: transaction_overrides.json, categorisation_cache.json, SQLite.
    Returns {"txn_updated": n, "cache_updated": n, "master_updated": n}.
    """
    if not entries:
        return {"txn_updated": 0, "cache_updated": 0, "master_updated": 0}

    # ── JSON side-files (overrides + cache) ──────────────────────────────────
    overrides_path = Path(
        config.get("data", {}).get("overrides_file", "data/transaction_overrides.json")
    )
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides: dict = json.loads(overrides_path.read_text("utf-8")) \
        if overrides_path.exists() else {}

    cache_path = Path(config.get("data", {}).get(
        "cache_file", "data/categorisation_cache.json"
    ))
    cache: dict = json.loads(cache_path.read_text("utf-8")) \
        if cache_path.exists() else {}

    txn_count = 0
    cache_count = 0
    # desc_upper → category for apply_to_all entries
    desc_cat_map: dict[str, str] = {}

    for entry in entries:
        txn_id = entry.get("txn_id", "").strip()
        category = entry.get("category", "").strip()
        if not txn_id or not category:
            continue

        sub_category = str(entry.get("sub_category", "") or "").strip()
        overrides[txn_id] = {"category": category}
        if sub_category:
            overrides[txn_id]["sub_category"] = sub_category
        note = str(entry.get("note", "")).strip()
        if note:
            overrides[txn_id]["note"] = note
        txn_count += 1

        if entry.get("apply_to_all"):
            desc = str(entry.get("description", ""))
            amount = float(entry.get("amount", -1))
            sign = "cr" if amount >= 0 else "dr"
            cache_key = f"{desc.upper().strip()[:80]}|{sign}"
            existing = cache.get(cache_key, {})
            cache[cache_key] = {
                "category": category,
                "sub_category": sub_category,
                "business": existing.get("business", False),
            }
            cache_count += 1
            desc_cat_map[desc.upper().strip()] = category

    overrides_path.write_text(json.dumps(overrides, indent=2), "utf-8")
    if cache_count:
        cache_path.write_text(json.dumps(cache, indent=2), "utf-8")

    # Write merchant_rules.json for Re-categorise All
    if desc_cat_map:
        rules_path = Path(config.get("data", {}).get(
            "merchant_rules_file", "data/merchant_rules.json"
        ))
        try:
            user_rules: dict = json.loads(rules_path.read_text("utf-8")) \
                if rules_path.exists() else {}
            for desc_upper, cat in desc_cat_map.items():
                user_rules[desc_upper[:80]] = cat
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            rules_path.write_text(
                json.dumps(user_rules, indent=2, sort_keys=True), "utf-8"
            )
        except Exception:
            pass

    # ── SQLite updates ────────────────────────────────────────────────────────
    history_changes: list[dict] = []
    master_updated = 0

    conn = _db.get_db(config)
    _db.init_db(conn)
    try:
        for entry in entries:
            txn_id = entry.get("txn_id", "").strip()
            new_cat = entry.get("category", "").strip()
            if not txn_id or not new_cat:
                continue

            row = conn.execute(
                "SELECT category, description FROM transactions WHERE txn_id = ?",
                (txn_id,),
            ).fetchone()
            old_cat = row["category"] if row else ""
            desc = str(entry.get("description", "") or (row["description"] if row else ""))

            # Update the specific transaction
            new_sub = str(entry.get("sub_category", "") or "").strip()
            fields_to_set: list[str] = ["category = ?", "sub_category = ?"]
            values: list = [new_cat, new_sub]
            note = str(entry.get("note", "")).strip()
            if note:
                fields_to_set.append("user_note = ?")
                values.append(note)
            values.append(txn_id)
            conn.execute(
                f"UPDATE transactions SET {', '.join(fields_to_set)} WHERE txn_id = ?",
                values,
            )
            master_updated += 1

            is_all = bool(entry.get("apply_to_all"))
            amount = float(entry.get("amount", -1))
            sign = "cr" if amount >= 0 else "dr"
            history_changes.append({
                "txn_id": txn_id,
                "description": desc[:80],
                "field": "category",
                "old_value": old_cat,
                "new_value": new_cat,
                "apply_to_all": is_all,
                "cache_key": f"{desc.upper().strip()[:80]}|{sign}" if is_all else None,
                "merchant_key": desc.upper().strip()[:80] if is_all else None,
            })

            if is_all:
                desc_upper = desc.upper().strip()
                matches = conn.execute(
                    "SELECT txn_id, category FROM transactions "
                    "WHERE upper(trim(description)) = ? AND txn_id != ?",
                    (desc_upper, txn_id),
                ).fetchall()
                if matches:
                    bulk_ids = [r["txn_id"] for r in matches]
                    ph = ",".join("?" * len(bulk_ids))
                    conn.execute(
                        f"UPDATE transactions SET category = ?, sub_category = ? WHERE txn_id IN ({ph})",
                        [new_cat, new_sub] + bulk_ids,
                    )
                    master_updated += len(bulk_ids)
                    for r in matches:
                        history_changes.append({
                            "txn_id": r["txn_id"],
                            "description": desc[:80],
                            "field": "category",
                            "old_value": r["category"] or "",
                            "new_value": new_cat,
                            "apply_to_all": True,
                            "cache_key": None,
                            "merchant_key": None,
                        })

        conn.commit()
    finally:
        conn.close()

    # ── Audit batch ───────────────────────────────────────────────────────────
    if history_changes:
        from src.override_history import append_batch
        ts = datetime.now()
        bid = ts.strftime("%Y%m%d%H%M%S") + hashlib.md5(
            json.dumps(history_changes, sort_keys=True).encode()
        ).hexdigest()[:6]
        cats = sorted({c["new_value"] for c in history_changes if c["field"] == "category"})
        n = len({c["txn_id"] for c in history_changes})
        append_batch({
            "batch_id": bid,
            "applied_at": ts.isoformat(),
            "action": "category_override",
            "summary": f"Changed {n} transaction(s) → {', '.join(cats)}",
            "changes": history_changes,
        }, config)

    return {
        "txn_updated": txn_count,
        "cache_updated": cache_count,
        "master_updated": master_updated,
    }


def save_note(txn_id: str, note: str, config: dict) -> dict[str, int]:
    """Save or clear a user note for a single transaction."""
    note = note.strip()

    # Update transaction_overrides.json side-file
    overrides_path = Path(
        config.get("data", {}).get("overrides_file", "data/transaction_overrides.json")
    )
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides: dict = json.loads(overrides_path.read_text("utf-8")) \
        if overrides_path.exists() else {}
    if txn_id not in overrides:
        overrides[txn_id] = {}
    if note:
        overrides[txn_id]["note"] = note
    else:
        overrides[txn_id].pop("note", None)
    overrides_path.write_text(json.dumps(overrides, indent=2), "utf-8")

    # Fetch old note before overwriting (for undo history)
    conn = _db.get_db(config)
    _db.init_db(conn)
    try:
        row = conn.execute(
            "SELECT user_note FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        old_note = str(row["user_note"] or "") if row else ""

        cur = conn.execute(
            "UPDATE transactions SET user_note = ? WHERE txn_id = ?", (note, txn_id)
        )
        conn.commit()
        updated = cur.rowcount
    finally:
        conn.close()

    if updated and note != old_note:
        from src.override_history import append_batch
        ts = datetime.now()
        bid = ts.strftime("%Y%m%d%H%M%S") + hashlib.md5(txn_id.encode()).hexdigest()[:6]
        append_batch({
            "batch_id": bid,
            "applied_at": ts.isoformat(),
            "action": "note",
            "summary": f"Note updated for transaction {txn_id[:8]}",
            "changes": [{
                "txn_id": txn_id,
                "description": "",
                "field": "user_note",
                "old_value": old_note,
                "new_value": note,
                "apply_to_all": False,
                "cache_key": None,
                "merchant_key": None,
            }],
        }, config)

    return {"master_updated": updated}


def apply_review(review_path: str, config: dict) -> None:
    review_file = Path(review_path)
    if not review_file.exists():
        logger.error(f"  ERROR: Review file not found: {review_path}")
        return

    entries: list[dict] = json.loads(review_file.read_text("utf-8"))
    if not entries:
        logger.info("  Review file is empty — nothing to apply.")
        return

    result = apply_entries(entries, config)
    logger.info(f"  Saved {result['txn_updated']} txn_id override(s) -> transaction_overrides.json")
    if result["cache_updated"]:
        logger.info(f"  Updated {result['cache_updated']} description-level cache entry(ies)")
    if result["master_updated"]:
        logger.info(f"  Updated {result['master_updated']} row(s) in SQLite")
