"""Australian income tax, HECS/HELP repayment, and franking credit estimation.

All results are estimates only. Verify with ATO at ato.gov.au and a qualified
tax professional before relying on these figures.

Update _TAX_BRACKETS and _HELP_THRESHOLDS each July when new ATO rates are
published.
"""

# ──────────────────────────────────────────────────────────────────────────────
# ATO individual income tax brackets
# Stage 3 cuts applied from 1 July 2024 (FY2025+).
# Format: (lower_threshold_inclusive, base_tax, marginal_rate)
# ──────────────────────────────────────────────────────────────────────────────

def _compute_gross_tax_2025_plus(income: float) -> float:
    """ATO individual brackets from 1 Jul 2024 (Stage 3 cuts)."""
    if income <= 18_200:
        return 0.0
    if income <= 45_000:
        return (income - 18_200) * 0.19
    if income <= 135_000:
        return 5_092 + (income - 45_000) * 0.325
    if income <= 190_000:
        return 34_342 + (income - 135_000) * 0.37
    return 54_692 + (income - 190_000) * 0.45


def _compute_gross_tax_pre_2025(income: float) -> float:
    """ATO individual brackets before Stage 3 cuts (FY2024 and earlier)."""
    if income <= 18_200:
        return 0.0
    if income <= 45_000:
        return (income - 18_200) * 0.19
    if income <= 120_000:
        return 5_092 + (income - 45_000) * 0.325
    if income <= 180_000:
        return 29_467 + (income - 120_000) * 0.37
    return 51_667 + (income - 180_000) * 0.45


_GROSS_TAX_FN: dict[int, object] = {
    2026: _compute_gross_tax_2025_plus,
    2025: _compute_gross_tax_2025_plus,
    2024: _compute_gross_tax_pre_2025,
}


def _lito(income: float) -> float:
    """Low Income Tax Offset — up to $700 reduction, phases out by $66,667."""
    if income <= 37_500:
        return 700.0
    if income <= 45_000:
        return max(0.0, 700.0 - (income - 37_500) * 0.05)
    if income <= 66_667:
        return max(0.0, 325.0 - (income - 45_000) * 0.015)
    return 0.0


def _medicare(income: float) -> float:
    """Medicare levy — 2% above phase-in range ($23,365–$29,207)."""
    if income >= 29_207:
        return income * 0.02
    if income >= 23_365:
        return (income - 23_365) * 0.10
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# HECS / HELP repayment thresholds (2024-25 rates — confirm at ato.gov.au annually)
# Format: (lower_income_inclusive, upper_income_exclusive_or_None, rate_pct)
# ──────────────────────────────────────────────────────────────────────────────
_HELP_THRESHOLDS_2025 = [
    (0,        54_435,  0.0),
    (54_435,   63_217,  1.0),
    (63_217,   66_308,  2.0),
    (66_308,   71_593,  2.5),
    (71_593,   77_226,  3.0),
    (77_226,   83_763,  3.5),
    (83_763,   88_653,  4.0),
    (88_653,   94_523,  4.5),
    (94_523,  103_598,  5.0),
    (103_598, 117_357,  5.5),
    (117_357, 128_961,  6.0),
    (128_961, 140_640,  6.5),
    (140_640, 145_021,  7.0),
    (145_021, 153_665,  7.5),
    (153_665, 158_663,  8.0),
    (158_663, 168_744,  8.5),
    (168_744, 174_990,  9.0),
    (174_990, None,    10.0),
]

_HELP_THRESHOLDS: dict[int, list] = {
    2025: _HELP_THRESHOLDS_2025,
    2026: _HELP_THRESHOLDS_2025,  # use 2024-25 as closest confirmed; update each July
}


def _closest_fy(table: dict, fy: int):
    if fy in table:
        return table[fy]
    return table[min(table.keys(), key=lambda k: abs(k - fy))]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def estimate_income_tax(taxable_income: float, fy: int) -> dict:
    """Estimate Australian individual income tax for a given taxable income.

    Returns:
        gross_tax, lito, tax_after_lito, medicare_levy, total_tax,
        net_income, effective_rate_pct
    """
    income = max(0.0, float(taxable_income))
    fn = _closest_fy(_GROSS_TAX_FN, fy)
    gross_tax = fn(income)
    lito = _lito(income)
    tax_after_lito = max(0.0, gross_tax - lito)
    medicare = _medicare(income)
    total_tax = round(tax_after_lito + medicare, 2)
    effective_rate = round(total_tax / income * 100, 1) if income > 0 else 0.0
    return {
        "gross_tax":        round(gross_tax, 2),
        "lito":             round(lito, 2),
        "tax_after_lito":   round(tax_after_lito, 2),
        "medicare_levy":    round(medicare, 2),
        "total_tax":        total_tax,
        "net_income":       round(income - total_tax, 2),
        "effective_rate_pct": effective_rate,
    }


def estimate_hecs_repayment(income: float, fy: int) -> dict | None:
    """Return HELP/HECS repayment estimate, or None if income is below threshold."""
    thresholds = _closest_fy(_HELP_THRESHOLDS, fy)
    for lo, hi, rate_pct in thresholds:
        if rate_pct == 0.0:
            continue
        if income >= lo and (hi is None or income < hi):
            return {
                "threshold":  lo,
                "rate_pct":   rate_pct,
                "repayment":  round(income * rate_pct / 100, 2),
                "income":     income,
            }
    return None


def gross_up_dividend(
    cash_dividend: float,
    franking_pct: float = 100.0,
    company_tax_rate: float = 30.0,
) -> dict:
    """Gross up a cash dividend to determine the imputed franking credit.

    Uses the formula: credit = cash × (franking_pct/100) × rate/(1−rate)
    """
    cash = max(0.0, float(cash_dividend))
    frac = (franking_pct / 100) * company_tax_rate / (100.0 - company_tax_rate)
    franking_credit = round(cash * frac, 2)
    grossed_up = round(cash + franking_credit, 2)
    return {
        "cash_dividend":   round(cash, 2),
        "franking_credit": franking_credit,
        "grossed_up":      grossed_up,
    }
