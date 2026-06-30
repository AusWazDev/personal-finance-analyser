"""
Generates reports/recommendations.md using Claude Sonnet.

Builds a JSON summary of spending patterns and sends it to the API,
asking for structured, actionable financial advice.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.ai_backend import get_backend as _get_backend
from src.utils import md_to_html as _md_to_html, EXCLUDE_FROM_SPEND as _EXCLUDE_FROM_SPEND

logger = logging.getLogger(__name__)


def generate_recommendations(df: pd.DataFrame, config: dict) -> None:
    output_dir = Path(config["data"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "recommendations.md"

    if df.empty:
        output_path.write_text("# Recommendations\n\nNo transaction data available.\n")
        return

    summary = _build_summary(df, config)
    summary["company_name"] = (
        config.get("business", {}).get("full_name")
        or config.get("business", {}).get("company_name", "your employer")
    )
    prompt = _build_prompt(summary)

    client = _get_backend(config)

    try:
        model = config.get("models", {}).get("recommendations", "claude-sonnet-4-6")
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        body = re.sub(r"^#[^\n]*\n", "", response.content[0].text.lstrip(), count=1) \
               if response.content[0].text.lstrip().startswith("#") and \
                  not response.content[0].text.lstrip().startswith("##") \
               else response.content[0].text
    except Exception as exc:
        logger.warning(f"  Warning: recommendations API call failed — {exc}")
        if output_path.exists():
            logger.info(f"  Keeping existing {output_path} (API call failed, previous content preserved)")
        else:
            err = str(exc)
            if "credit balance" in err.lower() or "insufficient" in err.lower():
                body = (
                    "## Recommendations unavailable\n\n"
                    "**Anthropic account credits are exhausted.**\n\n"
                    "Top up at console.anthropic.com/settings/billing to re-enable this feature.\n"
                )
            else:
                body = f"## Recommendations unavailable\n\n**API error:** {exc}\n"
            output_path.write_text(
                "# Financial Recommendations\n\n" + body, encoding="utf-8"
            )
        return

    content = (
        "# Financial Recommendations\n"
        f"*Generated {datetime.now().strftime('%x %H:%M')}*\n\n"
        + body
    )
    output_path.write_text(content, encoding="utf-8")
    logger.info(f"  Recommendations -> {output_path}")


def _load_superseded_pairs(config: dict | None) -> list:
    if not config:
        return []
    try:
        rel = config.get("data", {}).get("superseded_pairs_file", "data/superseded_pairs.json")
        p = Path(rel)
        if not p.is_absolute():
            p = Path(__file__).parent.parent / rel
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _build_summary(df: pd.DataFrame, config: dict | None = None) -> dict:
    """Build the JSON summary dict used for the Claude recommendations prompt."""
    spend = df[(df["amount"] < 0) & ~df["category"].isin(_EXCLUDE_FROM_SPEND)].copy()
    spend["amount_abs"] = spend["amount"].abs()
    spend["month"] = spend["date"].dt.to_period("M").astype(str)

    by_cat = spend.groupby("category")["amount_abs"].sum().sort_values(ascending=False).to_dict()
    monthly = spend.groupby("month")["amount_abs"].sum()
    monthly_mean = float(monthly.mean()) if not monthly.empty else 0
    spikes = {
        m: float(v)
        for m, v in monthly.items()
        if v > monthly_mean * 1.3 and monthly_mean > 0
    }
    monthly_income = (
        df[df["category"] == "Income"]
        .groupby(df["date"].dt.to_period("M").astype(str))["amount"]
        .sum().to_dict()
    )
    monthly_board = (
        df[df["category"] == "Board & Lodging"]
        .groupby(df["date"].dt.to_period("M").astype(str))["amount"]
        .sum().to_dict()
    )
    spend["merchant_clean"] = spend["description"].str.upper().str.strip().str[:60]

    # Net credits/reversals against debits so a reversed payment doesn't count as a spend month
    _spend_merchants = set(spend["merchant_clean"])
    _df_net = df.copy()
    _df_net["merchant_clean"] = _df_net["description"].str.upper().str.strip().str[:60]
    _df_net["month"] = _df_net["date"].dt.to_period("M").astype(str)
    _df_net = _df_net[
        _df_net["merchant_clean"].isin(_spend_merchants)
        & ~_df_net["category"].isin(_EXCLUDE_FROM_SPEND)
    ]
    _monthly_net = _df_net.groupby(["merchant_clean", "month"])["amount"].sum().reset_index()
    _monthly_net = _monthly_net[_monthly_net["amount"] < 0].copy()
    _monthly_net["amount_abs"] = _monthly_net["amount"].abs()
    _cat_map = spend.groupby("merchant_clean")["category"].first()
    _subcat_map = (
        spend[spend["sub_category"].notna() & (spend["sub_category"] != "")]
        .groupby("merchant_clean")["sub_category"].first()
        if "sub_category" in spend.columns else pd.Series(dtype=str)
    )

    recurring = (
        _monthly_net.groupby("merchant_clean")
        .agg(months=("month", "nunique"), avg=("amount_abs", "mean"),
             total=("amount_abs", "sum"), first_seen=("month", "min"),
             last_seen=("month", "max"))
        .query("months >= 2")
        .sort_values("total", ascending=False)
        .head(20)
    )
    recurring["cat"] = recurring.index.map(_cat_map)
    recurring["sub_cat"] = recurring.index.map(_subcat_map)
    recurring_list = recurring.reset_index().to_dict(orient="records")
    dining = float(by_cat.get("Dining Out", 0))
    groceries = float(by_cat.get("Groceries", 0))
    dining_ratio = round(dining / groceries, 2) if groceries > 0 else None
    biz = df[df["is_business"] & (df["amount"] < 0)].copy()
    biz_list = (
        biz[["date", "description", "amount", "category", "account"]]
        .assign(amount=lambda x: x["amount"].abs(),
                date=lambda x: x["date"].dt.strftime("%x"))
        .to_dict(orient="records")
    )
    biz_total = float(biz["amount"].abs().sum())

    superseded = _load_superseded_pairs(config)
    confirmed_superseded = [
        {"replaced": p["replaced"], "by": p["by"],
         "category": p.get("category", ""), "note": p.get("note", "")}
        for p in superseded
    ]

    return {
        "date_range": {
            "from": str(df["date"].min().date()),
            "to":   str(df["date"].max().date()),
        },
        "income_note": "Variable — distributed via Family Trust. No fixed salary.",
        "monthly_trust_income":  {k: round(v, 2) for k, v in monthly_income.items()},
        "monthly_board_income":  {k: round(v, 2) for k, v in monthly_board.items()},
        "spend_by_category_aud": {k: round(v, 2) for k, v in by_cat.items()},
        "monthly_total_spend":   {k: round(v, 2) for k, v in monthly.items()},
        "average_monthly_spend": round(monthly_mean, 2),
        "spike_months":          {k: round(v, 2) for k, v in spikes.items()},
        "dining_to_groceries_ratio": dining_ratio,
        "dining_total_aud":      round(dining, 2),
        "groceries_total_aud":   round(groceries, 2),
        "top_recurring_merchants": recurring_list,
        "confirmed_superseded_pairs": confirmed_superseded,
        "business_expense_total_aud": round(biz_total, 2),
        "business_expenses":     biz_list[:30],
    }


def _build_prompt(summary: dict) -> str:
    superseded_note = ""
    if summary.get("confirmed_superseded_pairs"):
        pairs_text = "\n".join(
            f"  - {p['replaced']} was replaced by {p['by']}"
            + (f" ({p['category']})" if p.get("category") else "")
            + (f": {p['note']}" if p.get("note") else "")
            for p in summary["confirmed_superseded_pairs"]
        )
        superseded_note = f"""
