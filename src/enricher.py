"""
PayPal enrichment.

ANZ shows PayPal debits as:
  "PAYMENT TO PAYPAL AUSTRALIA 1049883670375"
  "PYPL PAYIN4      1049804895023"

This module matches those entries to a PayPal CSV export by date + amount,
then replaces the vague ANZ description with the real PayPal merchant name.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def _write_run_metrics(updates: dict, config: dict) -> None:
    metrics_path = Path(config.get("data", {}).get("run_metrics_file", "Data/run_metrics.json"))
    try:
        existing = json.loads(metrics_path.read_text("utf-8")) if metrics_path.exists() else {}
    except Exception:
        existing = {}
    existing.update(updates)
    existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(existing, indent=2), "utf-8")
    except Exception:
        pass


def enrich_paypal_transactions(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Match ANZ PayPal entries against the PayPal export and enrich descriptions.
    Returns df with updated 'description' values for matched rows.
    """
    if df.empty:
        return df

    # PayPal rows from the dedicated PayPal account
    paypal_rows = df[df["account_type"] == "paypal"].copy()

    # ANZ rows that reference PayPal (exclude rows already enriched in a prior run)
    paypal_pattern = r"PAYPAL|PYPL"
    anz_paypal_mask = (
        df["description"].str.contains(paypal_pattern, case=False, na=False)
        & (df["account_type"] != "paypal")
        & (df["amount"] < 0)
        & ~df["description"].str.startswith("PayPal: ", na=False)
    )
    anz_paypal_idx = df.index[anz_paypal_mask].tolist()

    if paypal_rows.empty:
        if anz_paypal_idx:
            logger.info(f"  Note: {len(anz_paypal_idx)} PayPal entries in ANZ found; "
                        "add a PayPal CSV export to Data/Raw Data/ for merchant name enrichment.")
        _write_run_metrics({"paypal_unmatched": len(anz_paypal_idx)}, config)
        return df

    df = df.copy()
    matched = 0
    used_paypal_idx: set = set()  # prevent one PayPal row from enriching two ANZ rows

    # Sort ANZ rows by date so earlier debits get first pick of PayPal matches
    anz_paypal_idx_sorted = sorted(
        anz_paypal_idx,
        key=lambda i: df.loc[i, "date"],
    )

    for anz_idx in anz_paypal_idx_sorted:
        row = df.loc[anz_idx]
        anz_date: pd.Timestamp = row["date"]
        anz_amount: float = float(row["amount"])

        # Match by date (±5 days) and amount (within $0.02), excluding already-used rows
        date_lo = anz_date - pd.Timedelta(days=5)
        date_hi = anz_date + pd.Timedelta(days=5)

        matches = paypal_rows[
            ~paypal_rows.index.isin(used_paypal_idx)
            & (paypal_rows["date"] >= date_lo)
            & (paypal_rows["date"] <= date_hi)
            & (abs(paypal_rows["amount"] - anz_amount) < 0.02)
        ]

        if matches.empty:
            continue

        # Take closest date match
        best_pos = (matches["date"] - anz_date).abs().argsort().iloc[0]
        best = matches.iloc[best_pos]
        merchant = str(best["description"]).strip()

        if merchant and merchant.upper() not in ("PAYPAL", "", "PAYPAL AUSTRALIA PTY LIMITED"):
            original = df.at[anz_idx, "description"]
            df.at[anz_idx, "description"] = f"PayPal: {merchant}"
            # Keep original in reference for audit trail
            if not df.at[anz_idx, "reference"]:
                df.at[anz_idx, "reference"] = original
            used_paypal_idx.add(best.name)
            matched += 1

    unmatched = len(anz_paypal_idx) - matched
    _write_run_metrics({"paypal_unmatched": unmatched}, config)

    if matched:
        logger.info(f"  Enriched {matched} PayPal transactions with real merchant names")
    else:
        logger.info(f"  PayPal export loaded but no date+amount matches found "
                    "(timing difference > 5 days?)")

    return df


def find_paypal_hints(df: pd.DataFrame, max_hint_days: int = 45) -> dict[str, dict]:
    """
    For unmatched ANZ PayPal rows, find the closest PayPal CSV entry by amount.
    Used to show suggested matches on the review page for manual confirmation.
    Returns {txn_id: {"merchant", "date", "days_off", "amount"}}.
    """
    paypal_rows = df[df["account_type"] == "paypal"].copy()
    if paypal_rows.empty:
        return {}

    unmatched_mask = (
        df["description"].str.contains(r"PAYPAL|PYPL", case=False, na=False)
        & (df["account_type"] != "paypal")
        & (df["amount"] < 0)
        & ~df["description"].str.startswith("PayPal: ", na=False)
    )

    hints: dict[str, dict] = {}
    for _, row in df[unmatched_mask].iterrows():
        anz_date: pd.Timestamp = row["date"]
        anz_amount = float(row["amount"])
        txn_id = str(row.get("txn_id", ""))
        if not txn_id:
            continue

        amount_matches = paypal_rows[abs(paypal_rows["amount"] - anz_amount) < 0.02]
        if amount_matches.empty:
            continue

        sorted_idx = (amount_matches["date"] - anz_date).abs().argsort()
        best = amount_matches.iloc[sorted_idx.iloc[0]]
        days_off = int(abs((best["date"] - anz_date).days))

        if days_off <= max_hint_days:
            merchant = str(best["description"]).strip()
            if merchant and merchant.upper() not in ("PAYPAL", "", "PAYPAL AUSTRALIA PTY LIMITED"):
                hints[txn_id] = {
                    "merchant": merchant,
                    "date": best["date"].strftime("%Y-%m-%d"),
                    "days_off": days_off,
                    "amount": abs(float(best["amount"])),
                }

    return hints
