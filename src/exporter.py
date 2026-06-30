"""
Exports filtered transactions from the database.

Usage (standalone — does not need Raw Data):
    python finance_analyser.py --export out.csv
    python finance_analyser.py --export out.csv --category "Dining Out"
    python finance_analyser.py --export out.csv --from-date 2025-01 --to-date 2025-12
    python finance_analyser.py --export        # prints to stdout
"""

import logging
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def export_transactions(
    config: dict,
    output: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> None:
    from src.db import load_transactions
    df = load_transactions(config)
    if df.empty:
        logger.error("ERROR: no transactions found. Run a full analysis first.")
        sys.exit(1)

    total = len(df)

    all_categories = sorted(df["category"].dropna().unique()) if category else []

    if category:
        df = df[df["category"].str.lower() == category.lower()]
        if df.empty:
            logger.info(f"No transactions found for category '{category}'.")
            logger.info(f"Available categories: {', '.join(all_categories)}")
            sys.exit(0)

    if from_date:
        df = df[df["date"].dt.to_period("M").astype(str) >= from_date]

    if to_date:
        df = df[df["date"].dt.to_period("M").astype(str) <= to_date]

    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    filters = []
    if category:
        filters.append(f"category={category}")
    if from_date:
        filters.append(f"from={from_date}")
    if to_date:
        filters.append(f"to={to_date}")
    filter_desc = ", ".join(filters) if filters else "all"

    logger.info(f"  Exporting {len(df)} of {total} transactions ({filter_desc})")

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_csv(out_path, index=False, encoding="utf-8")
            logger.info(f"  Written to {out_path}")
        except OSError as exc:
            logger.error(f"  ERROR: could not write {out_path}: {exc}")
            sys.exit(1)
    else:
        print(df.to_csv(index=False))
