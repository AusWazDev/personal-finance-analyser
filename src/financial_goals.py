"""Financial goals — savings targets with progress tracking."""
import json
from datetime import date, timedelta
from pathlib import Path

FREQUENCIES = {"weekly", "fortnightly", "monthly", "quarterly", "annual"}

FREQUENCY_LABELS = {
    "weekly":      "Weekly",
    "fortnightly": "Fortnightly",
    "monthly":     "Monthly",
    "quarterly":   "Quarterly",
    "annual":      "Annual",
}

GOAL_CATEGORIES = [
    "Savings", "Emergency Fund", "Holiday", "Investment", "Vehicle",
    "Home", "Education", "Retirement", "Debt Repayment", "Other",
]

_MONTHLY_FACTOR = {
    "weekly":      52 / 12,
    "fortnightly": 26 / 12,
    "monthly":     1.0,
    "quarterly":   1 / 3,
    "annual":      1 / 12,
}


def load_goals(config: dict) -> dict:
    """Load goals from JSON; returns {"items": []} if absent."""
    path = Path(config.get("data", {}).get(
        "financial_goals_file", "Data/financial_goals.json"
    ))
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {"items": []}


def save_goals(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get(
        "financial_goals_file", "Data/financial_goals.json"
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), "utf-8")


def monthly_savings_total(goals: dict) -> float:
    """Sum of monthly-equivalent contributions across all active goals."""
    total = 0.0
    for item in goals.get("items", []):
        if not item.get("active", True):
            continue
        contrib = float(item.get("contribution_amount", 0) or 0)
        freq = item.get("frequency", "monthly")
        total += contrib * _MONTHLY_FACTOR.get(freq, 1.0)
    return round(total, 2)


def get_upcoming_milestones(goals: dict, days: int = 90) -> list[dict]:
    """Return active goals enriched with progress info, sorted by proximity to completion."""
    today = date.today()
    results = []
    for item in goals.get("items", []):
        if not item.get("active", True):
            continue
        target  = float(item.get("target_amount", 0) or 0)
        current = float(item.get("current_amount", 0) or 0)
        contrib = float(item.get("contribution_amount", 0) or 0)
        freq    = item.get("frequency", "monthly")
        monthly = contrib * _MONTHLY_FACTOR.get(freq, 1.0)

        remaining = max(0.0, target - current)
        pct = round(min(100.0, (current / target * 100) if target > 0 else 0.0), 1)

        estimated_date: date | None = None
        target_date_str = (item.get("target_date") or "").strip()
        if target_date_str:
            try:
                estimated_date = date.fromisoformat(target_date_str)
            except ValueError:
                pass

        if estimated_date is None and monthly > 0 and remaining > 0:
            months_needed = remaining / monthly
            estimated_date = today + timedelta(days=int(months_needed * 30.44))

        days_to_go = (estimated_date - today).days if estimated_date else None
        within_window = estimated_date is not None and estimated_date <= (today + timedelta(days=days))

        results.append({
            **item,
            "pct":            pct,
            "remaining":      round(remaining, 2),
            "monthly_equiv":  round(monthly, 2),
            "estimated_date": estimated_date.isoformat() if estimated_date else None,
            "days_to_go":     days_to_go,
            "within_window":  within_window,
            "freq_label":     FREQUENCY_LABELS.get(freq, freq.title()),
        })

    return sorted(results, key=lambda x: (x["days_to_go"] if x["days_to_go"] is not None else 999999))


def calculate_goal_balance(goal: dict, conn) -> float | None:
    """Return sum of credits to the linked account since goal creation, or None if no account set.

    Used to auto-populate 'Current amount' for goals tied to a specific savings account.
    """
    account = (goal.get("account") or "").strip()
    if not account:
        return None
    start = goal.get("created_date") or "1900-01-01"
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0.0) FROM transactions "
        "WHERE account = ? AND amount > 0 AND date >= ?",
        (account, start),
    ).fetchone()
    return round(float(row[0]), 2) if row else 0.0
