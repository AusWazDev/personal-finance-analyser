#!/usr/bin/env python3
"""
Personal Finance Analyser
=========================
Usage:
    python finance_analyser.py                    # full analysis
    python finance_analyser.py --month 2026-04    # single month only
    python finance_analyser.py --no-categorise    # skip API, use cache
    python finance_analyser.py --no-recommend     # skip recommendations
    python finance_analyser.py --no-archive       # skip archiving Raw Data
    python finance_analyser.py --config my.yaml   # alternate config file
"""

import argparse
import locale
import logging
import os
import sys
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore not installed — SSL uses certifi bundle

locale.setlocale(locale.LC_TIME, "")

import yaml

logger = logging.getLogger("finance_analyser")


def _check_dependencies() -> bool:
    missing = []
    for pkg in ("pandas", "bs4", "plotly", "anthropic", "yaml"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg if pkg != "yaml" else "pyyaml")
    if missing:
        print("Missing packages. Run:")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Personal Finance Analyser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--month", metavar="YYYY-MM",
                        help="Filter to a specific month (e.g. 2026-04)")
    parser.add_argument("--fy", metavar="YYYY",
                        help="Filter to an Australian financial year (e.g. 2026 = 1 Jul 2025–30 Jun 2026)")
    parser.add_argument("--no-categorise", action="store_true",
                        help="Skip Claude API calls; use only config overrides and cache")
    parser.add_argument("--no-recommend", action="store_true",
                        help="Skip generating recommendations.md")
    parser.add_argument("--no-archive", action="store_true",
                        help="Skip archiving Raw Data files after analysis")
    parser.add_argument("--apply-review", metavar="REVIEW_JSON",
                        help="Apply category overrides from a review JSON file "
                             "(downloaded from reports/review.html)")
    parser.add_argument("--export", metavar="OUTPUT_CSV", nargs="?", const="",
                        help="Export transactions from database to a CSV file "
                             "(omit filename to print to stdout)")
    parser.add_argument("--category", metavar="CATEGORY",
                        help="Filter by category when using --export")
    parser.add_argument("--from-date", metavar="YYYY-MM",
                        help="Start month filter for --export (e.g. 2025-01)")
    parser.add_argument("--to-date", metavar="YYYY-MM",
                        help="End month filter for --export (e.g. 2025-12)")
    parser.add_argument("--recategorise-all", action="store_true",
                        help="Re-run AI categorisation over all transactions in the database "
                             "and regenerate reports (no Raw Data import)")
    parser.add_argument("--recategorise-miscellaneous", action="store_true",
                        help="Re-run AI categorisation over only Miscellaneous transactions "
                             "and regenerate reports (faster than --recategorise-all)")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file (default: config.yaml)")
    args = parser.parse_args()

    if not _check_dependencies():
        sys.exit(1)

    if not Path(args.config).exists():
        print(f"Config file not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    from src.logging_config import setup_logging
    setup_logging(config)

    # Verify ANTHROPIC_API_KEY is available (unless skipping API)
    if not args.no_categorise:
        api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")

        # On Windows, the User env var may not be inherited if the parent process
        # was started before the variable was set — read from registry directly.
        if not api_key and sys.platform == "win32":
            import subprocess as _sp
            try:
                _r = _sp.run(
                    ["powershell", "-NoProfile", "-Command",
                     "[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')"],
                    capture_output=True, text=True, timeout=5,
                )
                api_key = _r.stdout.strip() or None
                if api_key:
                    os.environ["ANTHROPIC_API_KEY"] = api_key
            except Exception:
                pass

        if not api_key:
            logger.error(
                "ERROR: No ANTHROPIC_API_KEY found.\n"
                "  Set it as an environment variable:  ANTHROPIC_API_KEY=sk-ant-...\n"
                "  Or add it to config.yaml:           anthropic_api_key: sk-ant-...\n"
                "  Or skip the API:                    --no-categorise"
            )
            sys.exit(1)

    import pandas as pd
    from src.parsers import load_all_transactions
    from src.enricher import enrich_paypal_transactions
    from src.categoriser import categorise_transactions, categorise_payin4_merchants
    from src.reporter import generate_all_reports
    from src.recommendations import generate_recommendations
    from src.archiver import (
        plan_zip_name, create_zip_and_clear,
        archive_specific_files,
    )
    from src.db import (
        get_db, init_db, load_transactions,
        upsert_transactions, update_transaction_categories, update_transaction_descriptions,
    )

    # -- Ensure database schema exists (idempotent) ----------------------------
    _conn = get_db(config)
    init_db(_conn)
    _conn.close()

    # -- Export mode (standalone — reads master CSV, no Raw Data needed) ------
    if args.export is not None:
        from src.exporter import export_transactions
        export_transactions(
            config,
            output=args.export or None,
            category=args.category,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        sys.exit(0)

    # -- Apply review overrides (standalone mode) -----------------------------
    if args.apply_review:
        from src.review_applier import apply_review
        apply_review(args.apply_review, config)
        logger.info("\nReview overrides applied. Re-run without --apply-review to regenerate reports.")
        sys.exit(0)

    # -- Re-categorise all mode (standalone — reads master CSV, updates categories) --
    if args.recategorise_all:
        from src.parsers import parse_paypal_csv, _detect_file_type, _make_txn_id, _apply_exclusions
        from pathlib import Path as _Path

        # Check for new PayPal CSVs in Raw Data (user may have dropped gap statements here)
        input_dir = _Path(config["data"]["input_dir"])
        paypal_file_set: set = set()
        if input_dir.exists():
            for suffix in ("*.csv", "*.CSV"):
                paypal_file_set.update(input_dir.glob(suffix))
        paypal_files = [
            f for f in sorted(paypal_file_set)
            if _detect_file_type(f, config)[0] == "paypal_csv"
        ]

        if paypal_files:
            logger.info(f"\n-- Found {len(paypal_files)} PayPal CSV(s) in Raw Data "
                        "— ingesting before re-enrichment")
            new_paypal_dfs = []
            for pf in paypal_files:
                parsed = parse_paypal_csv(pf)
                if not parsed.empty:
                    new_paypal_dfs.append(parsed)
                    logger.info(f"  {pf.name}: {len(parsed)} rows")
            if new_paypal_dfs:
                new_paypal = pd.concat(new_paypal_dfs, ignore_index=True)
                new_paypal["txn_id"] = new_paypal.apply(_make_txn_id, axis=1)
                new_paypal = new_paypal.drop_duplicates(subset=["txn_id"], keep="first")
                new_paypal = _apply_exclusions(new_paypal, config)
                new_paypal = categorise_transactions(
                    new_paypal, config, use_api=not args.no_categorise
                )
                zip_name = plan_zip_name(config)
                added = upsert_transactions(new_paypal, config, zip_name)
                logger.info(f"  Added {added} new PayPal transactions to database")
                archive_specific_files(paypal_files, config, zip_name)

        logger.info("\n-- Loading transactions from database ---------------------------")
        report_df = load_transactions(config)
        if report_df.empty:
            logger.info("  No transactions found — nothing to recategorise.")
            sys.exit(0)
        logger.info(f"  Loaded {len(report_df)} transactions")

        logger.info("\n-- Re-enriching PayPal descriptions (±3 day window) -------------")
        report_df = enrich_paypal_transactions(report_df, config)
        enriched_count = report_df["description"].str.startswith("PayPal: ", na=False).sum()
        logger.info(f"  {enriched_count} total enriched PayPal rows after re-enrichment")
        update_transaction_descriptions(report_df, config)

        logger.info("\n-- Recategorising all transactions ------------------------------")
        report_df = categorise_transactions(
            report_df, config, use_api=not args.no_categorise, force_recategorise=True
        )
        uncategorised = (report_df["category"] == "Miscellaneous").sum()
        logger.info(f"  Done ({uncategorised} fell back to Miscellaneous)")

        logger.info("\n-- Saving recategorised transactions to database ----------------")
        update_transaction_categories(report_df, config)

        logger.info("\n-- Applying Pay-in-4 merchant categories -----------------------")
        from src.payin4_detector import load_payin4_groups, save_payin4_groups
        existing_groups = load_payin4_groups(config)
        if existing_groups:
            existing_groups, report_df, changed_ids = categorise_payin4_merchants(
                existing_groups, report_df, config, use_api=not args.no_categorise
            )
            save_payin4_groups(existing_groups, config)
            if changed_ids:
                changed_df = report_df[report_df["txn_id"].astype(str).isin(changed_ids)]
                update_transaction_categories(changed_df, config)

        if not args.no_recommend:
            logger.info("\n-- Generating recommendations -----------------------------------")
            generate_recommendations(report_df, config)

        logger.info("\n-- Generating reports -------------------------------------------")
        generate_all_reports(report_df, config)
        sys.exit(0)

    # -- Re-categorise Miscellaneous mode (targeted — only Misc rows) ----------
    if args.recategorise_miscellaneous:
        logger.info("\n-- Loading transactions from database ---------------------------")
        report_df = load_transactions(config)
        if report_df.empty:
            logger.info("  No transactions found.")
            sys.exit(0)
        misc_count = (report_df["category"] == "Miscellaneous").sum()
        logger.info(f"  Loaded {len(report_df)} transactions ({misc_count} Miscellaneous)")

        if misc_count == 0:
            logger.info("\n  Nothing to do — no Miscellaneous transactions in database.")
            sys.exit(0)

        logger.info("\n-- Re-enriching PayPal descriptions (±3 day window) -------------")
        report_df = enrich_paypal_transactions(report_df, config)
        newly_enriched = report_df["description"].str.startswith("PayPal: ", na=False).sum()
        logger.info(f"  {newly_enriched} total enriched PayPal rows")
        update_transaction_descriptions(report_df, config)

        # Re-check Miscellaneous count after enrichment (some PayPal rows may
        # now have enriched descriptions and will categorise differently)
        misc_mask = report_df["category"] == "Miscellaneous"
        misc_df = report_df[misc_mask].copy()
        misc_count = len(misc_df)
        logger.info(f"\n-- Recategorising {misc_count} Miscellaneous transactions ----------")
        if args.no_categorise:
            logger.info("  --no-categorise set: using config overrides and cache only")

        recategorised = categorise_transactions(
            misc_df, config,
            use_api=not args.no_categorise,
            force_recategorise=True,
        )
        still_misc = (recategorised["category"] == "Miscellaneous").sum()
        changed = misc_count - still_misc
        logger.info(f"  Done ({changed} re-categorised, {still_misc} remain Miscellaneous)")

        logger.info("\n-- Saving updated categories to database ------------------------")
        update_transaction_categories(recategorised, config)

        # Reload to get a consistent full dataset for reports
        report_df = load_transactions(config)

        logger.info("\n-- Applying Pay-in-4 merchant categories -----------------------")
        from src.payin4_detector import load_payin4_groups, save_payin4_groups
        existing_groups = load_payin4_groups(config)
        if existing_groups:
            existing_groups, report_df, _p4_ids = categorise_payin4_merchants(
                existing_groups, report_df, config, use_api=not args.no_categorise
            )
            save_payin4_groups(existing_groups, config)
            if _p4_ids:
                _p4_df = report_df[report_df["txn_id"].astype(str).isin(_p4_ids)]
                update_transaction_categories(_p4_df, config)
                report_df = load_transactions(config)

        if not args.no_recommend:
            logger.info("\n-- Generating recommendations -----------------------------------")
            generate_recommendations(report_df, config)

        logger.info("\n-- Generating reports -------------------------------------------")
        generate_all_reports(report_df, config)

        misc_remaining = (report_df["category"] == "Miscellaneous").sum()
        output_dir = config["data"]["output_dir"]
        logger.info(f"""
-- Done ---------------------------------------------------------
  {changed} transaction(s) re-categorised from Miscellaneous.
  {misc_remaining} remain Miscellaneous (check review.html).

  Open in your browser:
    {output_dir}/review.html  ({misc_remaining} Miscellaneous to review)
-----------------------------------------------------------------
""")
        sys.exit(0)

    # -- Load new transactions from Raw Data ----------------------------------
    logger.info("\n-- Loading transactions -----------------------------------------")
    _balance_snapshots: list[dict] = []
    df = load_all_transactions(config, balance_collector=_balance_snapshots)
    new_count = len(df)

    if new_count:
        # -- Enrich PayPal ----------------------------------------------------
        logger.info("\n-- PayPal enrichment --------------------------------------------")
        df = enrich_paypal_transactions(df, config)

        # -- Categorise -------------------------------------------------------
        logger.info("\n-- Categorising transactions ------------------------------------")
        if args.no_categorise:
            logger.info("  --no-categorise set: using config overrides and cache only")
        df = categorise_transactions(df, config, use_api=not args.no_categorise)

        uncategorised = (df["category"] == "Miscellaneous").sum()
        logger.info(f"  Categorised {new_count} transactions "
                    f"({uncategorised} fell back to Miscellaneous)")
    else:
        logger.info("  No new files in Raw Data — regenerating reports from database")

    # -- Save balance snapshots extracted during parsing ----------------------
    if _balance_snapshots and not args.no_archive:
        from src.balance_tracker import save_balance_history
        save_balance_history(_balance_snapshots, config)
        logger.info(f"  Saved {len(_balance_snapshots)} balance snapshot(s) -> SQLite")

    # -- Store new transactions in database ------------------------------------
    # zip_name is recorded in each row for traceability back to the source archive.
    zip_name = ""
    if new_count and not args.no_archive:
        logger.info("\n-- Storing new transactions to database -------------------------")
        zip_name = plan_zip_name(config)
        upsert_transactions(df, config, zip_name)
    elif new_count:
        logger.info("\n-- Database update skipped (--no-archive) -----------------------")

    # -- Load all transactions from database for report generation -------------
    report_df = load_transactions(config)

    if report_df.empty and df.empty:
        logger.info("\nNo transactions found and no master CSV exists.")
        logger.info(f"Add statement files to: {config['data']['input_dir']}")
        sys.exit(0)

    # Preview mode (--no-archive): merge new transactions in-memory so reports
    # reflect the current run without persisting anything.
    if new_count and args.no_archive:
        if not report_df.empty:
            seen_ids = set(report_df["txn_id"].dropna())
            fresh = df[~df["txn_id"].isin(seen_ids)]
            report_df = pd.concat([report_df, fresh], ignore_index=True)
        else:
            report_df = df
        report_df = report_df.sort_values("date").reset_index(drop=True)

    if report_df.empty:
        report_df = df

    # -- Detect Pay-in-4 groups --------------------------------------------------
    from src.payin4_detector import (
        detect_payin4_groups, merge_groups,
        load_payin4_groups, save_payin4_groups,
    )
    new_groups = detect_payin4_groups(report_df)
    existing_groups = load_payin4_groups(config)
    merged_groups = merge_groups(existing_groups, new_groups)
    if new_groups:
        logger.info(f"  Pay-in-4: {len(merged_groups)} plan(s) detected "
                    f"({len(new_groups)} new)")

    merged_groups, report_df, _p4_changed = categorise_payin4_merchants(
        merged_groups, report_df, config, use_api=not args.no_categorise
    )
    save_payin4_groups(merged_groups, config)
    if _p4_changed:
        _changed_df = report_df[report_df["txn_id"].astype(str).isin(_p4_changed)]
        update_transaction_categories(_changed_df, config)

    # Keep a reference to the full dataset before any display filter is applied.
    # Recommendations always cover the entire master CSV period.
    full_report_df = report_df

    # -- Month filter (display only — ingestion always processes all new data) -
    if args.month:
        mask = report_df["date"].dt.to_period("M").astype(str) == args.month
        report_df = report_df[mask].reset_index(drop=True)
        if report_df.empty:
            logger.info(f"\nNo transactions found for {args.month}.")
            sys.exit(0)
        logger.info(f"\nFiltered to {len(report_df)} transactions for {args.month}")

    # -- Financial year filter (display only) ----------------------------------
    if args.fy:
        try:
            fy_year = int(args.fy)
        except ValueError:
            logger.error(f"ERROR: --fy expects a 4-digit year, e.g. --fy 2026")
            sys.exit(1)
        fy_start = pd.Timestamp(f"{fy_year - 1}-07-01")
        fy_end   = pd.Timestamp(f"{fy_year}-06-30")
        mask = (report_df["date"] >= fy_start) & (report_df["date"] <= fy_end)
        report_df = report_df[mask].reset_index(drop=True)
        if report_df.empty:
            logger.info(f"\nNo transactions found for FY{fy_year} ({fy_year-1}-07-01 to {fy_year}-06-30).")
            sys.exit(0)
        logger.info(f"\nFiltered to {len(report_df)} transactions for FY{fy_year} "
                    f"({fy_year-1}-07-01 to {fy_year}-06-30)")

    # -- Recommendations (always from the full master CSV period) -------------
    if not args.no_recommend:
        logger.info("\n-- Generating recommendations -----------------------------------")
        generate_recommendations(full_report_df, config)

    # -- Reports ---------------------------------------------------------------
    logger.info("\n-- Generating reports -------------------------------------------")
    generate_all_reports(report_df, config)

    # -- Zip Raw Data and clear (after reports are generated) -----------------
    if new_count and not args.no_archive:
        logger.info("\n-- Archiving source files ---------------------------------------")
        create_zip_and_clear(config, zip_name)
    elif new_count:
        logger.info("\n-- Archive skipped (--no-archive) -------------------------------")
    else:
        logger.info("\n-- Archive skipped (no new files in Raw Data) -------------------")

    # -- Summary --------------------------------------------------------------
    output_dir = config["data"]["output_dir"]
    misc_count = (report_df["category"] == "Miscellaneous").sum()
    logger.info(f"""
-- Done ---------------------------------------------------------
  Open in your browser:
    {output_dir}/monthly_summary.html   (dashboard + recommendations)
    {output_dir}/transactions.html      (search all transactions)
    {output_dir}/review.html            ({misc_count} Miscellaneous to review)

  To apply review overrides:
    python finance_analyser.py --apply-review review_overrides.json

  Database:
    {config.get("data", {}).get("database", "data/finance.db")}

  Archived source files:
    {config["data"].get("archive_dir", "Data/Archive")}/
-----------------------------------------------------------------
""")


if __name__ == "__main__":
    main()
