"""
Detects PayPal Pay in 4 plans from the transaction database.

Each plan links:
  - One PayPal merchant purchase row  (e.g. "Virgin Australia" -$450.02)
  - Four PayPal instalment rows       ("PayPal Australia Pty Limited" ~$112.51 each)
  - Four ANZ bank debit rows          matching each instalment by date + amount

All three sets are already in the DB after a normal import. This module
reads the full transaction DataFrame and writes a JSON file so the
transactions page can show grouped, traceable Pay-in-4 records.
"""

import json
from pathlib import Path

import pandas as pd


_GROUPS_DEFAULT = "data/payin4_groups.json"
_INST_DESC = "PayPal Australia Pty Limited"
_SKIP_DESCS = {"PayPal", "", _INST_DESC}

# PayPal row types that represent real merchant purchases (not internal ledger rows)
_PURCHASE_NOTES = {
    "express checkout payment",
    "pre-approved payment bill user payment",
    "website payment",
    "general payment",
    "subscription payment",
    "order",
}


def detect_payin4_groups(df: pd.DataFrame) -> list[dict]:
    """Return a list of Pay-in-4 group dicts detected from the full transaction DataFrame.

    Detection strategy (purchase-first):
    1. Find PayPal merchant purchase rows (non-instalment, negative, completed).
    2. For each purchase, locate the first instalment at amount ≈ purchase/4 on
       the same or next day.
    3. Walk forward at 14-day intervals (±3 days) to collect instalments 2–4.
    4. Verify the four instalments sum to the purchase amount (±$0.05).
    5. For each PayPal instalment, find the matching ANZ bank debit (±5 days, ±$0.02).
    """
    if df.empty:
        return []

    paypal = df[df["account_type"] == "paypal"].copy()
    if paypal.empty:
        return []

    pending_col = "is_pending" if "is_pending" in paypal.columns else None

    def not_pending(sub):
        if pending_col:
            return sub[~sub[pending_col].astype(bool)]
        return sub

    purchases = not_pending(paypal[
        ~paypal["description"].str.strip().isin(_SKIP_DESCS)
        & (paypal["amount"] < 0)
        & paypal["note"].str.strip().str.lower().isin(_PURCHASE_NOTES)
    ]).copy()

    instalments = not_pending(paypal[
        (paypal["description"].str.strip() == _INST_DESC)
        & (paypal["amount"] < 0)
    ]).copy()

    # ANZ (and other non-PayPal) debit rows that reference PayPal
    anz_paypal = df[
        (df["account_type"] != "paypal")
        & df["description"].str.contains(r"PAYPAL|PYPL|PayPal", case=False, na=False)
        & (df["amount"] < 0)
    ].copy()

    groups: list[dict] = []
    used_inst: set[str] = set()
    used_anz: set[str] = set()

    for _, purchase in purchases.sort_values("date").iterrows():
        purchase_amount = abs(float(purchase["amount"]))
        if purchase_amount < 4.0:
            continue

        expected = purchase_amount / 4
        p_date = purchase["date"]
        tol = 0.52  # covers rounding between $0.00 and $0.51 per instalment

        firsts = instalments[
            ~instalments["txn_id"].isin(used_inst)
            & (abs(instalments["amount"] + expected) < tol)
            & (abs((instalments["date"] - p_date).dt.days) <= 1)
        ]

        for _, first in firsts.iterrows():
            inst_set = [first]
            used_temp: set[str] = {str(first["txn_id"])}

            for offset_days in [14, 28, 42]:
                target = first["date"] + pd.Timedelta(days=offset_days)
                nexts = instalments[
                    ~instalments["txn_id"].isin(used_inst | used_temp)
                    & (abs(instalments["amount"] + expected) < tol)
                    & (abs((instalments["date"] - target).dt.days) <= 3)
                ].copy()
                if nexts.empty:
                    break
                best = nexts.iloc[(nexts["date"] - target).abs().argsort().iloc[0]]
                inst_set.append(best)
                used_temp.add(str(best["txn_id"]))

            if len(inst_set) != 4:
                continue

            inst_total = sum(abs(float(r["amount"])) for r in inst_set)
            if abs(inst_total - purchase_amount) > 0.05:
                continue

            # Link each PayPal instalment to its ANZ bank debit
            details: list[dict] = []
            anz_temp: set[str] = set()
            for seq, inst in enumerate(inst_set, 1):
                inst_amt = abs(float(inst["amount"]))
                i_date = inst["date"]
                window = anz_paypal[
                    ~anz_paypal["txn_id"].isin(used_anz | anz_temp)
                    & (anz_paypal["date"] >= i_date - pd.Timedelta(days=5))
                    & (anz_paypal["date"] <= i_date + pd.Timedelta(days=5))
                    & (abs(abs(anz_paypal["amount"]) - inst_amt) < 0.02)
                ]
                anz_txn_id = anz_account = anz_desc = None
                if not window.empty:
                    best_anz = window.iloc[(window["date"] - i_date).abs().argsort().iloc[0]]
                    anz_txn_id  = str(best_anz["txn_id"])
                    anz_account = str(best_anz["account"])
                    anz_desc    = str(best_anz["description"])
                    anz_temp.add(anz_txn_id)

                details.append({
                    "sequence":        seq,
                    "date":            i_date.strftime("%Y-%m-%d"),
                    "amount":          round(inst_amt, 2),
                    "paypal_txn_id":   str(inst["txn_id"]),
                    "anz_txn_id":      anz_txn_id,
                    "anz_account":     anz_account,
                    "anz_description": anz_desc,
                })

            used_inst.update(used_temp)
            used_anz.update(anz_temp)

            anz_matched = sum(1 for d in details if d["anz_txn_id"])
            groups.append({
                "group_id":        str(purchase["txn_id"]),
                "merchant":        str(purchase["description"]),
                "total_amount":    round(purchase_amount, 2),
                "purchase_date":   p_date.strftime("%Y-%m-%d"),
                "purchase_txn_id": str(purchase["txn_id"]),
                "instalments":     details,
                "anz_total":       round(sum(d["amount"] for d in details), 2),
                "anz_matched":     anz_matched,
                "status":          "complete" if anz_matched == 4 else "partial",
            })
            break

    return groups


def merge_groups(existing: list[dict], new_groups: list[dict]) -> list[dict]:
    """Append new groups not already present (by group_id)."""
    existing_ids = {g["group_id"] for g in existing}
    return existing + [g for g in new_groups if g["group_id"] not in existing_ids]


def load_payin4_groups(config: dict) -> list[dict]:
    path = Path(config.get("data", {}).get("payin4_groups_file", _GROUPS_DEFAULT))
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("groups", [])
        except Exception:
            pass
    return []


def save_payin4_groups(groups: list[dict], config: dict) -> None:
    path = Path(config.get("data", {}).get("payin4_groups_file", _GROUPS_DEFAULT))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"groups": groups}, indent=2), encoding="utf-8")
