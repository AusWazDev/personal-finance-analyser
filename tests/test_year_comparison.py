"""Tests for the /year-comparison route — data computation and page rendering."""
import json
import sqlite3

import pandas as pd
import pytest

import server
import src.db as _db_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with an in-memory DB seeded with known transactions."""
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    (data_dir / "modules.json").write_text('{"modules": {}}', encoding="utf-8")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)

    def _db(_config):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        rows = [
            # Jun 2025 — this year (period_param=2025-06)
            ("T1", "2025-06-05", -120.0, "ANZ", "Groceries"),
            ("T2", "2025-06-12", -80.0,  "ANZ", "Transport"),
            # Jun 2024 — last year
            ("T3", "2024-06-10", -100.0, "ANZ", "Groceries"),
            ("T4", "2024-06-20", -60.0,  "ANZ", "Dining Out"),
            # Transfer — must be excluded from spend comparison
            ("T5", "2025-06-15", 500.0,  "ANZ", "Transfers"),
            # May 2025 — different month, should not appear in Jun comparison
            ("T6", "2025-05-01", -50.0,  "ANZ", "Groceries"),
        ]
        conn.executemany(
            "INSERT INTO transactions (txn_id, date, amount, account, category) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        return conn

    monkeypatch.setattr(_db_mod, "get_db", _db)
    monkeypatch.setattr(server, "get_db", _db)
    monkeypatch.setattr(_db_mod, "load_transactions", lambda config, **kw: _load_with_filter(_db(config), **kw))

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def _load_with_filter(conn, since=None, until=None, include_split_parents=False):
    """Minimal load_transactions replacement that respects since/until."""
    where = []
    params = []
    if not include_split_parents:
        where.append("(is_split_parent = 0 OR is_split_parent IS NULL)")
    if since:
        where.append("date >= ?")
        params.append(since)
    if until:
        where.append("date <= ?")
        params.append(until)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return pd.read_sql_query(
        f"SELECT * FROM transactions {clause} ORDER BY date ASC",
        conn,
        params=params or None,
    )


# ── Route registration ─────────────────────────────────────────────────────────

def test_year_comparison_route_registered():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    assert "/year-comparison" in rules


# ── Month view ─────────────────────────────────────────────────────────────────

def test_month_view_returns_200(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    assert r.status_code == 200


def test_month_view_shows_period_labels(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    assert b"June 2025" in r.data
    assert b"June 2024" in r.data


def test_month_view_category_totals_correct(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    # Groceries this period = $120, last period = $100
    assert b"120" in r.data
    assert b"100" in r.data


def test_month_view_excludes_transfers(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    # Transfers category excluded from spend; total = $200 (120 Groceries + 80 Transport).
    # If Transfer were included it would be $700 — check $200 appears and $700 does not.
    assert b"$200" in r.data
    assert b"$700" not in r.data


def test_month_view_excludes_other_months(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    # T6 is May 2025 ($50 Groceries) — must not inflate June total
    # Jun 2025 Groceries should be exactly 120, not 170
    assert b"170" not in r.data


def test_month_view_chart_json_present(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    html = r.data.decode("utf-8")
    assert "xlabels" in html
    assert "this_vals" in html
    assert "prev_vals" in html


def test_month_view_navigation_links(client):
    r = client.get("/year-comparison?view=month&period=2025-06")
    assert b"period=2025-05" in r.data   # prev link
    assert b"period=2025-07" in r.data   # next link


def test_month_view_january_navigation(client):
    """January prev-month nav should go to December of prior year."""
    r = client.get("/year-comparison?view=month&period=2025-01")
    assert r.status_code == 200
    assert b"period=2024-12" in r.data


# ── FY view ────────────────────────────────────────────────────────────────────

def test_fy_view_returns_200(client):
    r = client.get("/year-comparison?view=fy&period=2024")
    assert r.status_code == 200


def test_fy_view_shows_fy_labels(client):
    r = client.get("/year-comparison?view=fy&period=2024")
    assert b"FY2025" in r.data
    assert b"FY2024" in r.data


def test_fy_view_navigation_links(client):
    r = client.get("/year-comparison?view=fy&period=2024")
    assert b"period=2023" in r.data   # prev FY
    assert b"period=2025" in r.data   # next FY


# ── View toggle in NAV_TABS ───────────────────────────────────────────────────

def test_year_on_year_in_nav_tabs():
    from src.utils import NAV_TABS
    routes = [t[1] for t in NAV_TABS]
    assert "/year-comparison" in routes


# ── Empty data ────────────────────────────────────────────────────────────────

def test_empty_period_shows_empty_message(tmp_path, monkeypatch):
    """A period with no transactions must render gracefully."""
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    (data_dir / "modules.json").write_text('{"modules": {}}', encoding="utf-8")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)

    def _empty_db(_config):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        return conn

    monkeypatch.setattr(_db_mod, "get_db", _empty_db)
    monkeypatch.setattr(server, "get_db", _empty_db)
    monkeypatch.setattr(_db_mod, "load_transactions", lambda config, **kw: pd.DataFrame())

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/year-comparison?view=month&period=2025-06")
    assert r.status_code == 200
    assert b"No spending data" in r.data