IMPORTANT — confirmed provider replacements (do NOT flag these as duplicates or waste):
{pairs_text}

"""

    return f"""You are a financial advisor reviewing an Australian household's bank and credit card transactions.

Here is a JSON summary of their spending analysis:

{json.dumps(summary, indent=2, default=str)}

{superseded_note}Rules for analysing recurring merchants:
1. Each merchant in `top_recurring_merchants` has `first_seen` and `last_seen` month fields.
2. Only flag two merchants in the same category as DUPLICATE subscriptions if their active date
   ranges overlap by MORE than 2 months. Sequential providers (one ending before the other starts)
   are normal provider switches — do not flag them.
3. Any pair listed in `confirmed_superseded_pairs` has been explicitly confirmed as a replacement
   by the user — never flag these as duplicate or redundant regardless of dates.
4. ALWAYS trust the `cat` and `sub_cat` fields. Do not re-categorise or speculate about what a
   merchant does based on its name — the user has already categorised every transaction. Transaction
   descriptions often come from PayPal or bank intermediaries and may not match the actual merchant.
   If a merchant is categorised as "Utilities", treat it as a utility bill. Never contradict the
   assigned category or sub-category.
5. Do NOT suggest any merchant might be a business expense unless it explicitly appears in the
   `business_expenses` list. The user has already marked business transactions — do not second-guess
   personal utilities, subscriptions, or other categorised items as potentially business-related.

