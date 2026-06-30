"""Manual assets and liabilities for net worth tracking.

Assets: property, super, vehicle, investment, other.
Liabilities: mortgage, hecs, car_finance, credit_card, other.

Both stored in Data/manual_assets.json with a snapshot history per entry
so net worth can be trended over time.
"""
import json
import uuid
from datetime import date
from pathlib import Path

_DEFAULT_FILE = "Data/manual_assets.json"

ASSET_TYPES = ["property", "super", "vehicle", "investment", "other"]
LIABILITY_TYPES = ["mortgage", "hecs", "car_finance", "credit_card", "other"]

ASSET_TYPE_LABELS = {
    "property": "Property",
    "super": "Superannuation",
    "vehicle": "Vehicle",
    "investment": "Investment",
    "other": "Other",
}
LIABILITY_TYPE_LABELS = {
    "mortgage": "Mortgage",
    "hecs": "HECS / HELP",
    "car_finance": "Car Finance",
    "credit_card": "Credit Card",
    "other": "Other",
}


def load_manual_assets(config: dict) -> dict:
    path = Path(config.get("data", {}).get("manual_assets_file", _DEFAULT_FILE))
    try:
        data = json.loads(path.read_text("utf-8"))
        return {
            "assets": data.get("assets", []),
            "liabilities": data.get("liabilities", []),
        }
    except Exception:
        return {"assets": [], "liabilities": []}


