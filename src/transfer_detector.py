"""
Detects potential transfer pairs: transactions of equal-and-opposite amounts
within a time window that may represent short-term loans or reimbursements.

Three detection modes:
  find_transfer_pairs()       — cross-account debit/credit matching
  detect_family_loan_pairs()  — same-account in/out matching for configured contacts
"""

import json
import re
from pathlib import Path

import pandas as pd


_CANDIDATES_DEFAULT = "data/transfer_candidates.json"

_SKIP_CATS = {
    "Income", "Board & Lodging", "Gifts Received", "Interest Income",
    "Business Reimbursement", "Family Loan Received", "Family Loan Repayment",
    "Investment", "Transfers",
}

_LABELS = ["Family Loan", "Reimbursement", "Internal Transfer", "Refund", "Other Transfer"]


def _pair_id(id_a: str, id_b: str) -> str:
    return f"{min(id_a, id_b)}|{max(id_a, id_b)}"


def _own_account_names(config: dict) -> set[str]:
    return set(config.get("own_accounts", []))


def _kw_match(series: pd.Series, kws: list[str]) -> pd.Series:
    return series.str.upper().apply(lambda d: any(k in d for k in kws))


def _txn_dict(row: pd.Series) -> dict:
    return {
        "txn_id":      str(row["txn_id"]),
        "date":        row["date"].strftime("%Y-%m-%d"),
        "description": str(row.get("description", "")),
        "account":     str(row.get("account", "")),
        "category":    str(row.get("category", "")),
        "amount":      float(row["amount"]),
    }


def find_transfer_pairs(df: pd.DataFrame, window_days: int = 60, config: dict | None = None) -> list[dict]:
    """Scan df for debit/credit pairs of equal amounts within window_days.

    When own_accounts is configured:
      - Both pools are restricted to own accounts (payments to external people and
        purchases never generate credits in own accounts, so no false pairs form).
      - _SKIP_CATS is NOT applied as a pool pre-filter. For ≤3-day pairs the
        account+amount+date triple is definitive proof of a transfer; a miscategorised
        row (e.g. Income or Family Loan Repayment on one side) should not block it.
      - For wider pending pairs (>3 days), _SKIP_CATS is checked per-pair to filter noise.
      - Already-confirmed transfers (category="Transfers" on either side) are always
        excluded to prevent re-detecting pairs already processed in a previous run.

    When own_accounts is not configured, _SKIP_CATS is applied as a pool pre-filter
    (original behaviour).
    """
    if df.empty:
        return []

    own_accounts = _own_account_names(config) if config else set()

    if own_accounts:
        acct_ok = df["account"].isin(own_accounts)
        # No category pre-filter — _SKIP_CATS applied per-pair in the inner loop.
        debits  = df[(df["amount"] < 0) & acct_ok].copy()
        credits = df[(df["amount"] > 0) & acct_ok].copy()
    else:
        cat_ok  = ~df["category"].isin(_SKIP_CATS)
        debits  = df[(df["amount"] < 0) & cat_ok].copy()
        credits = df[(df["amount"] > 0) & cat_ok].copy()

    if debits.empty or credits.empty:
        return []

    pairs: list[dict] = []
    seen: set[str] = set()

    for _, dr in debits.iterrows():
        amount = abs(float(dr["amount"]))
        if amount < 10:
            continue
        date_lo = dr["date"] - pd.Timedelta(days=window_days)
        date_hi = dr["date"] + pd.Timedelta(days=window_days)

        matches = credits[
            (credits["date"] >= date_lo)
            & (credits["date"] <= date_hi)
            & (abs(credits["amount"] - amount) < 0.02)
            & (credits["account"] != dr["account"])
        ]

        for _, cr in matches.iterrows():
            pid = _pair_id(str(dr["txn_id"]), str(cr["txn_id"]))
            if pid in seen:
                continue

            days_apart = int(abs((cr["date"] - dr["date"]).days))

            if (own_accounts
                    and dr["account"] in own_accounts
                    and cr["account"] in own_accounts
                    and days_apart <= 3):
                # Auto-confirm: own accounts + tight window is definitive.
                # Skip only if either side is already confirmed (category="Transfers")
                # to avoid re-detecting pairs processed in a previous run.
                if dr["category"] == "Transfers" or cr["category"] == "Transfers":
                    continue
                status = "confirmed"
                label  = "Internal Transfer"
                conf   = 10
                note   = "Auto-confirmed: both accounts are own accounts"
            elif dr["category"] not in _SKIP_CATS and cr["category"] not in _SKIP_CATS:
                # Wider window: apply _SKIP_CATS per-pair as a noise filter.
                status = "pending"
                label  = "Family Loan"
                conf   = None
                note   = ""
            else:
                continue

            seen.add(pid)
            pairs.append({
                "pair_id":       pid,
                "status":        status,
                "amount":        round(amount, 2),
                "days_apart":    days_apart,
                "txn_a":         _txn_dict(dr),
                "txn_b":         _txn_dict(cr),
                "ai_confidence": conf,
                "ai_note":       note,
                "label":         label,
            })

    return pairs


