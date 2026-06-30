"""
Transaction categoriser.

Pipeline:
  1. Account-type shortcuts (savings → Transfers, investment → Investment)
  2. Config merchant_categories overrides (no API call needed)
  3. JSON cache lookup (avoids re-calling API for seen descriptions)
  4. Claude Haiku batch API call for everything else
  5. Refund detection (credits matching a recent debit inherit the debit's category)
  6. Business expense post-processing (config keyword overrides)
  7. txn_id overrides (highest priority — always wins)
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from src.ai_backend import get_backend as _get_backend
from src.utils import is_valid_subcat, VALID_CATEGORIES

logger = logging.getLogger(__name__)

BATCH_SIZE = 60

_SYSTEM_PROMPT = """You are a personal finance transaction categoriser for an Australian bank account holder.

Categorise each transaction into EXACTLY one of:
Housing | Groceries | Dining Out | Transport | Subscriptions | Utilities |
Health | Insurance | Entertainment | Personal Care | Travel | Education |
Gifts Given | Gifts Received | Donations | Bank Fees & Charges | Bank Interest Charged | Interest Income |
Income | Board & Lodging | Business Reimbursement | Family Loan Received | Family Loan Repayment |
Transfers | Business Expense | Investment | Miscellaneous

Rules:
- Housing: rent (DEFT, BPAY real estate), mortgage, body corporate fees
- Groceries: supermarkets (Woolworths, Coles, Aldi, IGA), fresh produce, butchers, bakeries
- Dining Out: restaurants, cafes, fast food, takeaway, UberEats, DoorDash, Menulog
- Transport: fuel (BP, Ampol, Caltex, Mobil), parking, tolls, VicRoads rego, rideshare (Uber), public transport (Myki)
- Subscriptions: streaming (Netflix, Spotify, Disney+, HBO, Prime Video), software (Microsoft 365), annual memberships
- Utilities: electricity, gas, water (Yarra Valley Water), internet, phone (Optus, Telstra)
- Health: pharmacy, GP, dentist, hospital, health insurance (Australian Unity, Medibank, Bupa), physio, optical
- Insurance: car insurance, home/contents insurance, life insurance, income protection — NOT health insurance
- Entertainment: pubs/bars, events, gaming (Xbox, PlayStation), cinemas, sporting events
- Personal Care: haircuts, beauty salon, gym, clothing, cosmetics, massage
- Travel: flights (Jetstar, Qantas, Virgin), hotels, Airbnb, holiday packages, travel agencies — NOT daily commuting
- Education: school fees, HECS/HELP, online courses, textbooks, tutoring, professional development
- Gifts Given: money spent on personal gifts for others — bottle shops (BWS, Dan Murphy's, Liquorland), florists, gift wrapping. NOT charitable donations.
- Gifts Received: money received as a personal gift (birthday money, cash present from family/friends) — a CREDIT to your account
- Donations: charitable donations to DGR-registered organisations (tax deductible), fundraising contributions, community giving
- Bank Fees & Charges: account-keeping fees, monthly fees, overdrawn fees, late fees, dishonour fees, ATM charges, overseas transaction fees
- Bank Interest Charged: interest charged by a bank or credit card on outstanding balances, purchase interest, cash advance interest
- Interest Income: bank interest credited to your account on savings or deposits (you RECEIVE this money)
- Income: salary, wages, trust distributions, dividends, government payments (Centrelink), tax refunds
- Board & Lodging: regular payments from household members (board, rent contribution)
- Business Reimbursement: lump-sum or individual credits from an employer reimbursing documented work expenses — money you already spent that is being paid back
- Family Loan Received: a credit that is explicitly a personal loan from a family member — ONLY use if the description clearly states "loan" or you are certain it is borrowed money, NOT for general gifts or transfers between accounts
- Family Loan Repayment: a debit that explicitly repays a personal loan to a family member — ONLY use if the description clearly states "loan repayment"; payments between own accounts or regular family transfers are Transfers, NOT this category
- Transfers: payments between own accounts, credit card repayments, BPAY to own accounts
- Business Expense: ASIC fees, web hosting (Webcentral), work travel, professional services — only if clearly work-related and reimbursable
- Investment: share purchases, ETF contributions, brokerage fees (CMC Markets), managed funds, superannuation top-ups
- Miscellaneous: anything that genuinely doesn't fit the above categories

Also return business=true only if the transaction is clearly a work expense that an employer would reimburse.

Respond with a JSON array (no prose):
[{"id": <index>, "category": "<category>", "business": <true|false>}, ...]
"""


def _load_cache(path: str) -> dict:
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(cache, f, indent=2)


def _write_run_metrics(updates: dict, config: dict) -> None:
    """Merge updates into run_metrics.json, stamping updated_at."""
    metrics_path = Path(config.get("data", {}).get("run_metrics_file", "Data/run_metrics.json"))
    try:
        existing = json.loads(metrics_path.read_text("utf-8")) if metrics_path.exists() else {}
    except Exception:
        existing = {}
    existing.update(updates)
    existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(existing, indent=2), "utf-8")
    except Exception:
        pass


def _cache_key(description: str, amount: float) -> str:
    sign = "cr" if amount >= 0 else "dr"
    return f"{str(description).upper().strip()[:80]}|{sign}"


def _config_category(description: str, config: dict) -> tuple[str, str] | tuple[None, None]:
    """Return (category, sub_category) from merchant rules, or (None, None) if no match.
    Rule values may be a plain category string or {"category": ..., "sub_category": ...}.
    """
    desc_upper = str(description).upper()
    for merchant, rule in config.get("merchant_categories", {}).items():
        if merchant.upper() in desc_upper:
            if isinstance(rule, dict):
                return rule.get("category", "Miscellaneous"), rule.get("sub_category", "")
            return str(rule), ""
    return None, None


def _is_business_by_config(row: pd.Series, config: dict) -> bool:
    biz_cfg = config.get("business", {})
    keywords = biz_cfg.get("expense_keywords", [])
    merchants = biz_cfg.get("known_business_merchants", [])
    desc = str(row.get("description", "")).upper()
    note = str(row.get("note", "")).upper()

    for kw in keywords + merchants:
        if kw.upper() in desc or kw.upper() in note:
            return True
    return False


def _call_api_with_retry(client, max_retries: int = 3, **kwargs):
    """Call AI backend with exponential backoff for transient Claude API errors."""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:
            _cls = type(exc).__name__
            is_rate_limit = _cls == "RateLimitError"
            is_server_err = _cls == "APIStatusError" and getattr(exc, "status_code", 0) >= 500
            is_conn_err = _cls == "APIConnectionError"
            if not (is_rate_limit or is_server_err or is_conn_err):
                raise
            if attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                if is_rate_limit:
                    logger.warning(f"  Rate limit — retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                elif is_server_err:
                    logger.warning(f"  API error {getattr(exc, 'status_code', '?')} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                else:
                    logger.warning(f"  Network error — retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise


def _parse_api_response(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return []


def _load_overrides(config: dict) -> dict:
    """Load txn_id-level overrides from data/transaction_overrides.json."""
    path = Path(config.get("data", {}).get("overrides_file", "data/transaction_overrides.json"))
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def categorise_transactions(df: pd.DataFrame, config: dict,
                            use_api: bool = True,
                            force_recategorise: bool = False) -> pd.DataFrame:
    if df.empty:
        return df

    # Merge user-defined merchant rules (higher priority than config.yaml)
    rules_path = Path(config.get("data", {}).get("merchant_rules_file", "data/merchant_rules.json"))
    if rules_path.exists():
        try:
            with open(rules_path, encoding="utf-8") as f:
                user_rules = json.load(f)
            if user_rules:
                merged = {**config.get("merchant_categories", {}), **user_rules}
                config = {**config, "merchant_categories": merged}
        except Exception:
            pass

    cache_path = config.get("data", {}).get("cache_file", "data/categorisation_cache.json")
    cache = _load_cache(cache_path)

    client = _get_backend(config)

    income_cfg     = config.get("income", {})
    board_cfg      = config.get("board_income", {})
    holder_name    = income_cfg.get("account_holder_name", "").upper()
    company_name   = config.get("business", {}).get("company_name", "").upper()
    reimb_kws      = [k.upper() for k in config.get("business", {}).get("reimbursement_keywords", [])]
    income_kws     = [k.upper() for k in income_cfg.get("income_keywords", [])]
    trust_kws      = [k.upper() for k in income_cfg.get("trust_keywords", [])]
    known_payers   = [k.upper() for k in income_cfg.get("known_income_payers", [])]
    board_payers   = [k.upper() for k in board_cfg.get("payers", [])]
    board_note_kws = [k.upper() for k in board_cfg.get("note_keywords", [])]
    gift_kws       = [k.upper() for k in config.get("gifts", {}).get("received_keywords", [])]
    fl_contacts    = config.get("family_loans", {}).get("contacts", [])
    fl_in_kws  = [([k.upper() for k in c.get("in_keywords",  [])], c.get("account", "")) for c in fl_contacts]
    fl_out_kws = [([k.upper() for k in c.get("out_keywords", [])], c.get("account", "")) for c in fl_contacts]

    # ── Pre-compute string columns (uppercased, null-safe) ────────────────────
    desc_upper  = df["description"].fillna("").str.upper().str.strip()
    note_upper  = (df["note"].fillna("").str.upper().str.strip()
                   if "note" in df.columns else pd.Series("", index=df.index))
    payee_upper = (df["payee_name"].fillna("").str.upper().str.strip()
                   if "payee_name" in df.columns else pd.Series("", index=df.index))
    note_raw    = df["note"].fillna("") if "note" in df.columns else pd.Series("", index=df.index)
    acct_type_s = df["account_type"].fillna("")
    amount_s    = df["amount"].fillna(0.0)
    acct_s      = df["account"].fillna("") if "account" in df.columns else pd.Series("", index=df.index)

    # ── Output Series (index-aligned with df) ─────────────────────────────────
    cat  = pd.Series(None,  index=df.index, dtype=object)
    sub  = pd.Series("",   index=df.index, dtype=object)
    biz  = pd.Series(False, index=df.index)
    todo = pd.Series(True,  index=df.index)   # True = not yet assigned

    def _assign(mask: "pd.Series[bool]", cat_val: str, sub_val: str = "") -> None:
        cat[mask]  = cat_val
        sub[mask]  = sub_val
        todo[mask] = False

    def _any_kw(series: pd.Series, kws: list[str]) -> "pd.Series[bool]":
        """True where any keyword appears in the (already-uppercased) series."""
        if not kws:
            return pd.Series(False, index=series.index)
        return series.apply(lambda s: any(k in s for k in kws))

    # ── Priority rules (first match per row wins) ─────────────────────────────

    # Account-type shortcuts
    _assign(todo & (acct_type_s == "savings"),    "Transfers")
    _assign(todo & (acct_type_s == "investment"), "Investment")
    # PayPal CSV rows are enrichment reference data — real debit is on ANZ side
    _assign(todo & (acct_type_s == "paypal"),     "Transfers")

    # Unmatched ANZ PayPal debits → Miscellaneous for manual review
    _assign(
        todo & (amount_s < 0)
             & desc_upper.str.contains(r"PAYPAL|PYPL", regex=True, na=False)
             & ~desc_upper.str.startswith("PAYPAL: "),
        "Miscellaneous",
    )

    # Gifts Received
    if gift_kws:
        _assign(
            todo & (amount_s > 0) & _any_kw(desc_upper + " " + note_upper, gift_kws),
            "Gifts Received",
        )

    # Family Loan Received
    if fl_in_kws:
        fl_in = pd.Series(False, index=df.index)
        for kws, req_acct in fl_in_kws:
            kw = _any_kw(desc_upper, kws)
            fl_in |= (kw & (acct_s == req_acct)) if req_acct else kw
        _assign(todo & (amount_s >= 200) & fl_in, "Family Loan Received")

    # Family Loan Repayment
    if fl_out_kws:
        fl_out = pd.Series(False, index=df.index)
        for kws, req_acct in fl_out_kws:
            kw = _any_kw(desc_upper, kws)
            fl_out |= (kw & (acct_s == req_acct)) if req_acct else kw
        _assign(todo & (amount_s < 0) & fl_out, "Family Loan Repayment")

    # Board & Lodging (checked before general income)
    if board_payers:
        payer_match = _any_kw(desc_upper + " " + payee_upper, board_payers)
        note_ok = _any_kw(note_upper, board_note_kws) if board_note_kws else pd.Series(True, index=df.index)
        _assign(todo & (amount_s > 0) & payer_match & note_ok, "Board & Lodging")

    # Business Reimbursement: employer paying back documented expenses
    if company_name:
        _assign(
            todo & (amount_s > 0)
                 & (desc_upper + " " + payee_upper).str.contains(company_name, regex=False, na=False),
            "Business Reimbursement",
        )

    # Income: trust distributions
    if trust_kws:
        _assign(todo & (amount_s > 0) & _any_kw(desc_upper + " " + payee_upper, trust_kws), "Income")

    # Income: known income keywords in description
    if income_kws:
        _assign(todo & (amount_s > 0) & _any_kw(desc_upper, income_kws), "Income")

    # Income: known individual payers
    if known_payers:
        _assign(todo & (amount_s > 0) & _any_kw(desc_upper + " " + payee_upper, known_payers), "Income")

    # Business Reimbursement: note explicitly says reimbursement
    if reimb_kws:
        _assign(todo & (amount_s > 0) & _any_kw(note_upper, reimb_kws), "Business Reimbursement")

    # Transfers: self-transfers between own accounts
    if holder_name:
        _assign(
            todo & (amount_s > 0)
                 & payee_upper.str.contains(holder_name, regex=False, na=False),
            "Transfers",
        )

    # Config merchant override (first match per row wins — dict iteration order)
    for merchant, rule in config.get("merchant_categories", {}).items():
        m = todo & desc_upper.str.contains(merchant.upper(), regex=False, na=False)
        if not m.any():
            continue
        if isinstance(rule, dict):
            mc, ms = rule.get("category", "Miscellaneous"), rule.get("sub_category", "")
        else:
            mc, ms = str(rule), ""
        cat[m]  = mc
        sub[m]  = ms
        biz[m]  = (mc == "Business Expense")
        todo[m] = False

    # Cache lookup
    cache_keys = desc_upper.str[:80] + "|" + amount_s.map(lambda a: "cr" if a >= 0 else "dr")
    if not force_recategorise:
        cache_hit = todo & cache_keys.isin(cache)
        if cache_hit.any():
            hit_data = cache_keys[cache_hit].map(cache)
            cat[cache_hit] = hit_data.map(lambda d: d["category"] if isinstance(d, dict) else None)
            sub[cache_hit] = hit_data.map(lambda d: d.get("sub_category", "") if isinstance(d, dict) else "")
            biz[cache_hit] = hit_data.map(lambda d: bool(d.get("business", False)) if isinstance(d, dict) else False)
            todo[cache_hit] = False

    # ── API classification ────────────────────────────────────────────────────
    n_todo = int(todo.sum())
    if n_todo and not use_api:
        logger.info(f"  Skipping API ({n_todo} transactions will be Miscellaneous)")

    if n_todo and use_api:
        logger.info(f"  Calling Claude API for {n_todo} uncached transactions ...")

        # Deduplicate: same cache key → same result
        key_to_info: dict[str, dict] = {}
        for idx, key in cache_keys[todo].items():
            if key not in key_to_info:
                key_to_info[key] = {
                    "desc":    str(df.at[idx, "description"]),
                    "amount":  float(df.at[idx, "amount"]),
                    "note":    str(note_raw.at[idx]),
                    "indices": [],
                }
            key_to_info[key]["indices"].append(idx)

        unique_keys = list(key_to_info.keys())

        api_error_count = 0
        for batch_start in range(0, len(unique_keys), BATCH_SIZE):
            batch_keys = unique_keys[batch_start:batch_start + BATCH_SIZE]
            batch_items = [
                {
                    "id":          i,
                    "description": key_to_info[k]["desc"],
                    "amount":      key_to_info[k]["amount"],
                    "note":        key_to_info[k]["note"],
                }
                for i, k in enumerate(batch_keys)
            ]

            try:
                model = config.get("models", {}).get("categoriser", "claude-haiku-4-5-20251001")
                response = _call_api_with_retry(
                    client,
                    model=model,
                    max_tokens=2048,
                    system=_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Categorise these Australian bank transactions:\n"
                            + json.dumps(batch_items, indent=2)
                        ),
                    }],
                )

                results = _parse_api_response(response.content[0].text)
                for r in results:
                    batch_idx = r.get("id")
                    if batch_idx is None or batch_idx >= len(batch_keys):
                        continue
                    k = batch_keys[batch_idx]
                    api_cat = r.get("category", "Miscellaneous")
                    if api_cat == "Gifts & Donations":
                        api_cat = "Gifts Given"
                    if api_cat not in VALID_CATEGORIES:
                        api_cat = "Miscellaneous"
                    api_biz = bool(r.get("business", False))

                    cache[k] = {"category": api_cat, "business": api_biz}
                    for idx in key_to_info[k]["indices"]:
                        cat[idx] = api_cat
                        biz[idx] = api_biz

            except Exception as exc:
                logger.warning(f"  Warning: API batch error -- {exc}")
                api_error_count += 1

        _save_cache(cache, cache_path)
        _write_run_metrics({"api_errors": api_error_count}, config)

    # ── Build result columns ──────────────────────────────────────────────────
    df = df.copy()
    df["category"]    = cat.fillna("Miscellaneous")
    df["sub_category"] = sub
    df["is_business"]  = biz
    if "user_note" not in df.columns:
        df["user_note"] = ""
    else:
        df["user_note"] = df["user_note"].fillna("")

    # Business keyword override (always wins — catches any rule-assigned category)
    biz_cfg = config.get("business", {})
    biz_kws = [k.upper() for k in
               biz_cfg.get("expense_keywords", []) + biz_cfg.get("known_business_merchants", [])]
    if biz_kws:
        biz_match = _any_kw(desc_upper + " " + note_upper, biz_kws)
        df.loc[biz_match, "is_business"] = True
        df.loc[biz_match & (df["amount"] < 0), "category"] = "Business Expense"

    # ── txn_id overrides (highest priority — applied last) ────────────────────
    overrides = _load_overrides(config)
    if overrides:
        applied = 0
        for i in df.index[df["txn_id"].astype(str).isin(overrides)]:
            tid = str(df.at[i, "txn_id"])
            entry = overrides[tid]
            df.at[i, "category"]     = entry.get("category",     df.at[i, "category"])
            df.at[i, "sub_category"] = entry.get("sub_category", df.at[i, "sub_category"])
            if "is_business" in entry:
                df.at[i, "is_business"] = bool(entry["is_business"])
            if "note" in entry:
                df.at[i, "user_note"] = entry["note"]
            applied += 1
        if applied:
            logger.info(f"  Applied {applied} txn_id override(s) from transaction_overrides.json")

    # ── PayPal account rows always Transfers — cannot be overridden ───────────
    # These are enrichment reference data; the real debit is on the ANZ side.
    paypal_mask = df["account_type"] == "paypal"
    if paypal_mask.any():
        df.loc[paypal_mask, "category"] = "Transfers"
        df.loc[paypal_mask, "is_business"] = False

    # ── Reversal detection ────────────────────────────────────────────────────
    # A reversal credit cancels the original debit. Mark both Transfers/Reversal
    # so they net to zero and are excluded from all spend analysis.
    reversal_mask = df["description"].str.contains(r"REVERSAL", case=False, na=False)
    if reversal_mask.any():
        already_paired: set = set()
        pairs_found = 0
        for rev_idx, rev_row in df[reversal_mask].iterrows():
            if rev_idx in already_paired:
                continue
            rev_amount = float(rev_row["amount"])
            rev_date = rev_row["date"]
            date_lo = rev_date - pd.Timedelta(days=7)
            date_hi = rev_date + pd.Timedelta(days=7)
            # Find original: opposite sign, same |amount|, same account, within ±7 days
            candidates = df[
                (df["account_type"] == rev_row["account_type"])
                & (df["date"] >= date_lo)
                & (df["date"] <= date_hi)
                & (abs(df["amount"] + rev_amount) < 0.02)
                & (df.index != rev_idx)
                & ~df["description"].str.contains(r"REVERSAL", case=False, na=False)
                & ~df.index.isin(already_paired)
            ]
            if not candidates.empty:
                orig_idx = candidates.iloc[(candidates["date"] - rev_date).abs().argsort().iloc[0]].name
                already_paired.update({rev_idx, orig_idx})
                df.at[rev_idx, "category"] = "Transfers"
                df.at[rev_idx, "sub_category"] = "Reversal"
                df.at[rev_idx, "is_business"] = False
                df.at[orig_idx, "category"] = "Transfers"
                df.at[orig_idx, "sub_category"] = "Reversal"
                df.at[orig_idx, "is_business"] = False
                pairs_found += 1
            else:
                # Unmatched reversal — still mark it so it doesn't skew income
                df.at[rev_idx, "category"] = "Transfers"
                df.at[rev_idx, "sub_category"] = "Reversal"
                df.at[rev_idx, "is_business"] = False

        if pairs_found:
            logger.info(f"  Nullified {pairs_found} reversal pair(s) (excluded from all spend analysis)")

    # ── Refund detection ──────────────────────────────────────────────────────
    # Credits categorised as Income or Miscellaneous that share a merchant prefix
    # with a recent debit (within 30 days) are treated as refunds and inherit the
    # debit's category with sub_category = "Refund".
    _REFUND_WINDOW = pd.Timedelta(days=30)
    _MKEY_LEN = 25
    _DEFINITE_INCOME = {"Board & Lodging", "Interest Income", "Business Reimbursement", "Transfers", "Investment"}

    refund_candidates = df[(df["amount"] > 0) & df["category"].isin({"Income", "Miscellaneous"})]
    if not refund_candidates.empty:
        debits = df[df["amount"] < 0].copy()
        debits["_mkey"] = debits["description"].str.upper().str.strip().str[:_MKEY_LEN]
        spendable_debits = debits[~debits["category"].isin(_DEFINITE_INCOME | {"Miscellaneous", "Transfers"})]
        refunds_found = 0
        for idx, row in refund_candidates.iterrows():
            mkey = str(row["description"]).upper().strip()[:_MKEY_LEN]
            row_date = row["date"]
            matching = spendable_debits[
                (spendable_debits["_mkey"] == mkey)
                & (spendable_debits["date"] >= row_date - _REFUND_WINDOW)
                & (spendable_debits["date"] <= row_date)
            ]
            if not matching.empty:
                inherited = matching.sort_values("date", ascending=False).iloc[0]["category"]
                df.at[idx, "category"] = inherited
                df.at[idx, "sub_category"] = "Refund"
                refunds_found += 1
        if refunds_found:
            logger.info(f"  Detected {refunds_found} refund(s) — category inherited from matching debit")

    # ── Sub-category validation ───────────────────────────────────────────────
    # Drop any sub_category that doesn't belong to its category.  Catches stale
    # cache entries where the category changed after the sub_category was set.
    invalid_sub = ~df.apply(
        lambda r: is_valid_subcat(str(r.get("category", "")), str(r.get("sub_category", ""))),
        axis=1,
    )
    if invalid_sub.any():
        df.loc[invalid_sub, "sub_category"] = ""

    return df


def categorise_payin4_merchants(
    groups: list[dict],
    df: pd.DataFrame,
    config: dict,
    use_api: bool = True,
) -> tuple[list[dict], pd.DataFrame, set[str]]:
    """Determine merchant spending category for each Pay-in-4 group, then apply
    it (sub_category='Pay-in-4') to the matched ANZ instalment rows in df.

    Groups that already have 'merchant_category' set are skipped (no repeat API
    calls). Returns (updated_groups, updated_df, set_of_changed_txn_ids).
    """
    if not groups or df.empty:
        return groups, df, set()

    # Merge user merchant rules (mirrors categorise_transactions)
    rules_path = Path(config.get("data", {}).get("merchant_rules_file", "data/merchant_rules.json"))
    if rules_path.exists():
        try:
            with open(rules_path, encoding="utf-8") as f:
                user_rules = json.load(f)
            if user_rules:
                config = {**config, "merchant_categories": {**config.get("merchant_categories", {}), **user_rules}}
        except Exception:
            pass

    cache_path = config.get("data", {}).get("cache_file", "data/categorisation_cache.json")
    cache = _load_cache(cache_path)
    cache_updated = False

    needs_api: list[tuple[int, str]] = []  # (group_index, merchant_description)

    for i, group in enumerate(groups):
        if group.get("merchant_category"):
            continue
        merchant = group.get("merchant", "").strip()
        if not merchant:
            continue
        cat, _ = _config_category(merchant, config)
        if cat:
            group["merchant_category"] = cat
            continue
        key = _cache_key(merchant, -1.0)  # purchases are debits
        if key in cache:
            group["merchant_category"] = cache[key]["category"]
            continue
        needs_api.append((i, merchant))

    if needs_api and use_api:
        client = _get_backend(config)
        batch_items = [
            {"id": j, "description": merchant, "amount": -1.0, "note": "Pay-in-4 purchase"}
            for j, (_, merchant) in enumerate(needs_api)
        ]
        try:
            model = config.get("models", {}).get("categoriser", "claude-haiku-4-5-20251001")
            response = _call_api_with_retry(
                client, model=model, max_tokens=512, system=_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": "Categorise these PayPal merchant purchases:\n" + json.dumps(batch_items, indent=2),
                }],
            )
            for r in _parse_api_response(response.content[0].text):
                batch_idx = r.get("id")
                if batch_idx is None or batch_idx >= len(needs_api):
                    continue
                group_idx, merchant = needs_api[batch_idx]
                cat = r.get("category", "Miscellaneous")
                if cat not in VALID_CATEGORIES:
                    cat = "Miscellaneous"
                groups[group_idx]["merchant_category"] = cat
                key = _cache_key(merchant, -1.0)
                cache[key] = {"category": cat, "business": bool(r.get("business", False))}
                cache_updated = True
        except Exception as exc:
            logger.warning(f"  Warning: Pay-in-4 merchant categorisation error — {exc}")

    if cache_updated:
        _save_cache(cache, cache_path)

    # Apply merchant categories to the ANZ instalment rows in df
    df = df.copy()
    changed_ids: set[str] = set()
    for group in groups:
        cat = group.get("merchant_category", "")
        if not cat or cat == "Transfers":
            continue
        anz_ids = {d["anz_txn_id"] for d in group["instalments"] if d.get("anz_txn_id")}
        if not anz_ids:
            continue
        mask = df["txn_id"].astype(str).isin(anz_ids)
        if mask.any():
            df.loc[mask, "category"] = cat
            df.loc[mask, "sub_category"] = "Pay-in-4"
            changed_ids.update(anz_ids)

    if changed_ids:
        logger.info(f"  Pay-in-4: categorised {len(changed_ids)} ANZ instalment row(s) using merchant categories")

    return groups, df, changed_ids
