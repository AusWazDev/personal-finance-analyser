"""Investment portfolio tracking — purchase lots, holdings summary, optional live prices."""
import json
import uuid
from pathlib import Path

_DEFAULT_FILE = "Data/portfolio.json"


def load_portfolio(config: dict) -> dict:
    path = Path(config.get("data", {}).get("portfolio_file", _DEFAULT_FILE))
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {"lots": []}


def save_portfolio(data: dict, config: dict) -> None:
    path = Path(config.get("data", {}).get("portfolio_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), "utf-8")


def new_lot_id() -> str:
    return "P" + uuid.uuid4().hex[:10].upper()


def holdings_summary(lots: list[dict], prices: dict | None = None) -> list[dict]:
    """Aggregate lots by ticker and return a list of holding dicts, sorted by cost basis desc."""
    from collections import defaultdict
    from datetime import date as _date_cls

    agg: dict = defaultdict(lambda: {
        "ticker": "", "name": "", "units": 0.0, "cost_basis": 0.0, "lots": 0,
        "earliest_date": None,
    })
    for lot in lots:
        ticker = (lot.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        h = agg[ticker]
        h["ticker"] = ticker
        h["name"] = lot.get("name") or ticker
        units = float(lot.get("units", 0) or 0)
        cost = float(lot.get("cost_per_unit", 0) or 0)
        h["units"] = round(h["units"] + units, 6)
        h["cost_basis"] = round(h["cost_basis"] + units * cost, 2)
        h["lots"] += 1
        lot_date = lot.get("date")
        if lot_date:
            if h["earliest_date"] is None or lot_date < h["earliest_date"]:
                h["earliest_date"] = lot_date

    today = _date_cls.today()
    prices = prices or {}
    result = []
    for ticker, h in agg.items():
        avg_cost = h["cost_basis"] / h["units"] if h["units"] else 0
        h["avg_cost"] = round(avg_cost, 4)
        price = prices.get(ticker)
        if price is not None and h["units"] > 0:
            h["current_price"] = round(price, 4)
            h["current_value"] = round(price * h["units"], 2)
            h["unrealised_pl"] = round(h["current_value"] - h["cost_basis"], 2)
            h["pl_pct"] = round(h["unrealised_pl"] / h["cost_basis"] * 100, 2) if h["cost_basis"] else 0.0
            # Annualised return (CAGR): (current/cost)^(1/years) - 1
            if h["earliest_date"] and h["cost_basis"] > 0:
                try:
                    days = (today - _date_cls.fromisoformat(str(h["earliest_date"]))).days
                    years = days / 365.25
                    if years >= 0.25:  # suppress for < 3 months (CAGR is meaningless short-term)
                        ratio = h["current_value"] / h["cost_basis"]
                        h["cagr"] = round((ratio ** (1 / years) - 1) * 100, 1)
                    else:
                        h["cagr"] = None
                except Exception:
                    h["cagr"] = None
            else:
                h["cagr"] = None
        else:
            h["current_price"] = None
            h["current_value"] = None
            h["unrealised_pl"] = None
            h["pl_pct"] = None
            h["cagr"] = None
        result.append(h)

    return sorted(result, key=lambda h: -(h["cost_basis"]))


def fetch_current_price(ticker: str) -> float | None:
    """Fetch current market price from Yahoo Finance (no API key required)."""
    import json as _json
    import urllib.request

    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?interval=1d&range=1d"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch prices for multiple tickers; silently skips failures."""
    result = {}
    for t in tickers:
        p = fetch_current_price(t)
        if p is not None:
            result[t] = p
    return result