def detect_family_loan_pairs(df: pd.DataFrame, config: dict) -> list[dict]:
    """Scenario 2: same-account in/out matching for configured family loan contacts.

    Credits from the contact paired with debits to the contact within window_days.
    When total credits ≈ total debits the pairs are auto-confirmed as Family Loan.
    """
    contacts = config.get("family_loans", {}).get("contacts", [])
    if not contacts or df.empty:
        return []

    pairs: list[dict] = []
    seen: set[str] = set()

    for contact in contacts:
        name     = contact.get("name", "")
        in_kws   = [k.upper() for k in contact.get("in_keywords", [])]
        out_kws  = [k.upper() for k in contact.get("out_keywords", [])]
        acct     = contact.get("account", "")
        window   = contact.get("window_days", 120)

        if not in_kws or not out_kws:
            continue

        sub = df[df["account"] == acct].copy() if acct else df.copy()
        if sub.empty:
            continue

        pos = sub[sub["amount"] > 0]
        neg = sub[sub["amount"] < 0]
        credits = pos[_kw_match(pos["description"], in_kws)].copy()
        debits  = neg[_kw_match(neg["description"], out_kws)].copy()

        if credits.empty or debits.empty:
            continue

        total_in  = credits["amount"].sum()
        total_out = abs(debits["amount"].sum())
        loan_balanced = abs(total_in - total_out) < 0.02

        status  = "confirmed" if loan_balanced else "pending"
        ai_note = (
            f"Family loan: {name}. "
            f"Total in ${total_in:.2f} / out ${total_out:.2f} — "
            f"{'balanced' if loan_balanced else 'unbalanced'}."
        )
        conf = 9 if loan_balanced else 5

        for _, cr in credits.iterrows():
            cr_date = cr["date"]
            date_lo = cr_date - pd.Timedelta(days=window)
            date_hi = cr_date + pd.Timedelta(days=window)

            window_debits = debits[(debits["date"] >= date_lo) & (debits["date"] <= date_hi)]

            for _, dr in window_debits.iterrows():
                pid = _pair_id(str(cr["txn_id"]), str(dr["txn_id"]))
                if pid in seen:
                    continue
                seen.add(pid)

                pairs.append({
                    "pair_id":       pid,
                    "status":        status,
                    "amount":        round(abs(float(dr["amount"])), 2),
                    "days_apart":    int(abs((cr["date"] - dr["date"]).days)),
                    "txn_a":         _txn_dict(dr),
                    "txn_b":         _txn_dict(cr),
                    "ai_confidence": conf,
                    "ai_note":       ai_note,
                    "label":         "Family Loan",
                })

    return pairs


def score_pairs_with_ai(pairs: list[dict], config: dict, max_new: int = 20) -> list[dict]:
    """Add ai_confidence and ai_note to pending unscored pairs using Claude Haiku."""
    pending = [p for p in pairs if p.get("ai_confidence") is None and p["status"] == "pending"]
    if not pending:
        return pairs

    import os
    api_key = (config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        for p in pending:
            p["ai_confidence"] = -1
        return pairs

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        return pairs

    for pair in pending[:max_new]:
        a, b = pair["txn_a"], pair["txn_b"]
        prompt = (
            f"Are these two bank transactions a matched transfer (e.g. loan, reimbursement, "
            f"short-term lending between people)? They have equal and opposite amounts.\n\n"
            f"OUT  {a['date']}  \"{a['description']}\"  [{a['account']}]  -${pair['amount']:.2f}\n"
            f"IN   {b['date']}  \"{b['description']}\"  [{b['account']}]  +${pair['amount']:.2f}\n"
            f"Days apart: {pair['days_apart']}\n\n"
            f"Reply with JSON only: "
            f'{{\"confidence\": 0-10, \"note\": \"one sentence\", '
            f'\"label\": \"Family Loan|Reimbursement|Internal Transfer|Refund|Coincidence\"}}'
        )
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                pair["ai_confidence"] = max(0, min(10, int(result.get("confidence", 5))))
                pair["ai_note"]       = str(result.get("note", ""))[:200]
                pair["label"]         = str(result.get("label", "Family Loan"))
        except Exception:
            pair["ai_confidence"] = -1
            pair["ai_note"]       = ""

    return pairs


def load_transfer_candidates(config: dict) -> dict:
    path = Path(config.get("data", {}).get("transfer_candidates_file", _CANDIDATES_DEFAULT))
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pairs": []}


def save_transfer_candidates(candidates: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("transfer_candidates_file", _CANDIDATES_DEFAULT))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")


def merge_candidates(existing: dict, new_pairs: list[dict]) -> dict:
    """Append new pairs that don't already exist (by pair_id).

    For pairs that already exist, if the new version is auto-confirmed (Internal Transfer
    or Family Loan confirmed) and the existing is still pending, upgrade the status.
    """
    existing_map = {p["pair_id"]: p for p in existing.get("pairs", [])}
    merged = list(existing.get("pairs", []))

    for p in new_pairs:
        pid = p["pair_id"]
        if pid not in existing_map:
            merged.append(p)
        elif p["status"] == "confirmed" and existing_map[pid]["status"] == "pending":
            # Upgrade existing pending pair to confirmed (e.g. now recognised as own-account)
            existing_map[pid].update({
                "status":        p["status"],
                "label":         p["label"],
                "ai_confidence": p["ai_confidence"],
                "ai_note":       p["ai_note"],
            })

    return {"pairs": merged}