Write practical financial recommendations. Use Australian context (AUD, Australian providers).
Be specific — name actual merchants from the data where relevant.
Keep each section concise (3–6 bullet points max).
Do NOT include a document title, filename, or any top-level `#` heading — begin directly with `## Subscription Audit`.

Structure:

## Subscription Audit
- List subscriptions detected (≥2 months) and flag any that may be duplicate or unused.
- State approximate monthly total across all subscriptions.

## Categories Worth Shopping Around
- Identify utilities, insurance, fuel, or phone plans where savings are likely available.
- Suggest specific Australian alternatives where relevant (e.g. Belong for Optus, Beem for payments).

## Spend Spike Analysis
- For each spike month, note the amount, % above average, and likely cause based on merchants.
- If no spikes, confirm spending is consistent.

## Dining Out vs Groceries
- State the ratio and total for each.
- Comment on whether the balance seems healthy and give a specific suggestion.

## Business Expense Summary
- List all flagged business expenses in a markdown table (Date | Merchant | Amount | Category).
- State the total reimbursable amount.
- Remind the user to submit these to their employer ({summary.get("company_name", "their employer")}).

## Quick Wins
Provide exactly 5 specific, numbered actions the user can take this month to reduce spending,
each with an estimated saving in AUD.
"""


def generate_recommendations_html(
    df: pd.DataFrame,
    config: dict,
    period_label: str = "All Time",
) -> str:
    """Generate recommendations for a given (already-filtered) DataFrame and return HTML."""
    if df.empty:
        return _md_to_html("# Recommendations\n\nNo transaction data available for this period.\n")

    summary = _build_summary(df, config)
    summary["company_name"] = (
        config.get("business", {}).get("full_name")
        or config.get("business", {}).get("company_name", "your employer")
    )
    prompt = _build_prompt(summary)

    client = _get_backend(config)

    try:
        model = config.get("models", {}).get("recommendations", "claude-sonnet-4-6")
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        body = re.sub(r"^#[^\n]*\n", "", raw.lstrip(), count=1) \
               if raw.lstrip().startswith("#") and not raw.lstrip().startswith("##") \
               else raw
    except Exception as exc:
        err = str(exc)
        if "credit balance" in err.lower() or "insufficient" in err.lower():
            body = (
                "## Recommendations unavailable\n\n"
                "**Anthropic account credits are exhausted.**\n\n"
                "Top up at console.anthropic.com/settings/billing to re-enable this feature.\n"
            )
        else:
            body = f"## Recommendations unavailable\n\n**API error:** {exc}\n"

    md = (
        f"# Financial Recommendations — {period_label}\n"
        f"*Generated {datetime.now().strftime('%x %H:%M')}*\n\n"
        + body
    )
    return _md_to_html(md)
