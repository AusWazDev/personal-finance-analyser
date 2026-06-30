"""Tests for project_net_worth() in src/manual_assets.py."""
import pytest
from src.manual_assets import project_net_worth


def _empty_manual():
    return {"assets": [], "liabilities": []}


# ── Basic structure ────────────────────────────────────────────────────────────

def test_projection_returns_correct_keys():
    result = project_net_worth(100_000, _empty_manual(), years=5)
    assert set(result.keys()) == {"labels", "pessimistic", "base", "optimistic"}


def test_projection_label_count():
    result = project_net_worth(100_000, _empty_manual(), years=10)
    assert len(result["labels"]) == 11  # 10 + starting year


def test_first_value_equals_current():
    nw = 250_000.0
    result = project_net_worth(nw, _empty_manual(), years=5)
    assert result["base"][0] == pytest.approx(nw, rel=0.01)
    assert result["pessimistic"][0] == pytest.approx(nw, rel=0.01)
    assert result["optimistic"][0] == pytest.approx(nw, rel=0.01)


def test_higher_rate_produces_higher_values():
    result = project_net_worth(100_000, _empty_manual(), years=5)
    assert result["optimistic"][-1] > result["base"][-1] > result["pessimistic"][-1]


def test_growth_over_time():
    result = project_net_worth(100_000, _empty_manual(), years=5)
    # Base (6%) should grow — each year > previous
    base = result["base"]
    assert all(base[i + 1] >= base[i] for i in range(len(base) - 1))


def test_monthly_savings_increases_projection():
    r_no_save = project_net_worth(100_000, _empty_manual(), years=5, monthly_savings=0)
    r_save    = project_net_worth(100_000, _empty_manual(), years=5, monthly_savings=1000)
    assert r_save["base"][-1] > r_no_save["base"][-1]


def test_liabilities_amortised():
    """With a liability the initial position should still be current_net_worth."""
    manual = {
        "assets": [],
        "liabilities": [{"liability_id": "L1", "type": "mortgage",
                          "snapshots": [{"date": "2026-01-01", "balance": 400_000}]}],
    }
    result = project_net_worth(-100_000, manual, years=3)
    # Starting point reflects negative net worth
    assert result["base"][0] < 0


def test_labels_are_years_from_today():
    from datetime import date
    result = project_net_worth(50_000, _empty_manual(), years=3)
    current_year = str(date.today().year)
    assert result["labels"][0] == current_year


def test_zero_net_worth_does_not_crash():
    result = project_net_worth(0, _empty_manual(), years=5)
    assert len(result["base"]) == 6


def test_years_param_respected():
    result = project_net_worth(100_000, _empty_manual(), years=7)
    assert len(result["labels"]) == 8
