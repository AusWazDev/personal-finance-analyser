"""Reimbursement batch tracker — data helpers."""

import json
from pathlib import Path

_DEFAULT_FILE = "data/reimbursement_batches.json"


def load_batches(config: dict) -> dict:
    path = Path(config.get("data", {}).get("reimbursement_batches_file", _DEFAULT_FILE))
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"batches": []}


def save_batches(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("reimbursement_batches_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def suggest_reimbursement_batches(
    expenses: list[dict],
    credits: list[dict],
    *,
    exact_tol: float = 1.00,
    near_pct: float = 0.05,
) -> list[dict]:
    """
    Period-based matching: each credit claims all expenses between it and the
    previous credit. Returns a list of suggestion dicts (may be empty).

    expenses / credits must be pre-filtered (batched/linked IDs already removed)
    and each row a plain dict with keys: txn_id, date, description, amount.
    """
    from datetime import datetime

    if not expenses:
        return []

    used_expense_ids: set = set()
    suggestions: list[dict] = []
    prev_date = datetime.min

    for credit in credits:
        credit_date   = datetime.fromisoformat(str(credit["date"])[:10])
        credit_amount = float(credit["amount"])

        candidates = [
            e for e in expenses
            if e["txn_id"] not in used_expense_ids
            and prev_date < datetime.fromisoformat(str(e["date"])[:10]) <= credit_date
        ]
        if not candidates:
            prev_date = credit_date
            continue

        expense_total = round(sum(abs(float(e["amount"])) for e in candidates), 2)
        diff = round(expense_total - credit_amount, 2)

        if abs(diff) <= exact_tol:
            quality = "exact"
        elif credit_amount > 0 and abs(diff) / credit_amount <= near_pct:
            quality = "near"
        else:
            quality = "partial"

        for e in candidates:
            used_expense_ids.add(e["txn_id"])

        prev_date = credit_date
        month_label = str(credit["date"])[:7]
        suggestions.append({
            "payment_txn_id":      credit["txn_id"],
            "payment_date":        str(credit["date"])[:10],
            "payment_description": credit["description"],
            "payment_amount":      credit_amount,
            "expense_txn_ids":     [e["txn_id"] for e in candidates],
            "expenses":            [dict(e) for e in candidates],
            "expense_total":       expense_total,
            "match_quality":       quality,
            "difference":          diff,
            "suggested_name":      f"Historical – {month_label}",
        })

    remaining = [e for e in expenses if e["txn_id"] not in used_expense_ids]
    if remaining:
        remaining_total = round(sum(abs(float(e["amount"])) for e in remaining), 2)
        suggestions.append({
            "payment_txn_id":      None,
            "payment_date":        None,
            "payment_description": None,
            "payment_amount":      None,
            "expense_txn_ids":     [e["txn_id"] for e in remaining],
            "expenses":            [dict(e) for e in remaining],
            "expense_total":       remaining_total,
            "match_quality":       "unmatched",
            "difference":          None,
            "suggested_name":      "Historical – Unreconciled",
        })

    return suggestions


def batch_status(batch: dict) -> str:
    if not batch.get("reimbursement_txn_id"):
        return "submitted"
    received = float(batch.get("received_amount") or 0)
    expected = float(batch.get("expense_total") or 0)
    diff = received - expected
    if abs(diff) < 0.02:
        return "reconciled"
    return "shortfall" if diff < 0 else "overpaid"