def save_manual_assets(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("manual_assets_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), "utf-8")


def new_asset_id() -> str:
    return "A" + uuid.uuid4().hex[:10].upper()


def new_liability_id() -> str:
    return "L" + uuid.uuid4().hex[:10].upper()


def latest_value(asset: dict) -> float:
    """Return the most recent snapshot value for an asset (0 if none)."""
    snaps = sorted(asset.get("snapshots", []), key=lambda s: s.get("date", ""))
    return float(snaps[-1]["value"]) if snaps else 0.0


def latest_liability_balance(liability: dict) -> float:
    """Return the most recent snapshot balance for a liability (0 if none)."""
    snaps = sorted(liability.get("snapshots", []), key=lambda s: s.get("date", ""))
    return float(snaps[-1]["balance"]) if snaps else 0.0


def total_assets_value(data: dict) -> float:
    return round(sum(latest_value(a) for a in data.get("assets", [])), 2)


def total_liabilities_balance(data: dict) -> float:
    return round(sum(latest_liability_balance(l) for l in data.get("liabilities", [])), 2)


def super_projected_balance(asset: dict) -> float | None:
    """Project super balance at retirement using compound interest + optional annual contribution.

    Returns None if insufficient data (no snapshots, or would require negative years).
    """
    current = latest_value(asset)
    if current <= 0:
        return None

    expected_return_pct = float(asset.get("expected_return_pct") or 7.0)
    r = expected_return_pct / 100
    retirement_age = int(asset.get("retirement_age") or 67)
    birth_year = asset.get("birth_year")

    if birth_year:
        years = retirement_age - (date.today().year - int(birth_year))
    else:
        years = int(asset.get("years_to_retire") or 25)

    if years <= 0:
        return round(current, 2)

    annual_contribution = float(asset.get("annual_contribution") or 0)
    fv_current = current * (1 + r) ** years
    if annual_contribution > 0 and r > 0:
        fv_contributions = annual_contribution * ((1 + r) ** years - 1) / r
    else:
        fv_contributions = annual_contribution * years
    return round(fv_current + fv_contributions, 2)


def compute_net_worth_history(balances_df, manual_data: dict):
    """Return monthly net worth history DataFrame [date, bank, manual, liabilities, net_worth].

    balances_df has columns: date (Timestamp), account (str), balance (float).
    Returns empty DataFrame if no data at all.
    """
    import pandas as pd

    start_dates = []
    if balances_df is not None and not balances_df.empty:
        start_dates.append(balances_df["date"].min())

    for asset in manual_data.get("assets", []):
        for s in asset.get("snapshots", []):
            try:
                start_dates.append(pd.Timestamp(s["date"]))
            except Exception:
                pass
    for liab in manual_data.get("liabilities", []):
        for s in liab.get("snapshots", []):
            try:
                start_dates.append(pd.Timestamp(s["date"]))
            except Exception:
                pass

    if not start_dates:
        return pd.DataFrame()

    # Use the latest of today or the latest data point so future-dated snapshots are included
    end_dates = [pd.Timestamp.now()]
    end_dates.extend(start_dates)
    if balances_df is not None and not balances_df.empty:
        end_dates.append(balances_df["date"].max())
    end = max(end_dates)
    months = pd.date_range(min(start_dates), end + pd.offsets.MonthEnd(0), freq="ME")
    rows = []
    for m in months:
        # Bank accounts
        bank = 0.0
        if balances_df is not None and not balances_df.empty:
            eligible = balances_df[balances_df["date"] <= m]
            if not eligible.empty:
                bank = float(eligible.sort_values("date").groupby("account")["balance"].last().sum())

        # Manual assets (LOCF per asset)
        manual = 0.0
        for asset in manual_data.get("assets", []):
            snaps = [(s["date"], float(s["value"])) for s in asset.get("snapshots", [])
                     if pd.Timestamp(s["date"]) <= m]
            if snaps:
                manual += max(snaps, key=lambda x: x[0])[1]

        # Liabilities (LOCF per liability)
        liabs = 0.0
        for liab in manual_data.get("liabilities", []):
            snaps = [(s["date"], float(s["balance"])) for s in liab.get("snapshots", [])
                     if pd.Timestamp(s["date"]) <= m]
            if snaps:
                liabs += max(snaps, key=lambda x: x[0])[1]

        rows.append({
            "date": m,
            "bank": round(bank, 2),
            "manual": round(manual, 2),
            "liabilities": round(liabs, 2),
            "net_worth": round(bank + manual - liabs, 2),
        })
    return pd.DataFrame(rows)


def project_net_worth(
    current_net_worth: float,
    manual_data: dict,
    years: int = 10,
    monthly_savings: float = 0.0,
) -> dict:
    """Project net worth over `years` at 3 growth scenarios.

    Uses per-asset expected_return_pct where set; falls back to defaults:
      - Pessimistic: 3% p.a. (cash/bonds)
      - Base:        6% p.a. (balanced portfolio)
      - Optimistic:  9% p.a. (growth portfolio)

    Liabilities are reduced at a constant monthly rate (sum of latest liability
    balances divided by a 25-year horizon).

    Returns dict with keys "labels", "pessimistic", "base", "optimistic"
    where each scenario is a list of yearly net worth values (len = years + 1,
    first element = current year).
    """
    total_assets = total_assets_value(manual_data)
    total_liabs  = total_liabilities_balance(manual_data)

    # Estimate implied annual savings contribution from monthly_savings
    annual_savings = monthly_savings * 12

    # Monthly liability reduction rate (amortise total over 25 years if no info)
    monthly_liability_reduction = total_liabs / (25 * 12) if total_liabs > 0 else 0

    def _project(annual_rate: float) -> list[float]:
        assets = float(total_assets + max(current_net_worth - total_assets + total_liabs, 0))
        liabs  = float(total_liabs)
        result = []
        r_monthly = annual_rate / 12
        for yr in range(years + 1):
            result.append(round(assets - liabs, 0))
            if yr < years:
                for _ in range(12):
                    assets  = assets * (1 + r_monthly) + (annual_savings / 12)
                    liabs   = max(liabs - monthly_liability_reduction, 0)
        return result

    current_year = date.today().year
    labels = [str(current_year + i) for i in range(years + 1)]

    return {
        "labels":      labels,
        "pessimistic": _project(0.03),
        "base":        _project(0.06),
        "optimistic":  _project(0.09),
    }
