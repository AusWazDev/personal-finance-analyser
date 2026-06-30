"""Budget management — monthly spending limits, persistence, and suggestions."""
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_DEFAULT_FILE = "Data/budgets.json"

# Categories that don't make sense to budget (pass-through / not discretionary spend)
_SKIP_SUGGEST = {"Transfers", "Investment", "Family Loan Repayment", "Family Loan Received"}


def load_budgets(config: dict) -> dict:
    """Return {category: monthly_limit} dict.

    Reads Data/budgets.json. On first call when the file is absent, auto-migrates
    any budgets: entries from config.yaml and saves them to the JSON file so future
    reads don't need config.yaml at all.
    """
    path = Path(config.get("data", {}).get("budgets_file", _DEFAULT_FILE))
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            return {k: float(v) for k, v in data.get("budgets", {}).items() if v}
        except Exception:
            pass

    # Auto-migrate from config.yaml on first use
    legacy = {k: float(v) for k, v in config.get("budgets", {}).items() if v}
    if legacy:
        save_budgets(legacy, config)
    return legacy


def save_budgets(budgets: dict, config: dict, rollover: dict | None = None) -> None:
    """Write {category: monthly_limit} to Data/budgets.json.

    Optionally also saves rollover: {category: bool} alongside the budget limits.
    """
    path = Path(config.get("data", {}).get("budgets_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve existing file contents (so rollover key isn't lost on a limits-only save)
    try:
        existing = json.loads(path.read_text("utf-8")) if path.exists() else {}
    except Exception:
        existing = {}
    cleaned = {k: round(float(v), 2) for k, v in budgets.items() if v and float(v) > 0}
    existing["budgets"] = cleaned
    existing["updated_at"] = date.today().isoformat()
    if rollover is not None:
        existing["rollover"] = {k: bool(v) for k, v in rollover.items() if v}
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), "utf-8")


def load_rollover_settings(config: dict) -> dict:
    """Return {category: bool} rollover-enabled flags from Data/budgets.json."""
    path = Path(config.get("data", {}).get("budgets_file", _DEFAULT_FILE))
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            return {k: bool(v) for k, v in data.get("rollover", {}).items()}
        except Exception:
            pass
    return {}


def load_period_settings(config: dict) -> dict:
    """Return {category: "monthly"|"fortnightly"} period settings from Data/budgets.json.

    Defaults to "monthly" when absent. A "fortnightly" budget means the limit applies
    per 14-day window rather than per calendar month.
    """
    path = Path(config.get("data", {}).get("budgets_file", _DEFAULT_FILE))
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            return {k: v for k, v in data.get("periods", {}).items()
                    if v in ("monthly", "fortnightly")}
        except Exception:
            pass
    return {}


def save_period_settings(periods: dict, config: dict) -> None:
    """Write {category: "monthly"|"fortnightly"} to Data/budgets.json."""
    path = Path(config.get("data", {}).get("budgets_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text("utf-8")) if path.exists() else {}
    except Exception:
        existing = {}
    existing["periods"] = {k: v for k, v in periods.items() if v == "fortnightly"}
    existing["updated_at"] = date.today().isoformat()
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), "utf-8")


def current_fortnight_window(ref: date | None = None) -> tuple[date, date]:
    """Return (start, end) dates of the current ISO-aligned 14-day window.

    Fortnights are anchored to 2024-01-01 (a Monday) and repeat every 14 days.
    """
    anchor = date(2024, 1, 1)
    today = ref or date.today()
    days_since = (today - anchor).days
    period_number = days_since // 14
    start = anchor + timedelta(days=period_number * 14)
    end   = start + timedelta(days=13)
    return start, end


def get_effective_budget(conn, category: str, month_str: str, config: dict) -> dict:
    """Compute effective monthly budget including rollover from the prior month.

    Returns {"base": float, "rollover_amount": float, "effective": float}.
    Rollover carries forward unspent balance from the previous month,
    capped at 1× the base limit (so at most double the base budget).
    """
    budgets = load_budgets(config)
    base = float(budgets.get(category, 0))
    if not base or not load_rollover_settings(config).get(category):
        return {"base": base, "rollover_amount": 0.0, "effective": base}

    year, month = int(month_str[:4]), int(month_str[5:7])
    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    prev_prefix = f"{prev_year:04d}-{prev_month:02d}"

    try:
        row = conn.execute(
            "SELECT SUM(ABS(amount)) FROM transactions "
            "WHERE date LIKE ? AND amount < 0 AND category = ?",
            (f"{prev_prefix}%", category),
        ).fetchone()
        prev_spend = float(row[0] or 0)
    except Exception:
        prev_spend = 0.0

    unspent = max(0.0, base - prev_spend)
    rollover_amount = round(min(unspent, base), 2)
    return {"base": base, "rollover_amount": rollover_amount, "effective": round(base + rollover_amount, 2)}


def suggest_budgets(conn, months: int = 3) -> dict:
    """Return suggested monthly limits based on historical average spend.

    Returns {category: {"avg": float, "months_with_data": int, "suggested": float}}
    where suggested = avg rounded up to the nearest $25 (minimum $25).
    Only includes expenditure categories; skips pass-through categories.
    """
    today = date.today()
    # Go back exactly `months` calendar months from the start of the current month
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    since_str = f"{y:04d}-{m:02d}-01"

    rows = conn.execute(
        "SELECT strftime('%Y-%m', date) AS month, category, SUM(ABS(amount)) AS total "
        "FROM transactions "
        "WHERE amount < 0 AND date >= ? "
        "GROUP BY month, category "
        "ORDER BY category, month",
        (since_str,),
    ).fetchall()

    cat_months: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        cat = row["category"] or "Miscellaneous"
        if cat not in _SKIP_SUGGEST:
            cat_months[cat].append(float(row["total"]))

    def _round25(v: float) -> float:
        return max(25.0, float(math.ceil(v / 25) * 25))

    return {
        cat: {
            "avg":             round(sum(totals) / len(totals), 2),
            "months_with_data": len(totals),
            "suggested":       _round25(sum(totals) / len(totals)),
        }
        for cat, totals in sorted(cat_months.items())
    }
