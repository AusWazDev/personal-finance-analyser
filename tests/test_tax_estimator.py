"""Tests for src/tax_estimator.py."""
import pytest
from src.tax_estimator import estimate_income_tax, estimate_hecs_repayment, gross_up_dividend


# ── estimate_income_tax ────────────────────────────────────────────────────────

def test_tax_zero_income():
    r = estimate_income_tax(0, 2026)
    assert r["total_tax"] == 0.0
    assert r["net_income"] == 0.0
    assert r["effective_rate_pct"] == 0.0


def test_tax_negative_income_treated_as_zero():
    r = estimate_income_tax(-5000, 2026)
    assert r["total_tax"] == 0.0


def test_tax_below_tax_free_threshold():
    # $18,200 — no tax
    r = estimate_income_tax(18_200, 2026)
    assert r["gross_tax"] == 0.0
    assert r["total_tax"] == 0.0


def test_tax_first_bracket_lito_wipes_tax():
    # $20,000: gross = (20000-18200)*0.19 = $342, LITO = $700 → tax_after_lito = 0
    # Medicare: 20000 < 23365 → $0
    r = estimate_income_tax(20_000, 2026)
    assert r["gross_tax"] == pytest.approx(342.0, abs=1)
    assert r["lito"] == 700.0
    assert r["tax_after_lito"] == 0.0
    assert r["medicare_levy"] == 0.0
    assert r["total_tax"] == 0.0


def test_tax_first_bracket_with_medicare():
    # $30,000: gross = 11800*0.19 = $2242, LITO $700, after_lito $1542, medicare $600
    r = estimate_income_tax(30_000, 2026)
    assert r["gross_tax"] == pytest.approx(2242.0, abs=1)
    assert r["lito"] == 700.0
    assert r["tax_after_lito"] == pytest.approx(1542.0, abs=1)
    assert r["medicare_levy"] == pytest.approx(600.0, abs=1)
    assert r["total_tax"] == pytest.approx(2142.0, abs=1)
    assert r["net_income"] == pytest.approx(27_858.0, abs=1)


def test_tax_second_bracket_2026():
    # $80,000 (FY2026, Stage 3 brackets): 5092 + (80000-45000)*0.325 = 5092+11375 = $16467
    # LITO = 0 (> $66,667), Medicare = $80,000*0.02 = $1600
    r = estimate_income_tax(80_000, 2026)
    assert r["gross_tax"] == pytest.approx(16_467.0, abs=1)
    assert r["lito"] == 0.0
    assert r["medicare_levy"] == pytest.approx(1600.0, abs=1)
    assert r["total_tax"] == pytest.approx(18_067.0, abs=1)


def test_tax_third_bracket_2026():
    # $150,000: 34342 + (150000-135000)*0.37 = 34342 + 5550 = $39892
    r = estimate_income_tax(150_000, 2026)
    assert r["gross_tax"] == pytest.approx(39_892.0, abs=1)
    assert r["medicare_levy"] == pytest.approx(3000.0, abs=1)


def test_tax_top_bracket_2026():
    # $250,000: 54692 + (250000-190000)*0.45 = 54692 + 27000 = $81692
    r = estimate_income_tax(250_000, 2026)
    assert r["gross_tax"] == pytest.approx(81_692.0, abs=1)


def test_tax_pre_2025_bracket():
    # FY2024 — old brackets: $120,001–$180,000 is 37c bracket
    # $150,000: 29467 + (150000-120000)*0.37 = 29467 + 11100 = $40567
    r = estimate_income_tax(150_000, 2024)
    assert r["gross_tax"] == pytest.approx(40_567.0, abs=1)


def test_tax_effective_rate_populated():
    r = estimate_income_tax(80_000, 2026)
    assert 0 < r["effective_rate_pct"] < 100


def test_tax_net_income_is_income_minus_tax():
    r = estimate_income_tax(80_000, 2026)
    assert r["net_income"] == pytest.approx(80_000 - r["total_tax"], abs=0.01)


# ── estimate_hecs_repayment ────────────────────────────────────────────────────

def test_hecs_below_threshold_returns_none():
    assert estimate_hecs_repayment(50_000, 2026) is None


def test_hecs_at_minimum_threshold():
    # $55,000 — first bracket 1%
    r = estimate_hecs_repayment(55_000, 2026)
    assert r is not None
    assert r["rate_pct"] == 1.0
    assert r["repayment"] == pytest.approx(550.0, abs=1)


def test_hecs_mid_bracket():
    # $120,000 — falls in 6.0% bracket (117,357–128,961)
    r = estimate_hecs_repayment(120_000, 2026)
    assert r is not None
    assert r["rate_pct"] == 6.0
    assert r["repayment"] == pytest.approx(7200.0, abs=1)


def test_hecs_high_income():
    # $200,000 — 10% bracket
    r = estimate_hecs_repayment(200_000, 2026)
    assert r is not None
    assert r["rate_pct"] == 10.0
    assert r["repayment"] == pytest.approx(20_000.0, abs=1)


# ── gross_up_dividend ──────────────────────────────────────────────────────────

def test_gross_up_fully_franked():
    # $700 cash, 100% franked at 30% rate → credit = $700 × 30/70 = $300, grossed = $1000
    r = gross_up_dividend(700.0, 100.0, 30.0)
    assert r["cash_dividend"] == 700.0
    assert r["franking_credit"] == pytest.approx(300.0, abs=0.01)
    assert r["grossed_up"] == pytest.approx(1000.0, abs=0.01)


def test_gross_up_unfranked():
    r = gross_up_dividend(1000.0, 0.0, 30.0)
    assert r["franking_credit"] == 0.0
    assert r["grossed_up"] == 1000.0


def test_gross_up_partial_franking():
    # 50% franked $700: credit = $700 × 0.5 × 30/70 = $150
    r = gross_up_dividend(700.0, 50.0, 30.0)
    assert r["franking_credit"] == pytest.approx(150.0, abs=0.01)
    assert r["grossed_up"] == pytest.approx(850.0, abs=0.01)


def test_gross_up_zero_dividend():
    r = gross_up_dividend(0.0)
    assert r["franking_credit"] == 0.0
    assert r["grossed_up"] == 0.0
