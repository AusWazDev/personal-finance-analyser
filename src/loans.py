"""Loan tracking — borrowed and lent, with repayment history."""
import json
from pathlib import Path

_DEFAULT_FILE = "Data/loans.json"
LOAN_CATEGORIES = {"Family Loan Received", "Family Loan Repayment"}


def load_loans(config: dict) -> dict:
    path = Path(config.get("data", {}).get("loans_file", _DEFAULT_FILE))
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {"loans": []}


def save_loans(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("loans_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), "utf-8")


def calculate_loan_position(loan: dict, conn) -> dict:
    """Return repayment history and outstanding balance for one loan.

    Prefers explicitly linked txn_ids (linked_repayment_txn_ids) when present.
    Falls back to the legacy keyword filter (category_filter + description_filter)
    for records created before transaction linking was introduced.
    """
    principal  = float(loan.get("principal", 0) or 0)
    linked_ids = [t for t in (loan.get("linked_repayment_txn_ids") or []) if t]

    if linked_ids:
        placeholders = ",".join("?" * len(linked_ids))
        rows = conn.execute(
            f"SELECT date, description, amount, account FROM transactions "
            f"WHERE txn_id IN ({placeholders}) ORDER BY date ASC",
            linked_ids,
        ).fetchall()

        repayments = [{
            "date":        r["date"],
            "description": r["description"],
            "amount":      round(abs(float(r["amount"])), 2),
            "account":     r["account"] or "",
        } for r in rows]

        total_repaid = round(sum(r["amount"] for r in repayments), 2)
        outstanding  = round(max(0.0, principal - total_repaid), 2)
        pct          = round(min(100.0, (total_repaid / principal * 100) if principal > 0 else 0.0), 1)

        running = principal
        for r in repayments:
            running      -= r["amount"]
            r["balance"]  = round(max(0.0, running), 2)

        status         = "complete" if outstanding <= 0.005 else "active"
        completed_date = repayments[-1]["date"] if (status == "complete" and repayments) else None

        return {
            "repayments":     repayments,
            "total_repaid":   total_repaid,
            "outstanding":    outstanding,
            "pct":            pct,
            "status":         status,
            "completed_date": completed_date,
        }

    # ── Legacy keyword-filter path ────────────────────────────────────────────
    category = (loan.get("category_filter") or "").strip()
    keyword  = (loan.get("description_filter") or "").strip().lower()
    start    = (loan.get("start_date") or "1900-01-01")

    if not category:
        return {
            "repayments": [], "total_repaid": 0.0,
            "outstanding": principal, "pct": 0.0,
            "status": "active", "completed_date": None,
        }

    rows = conn.execute(
        "SELECT date, description, amount, account FROM transactions "
        "WHERE category = ? AND date >= ? ORDER BY date ASC",
        (category, start),
    ).fetchall()

    repayments = []
    for r in rows:
        if keyword and keyword not in (r["description"] or "").lower():
            continue
        repayments.append({
            "date":        r["date"],
            "description": r["description"],
            "amount":      round(abs(float(r["amount"])), 2),
            "account":     r["account"] or "",
        })

    total_repaid = round(sum(r["amount"] for r in repayments), 2)
    outstanding  = round(max(0.0, principal - total_repaid), 2)
    pct          = round(min(100.0, (total_repaid / principal * 100) if principal > 0 else 0.0), 1)

    running = principal
    for r in repayments:
        running     -= r["amount"]
        r["balance"] = round(max(0.0, running), 2)

    status         = "complete" if outstanding <= 0.005 else "active"
    completed_date = repayments[-1]["date"] if (status == "complete" and repayments) else None

    return {
        "repayments":     repayments,
        "total_repaid":   total_repaid,
        "outstanding":    outstanding,
        "pct":            pct,
        "status":         status,
        "completed_date": completed_date,
    }


def payoff_months(outstanding: float, monthly_payment: float, interest_rate_pct: float = 0.0) -> int:
    """Return number of months to pay off a loan balance.

    Returns 0 if already paid off, -1 if payment can't cover interest.
    """
    import math
    if outstanding <= 0:
        return 0
    if monthly_payment <= 0:
        return -1
    r = interest_rate_pct / 100 / 12
    if r == 0:
        return math.ceil(outstanding / monthly_payment)
    if monthly_payment <= outstanding * r:
        return -1  # payment doesn't cover monthly interest
    return math.ceil(-math.log(1 - r * outstanding / monthly_payment) / math.log(1 + r))


def payoff_schedule(outstanding: float, monthly_payment: float, interest_rate_pct: float = 0.0) -> list[dict]:
    """Return month-by-month amortisation schedule until paid off (max 600 months)."""
    r = interest_rate_pct / 100 / 12
    balance = outstanding
    schedule = []
    for _ in range(600):
        if balance <= 0:
            break
        interest = round(balance * r, 2)
        principal_paid = round(min(monthly_payment - interest, balance), 2)
        if principal_paid <= 0:
            break
        balance = round(max(0.0, balance - principal_paid), 2)
        schedule.append({"interest": interest, "principal": principal_paid, "balance": balance})
    return schedule


def find_unlinked_loan_transactions(conn, loans: list) -> list:
    """Return recent loan-category transactions not covered by any loan's filters.

    A loan with a description_filter keyword covers matching transactions in BOTH
    loan categories (received + repayment), so disbursement-side entries are
    automatically linked once the loan record is set up.
    """
    rows = conn.execute(
        "SELECT txn_id, date, description, amount, category, account FROM transactions "
        "WHERE category IN ('Family Loan Received','Family Loan Repayment') "
        "ORDER BY date DESC LIMIT 50"
    ).fetchall()

    # Collect keywords that cover transactions in either loan category.
    # description_filter matches repayment-side transactions (the tracked side).
    # receipt_filter matches the disbursement/receipt-side transactions.
    # Both cover transactions in either loan category so that once a loan is set
    # up with both keywords, all related transactions are automatically linked.
    keywords: list[str] = []
    no_keyword_cats: list[str] = []
    for l in loans:
        kw = (l.get("description_filter") or "").strip().lower()
        rw = (l.get("receipt_filter")     or "").strip().lower()
        cat = (l.get("category_filter")   or "")
        if kw:
            keywords.append(kw)
        if rw:
            keywords.append(rw)
        if not kw and not rw and cat:
            no_keyword_cats.append(cat)

    result = []
    for r in rows:
        desc = (r["description"] or "").lower()
        matched = (
            any(kw in desc for kw in keywords)
            or r["category"] in no_keyword_cats
        )
        if not matched:
            result.append(dict(r))

    return result


def auto_link_transfer_pair(
    txn_a_id: str,
    txn_a_desc: str,
    txn_b_id: str,
    txn_b_desc: str,
    config: dict,
) -> bool:
    """Find a unique matching loan and attach the transfer txn_ids to it.

    Matches loans by checking whether the loan's description_filter or receipt_filter
    keyword appears in either transaction's description.  If exactly one loan matches,
    txn_a_id is appended to linked_repayment_txn_ids and txn_b_id to
    linked_receipt_txn_ids (duplicates are silently skipped).

    Returns True if a unique match was found and saved, False otherwise — callers
    should set loan_link_needed=True when False is returned.
    """
    data  = load_loans(config)
    loans = data.get("loans", [])
    if not loans:
        return False

    desc_a = (txn_a_desc or "").lower()
    desc_b = (txn_b_desc or "").lower()

    def _matches(ln: dict) -> bool:
        kw = (ln.get("description_filter") or "").strip().lower()
        rw = (ln.get("receipt_filter")     or "").strip().lower()
        return (
            (kw and (kw in desc_a or kw in desc_b)) or
            (rw and (rw in desc_a or rw in desc_b))
        )

    matched = [ln for ln in loans if _matches(ln)]
    if len(matched) != 1:
        return False

    loan    = matched[0]
    rep_ids = list(loan.get("linked_repayment_txn_ids") or [])
    rec_ids = list(loan.get("linked_receipt_txn_ids")   or [])

    if txn_a_id not in rep_ids:
        rep_ids.append(txn_a_id)
    if txn_b_id not in rec_ids:
        rec_ids.append(txn_b_id)

    loan["linked_repayment_txn_ids"] = rep_ids
    loan["linked_receipt_txn_ids"]   = rec_ids
    save_loans(data, config)
    return True


def get_loan_candidates(contact_name: str, config: dict, conn) -> dict:
    """Return candidate transactions for a named loan contact.

    Searches the full transaction history for descriptions matching the contact's
    configured in_keywords (receipts — money received from them) and out_keywords
    (repayments — money paid to them).  Results are ordered most-recent first.

    If the contact isn't in family_loans.contacts, falls back to all
    Family Loan Received / Family Loan Repayment transactions so the user
    can still link transactions for unlisted contacts.

    Returns {"receipts": [...], "repayments": [...]} where each entry is a
    dict of {txn_id, date, description, amount, category, account}.
    """
    contacts = config.get("family_loans", {}).get("contacts", [])
    contact  = next((c for c in contacts if c.get("name") == contact_name), None)

    if not contact:
        rows = conn.execute(
            "SELECT txn_id, date, description, amount, category, account "
            "FROM transactions "
            "WHERE category IN ('Family Loan Received','Family Loan Repayment') "
            "ORDER BY date DESC LIMIT 200"
        ).fetchall()
        receipts   = [dict(r) for r in rows if float(r["amount"]) > 0]
        repayments = [dict(r) for r in rows if float(r["amount"]) < 0]
        return {"receipts": receipts, "repayments": repayments}

    in_kws  = [k.upper() for k in contact.get("in_keywords",  [])]
    out_kws = [k.upper() for k in contact.get("out_keywords", [])]
    acct    = contact.get("account", "")

    if acct:
        rows = conn.execute(
            "SELECT txn_id, date, description, amount, category, account "
            "FROM transactions WHERE account = ? ORDER BY date DESC",
            (acct,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT txn_id, date, description, amount, category, account "
            "FROM transactions ORDER BY date DESC LIMIT 1000"
        ).fetchall()

    receipts, repayments = [], []
    for r in rows:
        desc_up = (r["description"] or "").upper()
        amt     = float(r["amount"])
        if amt > 0 and in_kws  and any(k in desc_up for k in in_kws):
            receipts.append(dict(r))
        elif amt < 0 and out_kws and any(k in desc_up for k in out_kws):
            repayments.append(dict(r))

    return {"receipts": receipts, "repayments": repayments}
