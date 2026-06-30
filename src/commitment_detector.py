"""
Recurring commitment detector and forward-expense planner.

Scans transaction history for committed recurring expenses (rent, utilities,
insurance, subscriptions, health) and maintains a forward-looking plan in
data/commitments.json.  Manual items (mortgage, credit card payment, etc.)
can be added via the /commitments web UI.
"""

import calendar
import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

import pandas as pd


_COMMITMENTS_DEFAULT = "data/commitments.json"

# Categories that represent committed / non-discretionary spending
_COMMITTED_CATS = {"Housing", "Utilities", "Insurance", "Subscriptions", "Health"}

FREQUENCIES = {
    "weekly":      7,
    "fortnightly": 14,
    "monthly":     30,
    "quarterly":   91,
    "annual":      365,
}

FREQUENCY_LABELS = {
    "weekly":      "Weekly",
    "fortnightly": "Fortnightly",
    "monthly":     "Monthly",
    "quarterly":   "Quarterly",
    "annual":      "Annual",
}


def _stable_id(key: str) -> str:
    return hashlib.md5(key.upper().encode()).hexdigest()[:12]


def _classify_frequency(avg_gap: float) -> str | None:
    if 5 <= avg_gap <= 10:
        return "weekly"
    if 11 <= avg_gap <= 21:
        return "fortnightly"
    if 22 <= avg_gap <= 46:
        return "monthly"
    if 75 <= avg_gap <= 110:
        return "quarterly"
    if 330 <= avg_gap <= 400:
        return "annual"
    return None


def _add_months(d: date, n: int) -> date:
    """Add n months to d, clamping to the last day of the target month."""
    month = d.month - 1 + n
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _next_due(last_seen: date, frequency: str) -> date:
    """Return the next projected due date on or after tomorrow."""
    today    = date.today()
    tomorrow = today + timedelta(days=1)

    if frequency == "monthly":
        d = last_seen
        while d < tomorrow:
            d = _add_months(d, 1)
        return d

    if frequency == "quarterly":
        d = last_seen
        while d < tomorrow:
            d = _add_months(d, 3)
        return d

    if frequency == "annual":
        d = last_seen
        while d < tomorrow:
            d = _add_months(d, 12)
        return d

    # weekly / fortnightly — simple day arithmetic
    freq_days = FREQUENCIES[frequency]
    d = last_seen
    while d < tomorrow:
        d += timedelta(days=freq_days)
    return d


def detect_recurring_commitments(df: pd.DataFrame) -> list[dict]:
    """
    Scan df for recurring expenses in committed categories.
    Returns a list of commitment dicts with source='detected'.
    """
    if df.empty:
        return []

    spend = df[
        (df["amount"] < 0) &
        df["category"].isin(_COMMITTED_CATS)
    ].copy()

    if spend.empty:
        return []

    # Normalise description for grouping
    spend["_key"] = spend["description"].str.upper().str.strip()

    detected: list[dict] = []

    for key, grp in spend.groupby("_key"):
        # Require appearances in at least 2 distinct calendar months
        months = grp["date"].dt.to_period("M").nunique()
        if months < 2:
            continue

        grp = grp.sort_values("date")
        dates = list(grp["date"])

        if len(dates) < 2:
            continue

        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_gap = sum(gaps) / len(gaps)

        freq = _classify_frequency(avg_gap)
        if freq is None:
            continue

        last_date  = grp["date"].max().date()
        amounts    = grp["amount"].abs()
        last_amt   = round(float(amounts.iloc[-1]), 2)
        avg_amt    = round(float(amounts.mean()), 2)
        category   = grp["category"].mode().iloc[0]
        account    = grp["account"].mode().iloc[0] if "account" in grp.columns else ""

        detected.append({
            "id":           _stable_id(str(key)),
            "name":         str(key).title()[:60],
            "category":     category,
            "merchant_key": str(key),
            "amount":       last_amt,
            "avg_amount":   avg_amt,
            "frequency":    freq,
            "account":      account,
            "active":       True,
            "source":       "detected",
            "last_seen":    last_date.isoformat(),
            "next_due":     _next_due(last_date, freq).isoformat(),
            "notes":        "",
        })

    return detected


def get_upcoming(commitments: dict, days_ahead: int = 90) -> list[dict]:
    """
    Project all active commitments over the next days_ahead days.
    Returns a flat list sorted by projected_date.
    """
    today   = date.today()
    horizon = today + timedelta(days=days_ahead)
    upcoming: list[dict] = []

    for item in commitments.get("items", []):
        if not item.get("active", True):
            continue
        freq = item.get("frequency", "monthly")

        try:
            seed = date.fromisoformat(item.get("next_due") or item.get("last_seen", ""))
        except (ValueError, TypeError):
            continue

        # Advance seed to first occurrence on/after today
        d = _next_due(seed - timedelta(days=1), freq)

        while d <= horizon:
            upcoming.append({**item, "projected_date": d.isoformat()})
            # Advance to next occurrence
            if freq == "monthly":
                d = _add_months(d, 1)
            elif freq == "quarterly":
                d = _add_months(d, 3)
            elif freq == "annual":
                d = _add_months(d, 12)
            else:
                d += timedelta(days=FREQUENCIES[freq])

    upcoming.sort(key=lambda x: x["projected_date"])
    return upcoming


MONTHLY_FACTORS: dict[str, float] = {
    "weekly":      52 / 12,
    "fortnightly": 26 / 12,
    "monthly":     1.0,
    "quarterly":   1 / 3,
    "annual":      1 / 12,
}


def monthly_committed_total(commitments: dict) -> float:
    """Approximate monthly cost of all active commitments."""
    total = 0.0
    for item in commitments.get("items", []):
        if not item.get("active", True):
            continue
        factor = MONTHLY_FACTORS.get(item.get("frequency", "monthly"), 1.0)
        total += float(item.get("amount", 0)) * factor
    return round(total, 2)


def load_commitments(config: dict) -> dict:
    path = Path(config.get("data", {}).get("commitments_file", _COMMITMENTS_DEFAULT))
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def save_commitments(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("commitments_file", _COMMITMENTS_DEFAULT))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def merge_commitments(existing: dict, detected: list[dict]) -> dict:
    """Append newly detected items that aren't already tracked (matched by id)."""
    existing_ids = {i["id"] for i in existing.get("items", [])}
    merged = list(existing.get("items", []))
    added = 0
    for item in detected:
        if item["id"] not in existing_ids:
            merged.append(item)
            added += 1
    if added:
        logger.info(f"  Commitments: {added} new recurring item(s) detected")
    return {"items": merged}
