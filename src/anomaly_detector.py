"""Anomaly detection — flag transactions that are unusually large for their category.

Algorithm:
  For each category, compute the median and IQR of absolute spend amounts over
  the last 12 months. A transaction is flagged as anomalous when:
    |amount| > median + MULTIPLIER * IQR
  with a minimum floor of FLOOR_FACTOR × median (so low-variance categories
  aren't over-triggered).

  Income, pass-through categories, and transfers are excluded.
  Single-transaction categories are never flagged (no baseline).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# How many IQR units above the median counts as anomalous
IQR_MULTIPLIER = 2.5
# Minimum ratio above median to flag (avoids noise in low-IQR categories)
FLOOR_RATIO = 3.0

_EXCLUDE = frozenset({
    "Income", "Board & Lodging", "Interest Income", "Business Reimbursement",
    "Family Loan Received", "Transfers", "Investment",
})


def detect_anomalies(conn, lookback_months: int = 12) -> list[str]:
    """Flag anomalous debit transactions in the DB.

    Computes per-category baselines from the last `lookback_months` months
    (excluding the current month so partial month data doesn't skew the baseline),
    then marks transactions whose absolute amount exceeds the threshold as anomalous.

    Returns list of txn_ids that were newly flagged.
    """
    import sqlite3

    today = date.today()
    baseline_end = (today.replace(day=1) - timedelta(days=1)).isoformat()  # last day of prev month
    baseline_start_dt = today.replace(day=1) - timedelta(days=lookback_months * 30)
    baseline_start = baseline_start_dt.replace(day=1).isoformat()

    # Build per-category baseline from historical data
    rows = conn.execute(
        "SELECT category, ABS(amount) as amt FROM transactions "
        "WHERE amount < 0 AND date >= ? AND date <= ? AND category NOT IN ({}) "
        "AND (is_split_parent = 0 OR is_split_parent IS NULL)".format(
            ",".join("?" * len(_EXCLUDE))
        ),
        [baseline_start, baseline_end, *list(_EXCLUDE)],
    ).fetchall()

    from collections import defaultdict
    by_cat: dict[str, list[float]] = defaultdict(list)
    for cat, amt in rows:
        if cat:
            by_cat[cat].append(amt)

    thresholds: dict[str, float] = {}
    for cat, amounts in by_cat.items():
        if len(amounts) < 3:  # not enough history to establish a baseline
            continue
        sorted_a = sorted(amounts)
        n = len(sorted_a)
        q1 = sorted_a[n // 4]
        q3 = sorted_a[(3 * n) // 4]
        median = sorted_a[n // 2]
        iqr = q3 - q1
        iqr_threshold = median + IQR_MULTIPLIER * iqr
        floor_threshold = median * FLOOR_RATIO
        thresholds[cat] = max(iqr_threshold, floor_threshold)

    if not thresholds:
        return []

    # Find all current debit transactions in those categories
    current_rows = conn.execute(
        "SELECT txn_id, category, ABS(amount) as amt, is_anomaly FROM transactions "
        "WHERE amount < 0 AND category IS NOT NULL "
        "AND (is_split_parent = 0 OR is_split_parent IS NULL)"
    ).fetchall()

    newly_flagged: list[str] = []
    to_flag: list[str] = []
    to_clear: list[str] = []

    for txn_id, cat, amt, currently_anomaly in current_rows:
        threshold = thresholds.get(cat)
        if threshold is None:
            continue
        should_flag = amt > threshold
        if should_flag and not currently_anomaly:
            to_flag.append(txn_id)
            newly_flagged.append(txn_id)
        elif not should_flag and currently_anomaly:
            to_clear.append(txn_id)

    if to_flag:
        conn.executemany(
            "UPDATE transactions SET is_anomaly = 1 WHERE txn_id = ?",
            [(t,) for t in to_flag],
        )
    if to_clear:
        conn.executemany(
            "UPDATE transactions SET is_anomaly = 0 WHERE txn_id = ?",
            [(t,) for t in to_clear],
        )
    if to_flag or to_clear:
        conn.commit()

    logger.info(f"Anomaly detection: {len(to_flag)} flagged, {len(to_clear)} cleared")
    return newly_flagged


def anomaly_summary(conn) -> list[dict]:
    """Return recently anomalous transactions for dashboard display."""
    rows = conn.execute(
        "SELECT txn_id, date, description, amount, category, account "
        "FROM transactions "
        "WHERE is_anomaly = 1 AND amount < 0 "
        "ORDER BY date DESC LIMIT 20"
    ).fetchall()
    return [
        {
            "txn_id":      r[0],
            "date":        r[1],
            "description": r[2],
            "amount":      abs(r[3]),
            "category":    r[4],
            "account":     r[5],
        }
        for r in rows
    ]
