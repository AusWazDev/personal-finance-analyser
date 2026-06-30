"""
Rule-based financial insight engine.

Computes triggered alerts from recent transaction data without requiring
the Claude API. Rules fire against the current calendar month compared to
the prior 3 months.

Each insight dict:
    type       : str  — spend_spike | new_merchant | large_txn | weekend_pattern
    title      : str  — short headline
    body       : str  — one-sentence detail
    severity   : str  — warning | info
    action_url : str | None  — optional drill-down link
"""

import sqlite3
from datetime import date, timedelta

from src.utils import EXCLUDE_FROM_SPEND as _EXCLUDE


def _ym(months_back: int = 0) -> str:
    today = date.today()
    m, y = today.month - months_back, today.year
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def _spend_spikes(conn: sqlite3.Connection, ym: str) -> list[dict]:
    """Categories where current-month spend is >30% above 3-month average."""
    prev_months = [_ym(i) for i in range(1, 4)]
    placeholders = ",".join("?" * len(prev_months))
    avg_rows = conn.execute(
        f"""
        SELECT category, SUM(ABS(amount)) / 3.0 AS avg_spend
        FROM transactions
        WHERE substr(date,1,7) IN ({placeholders})
          AND amount < 0
          AND COALESCE(is_split_parent, 0) = 0
        GROUP BY category
        """,
        prev_months,
    ).fetchall()
    avg_by_cat = {r[0]: r[1] for r in avg_rows if r[0] not in _EXCLUDE}

    cur_rows = conn.execute(
        """
        SELECT category, SUM(ABS(amount)) AS spend
        FROM transactions
        WHERE substr(date,1,7) = ?
          AND amount < 0
          AND COALESCE(is_split_parent, 0) = 0
        GROUP BY category
        """,
        (ym,),
    ).fetchall()

    spikes = []
    for cat, spend in cur_rows:
        if cat in _EXCLUDE:
            continue
        avg = avg_by_cat.get(cat, 0)
        if avg > 0 and spend > avg * 1.30 and spend >= 50:
            pct = round((spend / avg - 1) * 100)
            spikes.append({
                "type":       "spend_spike",
                "title":      f"{cat} up {pct}% vs average",
                "body":       f"${spend:.0f} this month vs ${avg:.0f} avg — {pct}% above usual.",
                "severity":   "warning" if pct >= 50 else "info",
                "action_url": f"/transactions?cat={cat}",
            })
    spikes.sort(key=lambda x: -int(x["title"].split("up ")[1].split("%")[0]))
    return spikes[:3]


def _new_merchants(conn: sqlite3.Connection, ym: str) -> list[dict]:
    """Merchants (by description) appearing for the first time this month."""
    prior = set(
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT UPPER(TRIM(description))
            FROM transactions
            WHERE substr(date,1,7) < ? AND amount < 0
            """,
            (ym,),
        ).fetchall()
    )
    cur = conn.execute(
        """
        SELECT UPPER(TRIM(description)) AS dk, description, category
        FROM transactions
        WHERE substr(date,1,7) = ? AND amount < 0
          AND COALESCE(is_split_parent, 0) = 0
        GROUP BY dk
        ORDER BY MIN(date)
        """,
        (ym,),
    ).fetchall()

    new = [(dk, desc, cat) for dk, desc, cat in cur if dk not in prior and cat not in _EXCLUDE][:3]
    return [
        {
            "type":       "new_merchant",
            "title":      f"New merchant: {desc[:45]}",
            "body":       "First transaction from this merchant in your history.",
            "severity":   "info",
            "action_url": f"/transactions?cat={cat}",
        }
        for _, desc, cat in new
    ]


def _large_transaction(conn: sqlite3.Connection, ym: str) -> list[dict]:
    """Single largest non-excluded expense this month (informational, only if ≥$100)."""
    rows = conn.execute(
        """
        SELECT description, ABS(amount) AS amt, date, category
        FROM transactions
        WHERE substr(date,1,7) = ? AND amount < 0
          AND COALESCE(is_split_parent, 0) = 0
        ORDER BY ABS(amount) DESC
        """,
        (ym,),
    ).fetchall()
    for desc, amt, dt, cat in rows:
        if cat in _EXCLUDE or amt < 100:
            continue
        return [
            {
                "type":       "large_txn",
                "title":      f"Largest transaction: ${amt:.0f}",
                "body":       f"{desc[:50]} on {dt} ({cat}).",
                "severity":   "info",
                "action_url": f"/transactions?cat={cat}",
            }
        ]
    return []


def _weekend_pattern(conn: sqlite3.Connection, ym: str) -> list[dict]:
    """Flag if average daily weekend spend is >2× weekday spend this month."""
    rows = conn.execute(
        """
        SELECT date, SUM(ABS(amount)) AS daily
        FROM transactions
        WHERE substr(date,1,7) = ? AND amount < 0
          AND COALESCE(is_split_parent, 0) = 0
        GROUP BY date
        """,
        (ym,),
    ).fetchall()
    if not rows:
        return []

    we_total, we_days, wd_total, wd_days = 0.0, 0, 0.0, 0
    for dt, daily in rows:
        d = date.fromisoformat(dt)
        if d.weekday() >= 5:
            we_total += daily
            we_days += 1
        else:
            wd_total += daily
            wd_days += 1

    if wd_days == 0 or we_days == 0:
        return []
    avg_we = we_total / we_days
    avg_wd = wd_total / wd_days
    if avg_wd > 0 and avg_we > avg_wd * 2 and we_total >= 100:
        ratio = round(avg_we / avg_wd, 1)
        return [
            {
                "type":       "weekend_pattern",
                "title":      f"Weekend spend {ratio}× higher than weekdays",
                "body":       f"Avg ${avg_we:.0f}/day on weekends vs ${avg_wd:.0f}/day on weekdays this month.",
                "severity":   "info",
                "action_url": None,
            }
        ]
    return []


def compute_insights(conn: sqlite3.Connection, config: dict) -> list[dict]:
    """Return all triggered insight alerts, capped at 10."""
    ym = _ym(0)
    results: list[dict] = []
    for fn in (_spend_spikes, _new_merchants, _large_transaction, _weekend_pattern):
        try:
            results += fn(conn, ym)
        except Exception:
            pass
    return results[:10]
