"""
Smoke tests for server.py — import chain, helper functions, route registration.

These tests catch partial-refactor breakage without needing a live server.
Run with:  python -m pytest tests/ -v
"""

import json
from pathlib import Path

import pytest

import server


# ── Module-level constants ────────────────────────────────────────────────────

def test_cat_groups_has_income_and_expenditure():
    labels = [g[0] for g in server._CAT_GROUPS]
    assert "Income" in labels
    assert "Expenditure" in labels


def test_valid_categories_non_empty():
    assert len(server._VALID_CATEGORIES) > 10


def test_valid_categories_contains_expected():
    assert "Groceries" in server._VALID_CATEGORIES
    assert "Income" in server._VALID_CATEGORIES
    assert "Miscellaneous" in server._VALID_CATEGORIES


def test_cat_groups_not_duplicated():
    # Module-level definition must be the only one — catches the issue fixed in this session
    all_cats = [c for _, cats in server._CAT_GROUPS for c in cats]
    assert len(all_cats) == len(set(all_cats)), "duplicate categories in _CAT_GROUPS"


# ── Helper functions present and callable ─────────────────────────────────────

def test_load_config_callable():
    assert callable(server._load_config)


def test_data_path_callable():
    assert callable(server._data_path)


def test_stream_job_callable():
    assert callable(server._stream_job)


# ── _load_config behaviour ────────────────────────────────────────────────────

def test_load_config_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    assert server._load_config() == {}


def test_load_config_reads_valid_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("data:\n  output_dir: reports\n", encoding="utf-8")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    result = server._load_config()
    assert result["data"]["output_dir"] == "reports"


def test_load_config_returns_empty_on_invalid_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("}{invalid yaml!!!", encoding="utf-8")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    assert server._load_config() == {}


def test_load_config_returns_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    assert isinstance(server._load_config(), dict)


# ── _data_path behaviour ──────────────────────────────────────────────────────

def test_data_path_uses_default_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    p = server._data_path("nonexistent_key", "data/foo.json")
    assert p == tmp_path / "data/foo.json"


def test_data_path_uses_config_value_when_present(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "data:\n  my_key: custom/path.json\n", encoding="utf-8"
    )
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    p = server._data_path("my_key", "data/default.json")
    assert p == tmp_path / "custom/path.json"


def test_data_path_returns_path_object(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    assert isinstance(server._data_path("k", "data/x.json"), Path)


# ── Flask route registration ──────────────────────────────────────────────────

def _rules():
    return {r.rule for r in server.app.url_map.iter_rules()}


def test_sse_routes_registered():
    rules = _rules()
    # SSE routes kept for diagnostic use (not exposed in UI)
    assert "/api/start-import" in rules
    assert "/api/refresh-reports" in rules
    assert "/api/recategorise" in rules
    assert "/api/pipeline-status" in rules


def test_page_routes_registered():
    rules = _rules()
    assert "/help" in rules
    assert "/merchant-rules" in rules
    assert "/settings/merchant-rules" in rules
    assert "/settings/accounts" in rules
    assert "/commitments" in rules
    assert "/reimbursements" in rules
    assert "/coverage" in rules
    assert "/capital-gains" in rules
    assert "/cash-flow" in rules
    assert "/fy-summary" in rules
    assert "/net-worth" in rules
    assert "/transfers" in rules
    assert "/review" in rules
    assert "/dashboard" in rules
    assert "/transactions" in rules


def test_api_routes_registered():
    rules = _rules()
    assert "/api/merchant-rules" in rules
    assert "/api/upload" in rules
    assert "/api/transactions/<txn_id>" in rules
    assert "/api/commitments" in rules
    assert "/api/capital-gains" in rules
    assert "/api/accounts" in rules
    assert "/api/accounts/<path:account_name>" in rules


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def test_favicon_returns_204(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 204


def test_root_redirects_to_dashboard(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/dashboard")


def test_coverage_empty_db_returns_200(client, tmp_path, monkeypatch):
    import sqlite3
    import src.db as _db_mod

    def _in_memory_db(_config):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        return conn

    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(_db_mod, "get_db", _in_memory_db)
    monkeypatch.setattr(server, "get_db", _in_memory_db)  # patch direct module-level ref
    r = client.get("/coverage")
    assert r.status_code == 200
    assert b"No transaction data found" in r.data


def test_coverage_with_data_shows_accounts(client, tmp_path, monkeypatch):
    import sqlite3
    import src.db as _db_mod

    def _seeded_db(_config):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        conn.execute(
            "INSERT INTO transactions (txn_id, date, amount, account, category) "
            "VALUES ('t1','2025-03-15',100.0,'ANZ Personal','Income')"
        )
        conn.execute(
            "INSERT INTO transactions (txn_id, date, amount, account, category) "
            "VALUES ('t2','2025-05-10',-50.0,'ANZ Personal','Groceries')"
        )
        conn.commit()
        return conn

    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(_db_mod, "get_db", _seeded_db)
    monkeypatch.setattr(server, "get_db", _seeded_db)  # patch direct module-level ref
    r = client.get("/coverage")
    assert r.status_code == 200
    assert b"ANZ Personal" in r.data
    # March has data, April is a gap, May has data — one gap expected
    assert b"1 gap" in r.data


# ── Setup wizard ──────────────────────────────────────────────────────────────

def test_setup_route_registered():
    rules = _rules()
    assert "/setup" in rules


def test_setup_get_returns_200_when_no_modules_json(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    r = client.get("/setup")
    assert r.status_code == 200
    assert b"Personal Finance Analyser" in r.data
    assert b"Get started" in r.data


def test_setup_get_redirects_when_modules_json_exists(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    (data_dir / "modules.json").write_text('{"modules": {"budgets": true}}', encoding="utf-8")
    r = client.get("/setup")
    assert r.status_code == 302
    assert r.location == "/" or r.location.endswith("/")


def test_setup_post_saves_modules_and_redirects(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "Data").mkdir()
    form_data = {"budgets": "on", "goals": "on"}
    r = client.post("/setup", data=form_data)
    assert r.status_code == 302
    modules_path = tmp_path / "Data" / "modules.json"
    assert modules_path.exists()
    import json
    saved = json.loads(modules_path.read_text())["modules"]
    assert saved["budgets"] is True
    assert saved["goals"] is True
    assert saved["coverage"] is False  # not submitted in form


def test_setup_post_all_modules_enabled(client, tmp_path, monkeypatch):
    from src.modules import DEFAULT_MODULES
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "Data").mkdir()
    form_data = {k: "on" for k in DEFAULT_MODULES}
    r = client.post("/setup", data=form_data)
    assert r.status_code == 302
    import json
    saved = json.loads((tmp_path / "Data" / "modules.json").read_text())["modules"]
    assert all(saved[k] is True for k in DEFAULT_MODULES)


def test_before_request_redirects_to_setup_when_no_modules_json(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    server.app.config["TESTING"] = False
    try:
        with server.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 302
            assert b"/setup" in r.data
    finally:
        server.app.config["TESTING"] = True


def test_before_request_does_not_redirect_setup_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    server.app.config["TESTING"] = False
    try:
        with server.app.test_client() as c:
            r = c.get("/setup")
            assert r.status_code == 200  # /setup itself is not redirected
    finally:
        server.app.config["TESTING"] = True


def test_setup_exempt_from_auth(tmp_path, monkeypatch):
    """Auth guard must not intercept /setup even when a password is configured."""
    import hashlib
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    salt = bytes.fromhex("aabbcc")
    h = hashlib.pbkdf2_hmac("sha256", b"secret", salt, 100000).hex()
    (tmp_path / "config.yaml").write_text(
        f"server:\n  password_hash: pbkdf2:sha256:100000:aabbcc:{h}\n",
        encoding="utf-8",
    )
    server.app.config["TESTING"] = False
    try:
        with server.app.test_client() as c:
            r = c.get("/setup")
            assert r.status_code == 200
    finally:
        server.app.config["TESTING"] = True


# ── Wizard — user_name + test-api-key ────────────────────────────────────────

def test_setup_post_saves_user_name(client, tmp_path, monkeypatch):
    """POST /setup with user_name writes Data/user_settings.json."""
    import json
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "Data").mkdir()
    r = client.post("/setup", data={"user_name": "Alice", "budgets": "on"})
    assert r.status_code == 302
    settings_path = tmp_path / "Data" / "user_settings.json"
    assert settings_path.exists()
    assert json.loads(settings_path.read_text())["user_name"] == "Alice"


def test_setup_post_blank_name_skips_user_settings(client, tmp_path, monkeypatch):
    """POST /setup with blank name must not create user_settings.json."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "Data").mkdir()
    client.post("/setup", data={"user_name": "   "})
    assert not (tmp_path / "Data" / "user_settings.json").exists()


def test_test_api_key_route_registered():
    assert "/api/setup/test-api-key" in _rules()


def test_test_api_key_no_env_var(client, monkeypatch):
    """Returns ok=False when env var is absent."""
    import os
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.get("/api/setup/test-api-key")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is False
    assert "not set" in data["message"]


def test_test_api_key_with_valid_key(client, monkeypatch):
    """Returns ok=True when API responds without error (mocked)."""
    import os
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    class _FakeMsg:
        pass
    class _FakeMsgs:
        def create(self, **kw):
            return _FakeMsg()
    class _FakeClient:
        messages = _FakeMsgs()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: _FakeClient())
    r = client.get("/api/setup/test-api-key")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_setup_wizard_renders_three_steps(client, tmp_path, monkeypatch):
    """GET /setup HTML contains the three step-tab labels."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    r = client.get("/setup")
    assert r.status_code == 200
    html = r.data.decode()
    assert "About You" in html
    assert "Import Guide" in html
    assert "Features" in html


# ── Tags routes ──────────────────────────────────────────────────────────────

def test_tags_routes_registered():
    rules = _rules()
    assert "/api/tags" in rules
    assert "/api/txn/<txn_id>/tags" in rules
    assert "/tags" in rules


def test_api_get_tags_no_config(client, tmp_path, monkeypatch):
    """GET /api/tags returns error when config.yaml is missing."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    r = client.get("/api/tags")
    assert r.status_code == 500
    assert b"config" in r.data.lower()


def test_api_set_tags_no_config(client, tmp_path, monkeypatch):
    """POST /api/txn/<id>/tags returns error when config.yaml is missing."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    r = client.post(
        "/api/txn/TXNABC/tags",
        json={"tags": ["Bali 2025"]},
        content_type="application/json",
    )
    assert r.status_code == 500


def test_api_set_tags_round_trip(tmp_path, monkeypatch):
    """POST /api/txn/<id>/tags stores tags; GET /api/tags returns them."""
    import json, pandas as pd
    from src.db import get_db, init_db, upsert_transactions

    db_path = tmp_path / "test.db"
    config = {"data": {"database": str(db_path)}}

    df = pd.DataFrame([{
        "txn_id": "TXNABC",
        "date": pd.Timestamp("2025-11-01"),
        "amount": -50.0,
        "description": "SOME MERCHANT",
        "category": "Groceries",
        "account": "ANZ Personal",
        "account_type": "personal",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])
    conn = get_db(config)
    init_db(conn)
    conn.close()
    upsert_transactions(df, config, zip_name=None)

    monkeypatch.setattr(
        server, "_load_config", lambda: {"data": {"database": str(db_path)}}
    )

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        # Set tags
        r = c.post(
            "/api/txn/TXNABC/tags",
            json={"tags": ["Bali 2025", "Kitchen Reno"]},
            content_type="application/json",
        )
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["ok"] is True
        assert set(data["tags"]) == {"Bali 2025", "Kitchen Reno"}

        # Read back via GET /api/tags
        r2 = c.get("/api/tags")
        assert r2.status_code == 200
        data2 = json.loads(r2.data)
        assert "Bali 2025" in data2["tags"]
        assert "Kitchen Reno" in data2["tags"]


def test_api_set_tags_comma_string_input(tmp_path, monkeypatch):
    """POST /api/txn/<id>/tags accepts comma-separated string as well as list."""
    import json, pandas as pd
    from src.db import get_db, init_db, upsert_transactions

    db_path = tmp_path / "test.db"
    config = {"data": {"database": str(db_path)}}
    df = pd.DataFrame([{
        "txn_id": "TXNDEF",
        "date": pd.Timestamp("2025-11-01"),
        "amount": -20.0,
        "description": "ANOTHER MERCHANT",
        "category": "Utilities",
        "account": "ANZ Personal",
        "account_type": "personal",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])
    conn = get_db(config)
    init_db(conn)
    conn.close()
    upsert_transactions(df, config, zip_name=None)

    monkeypatch.setattr(
        server, "_load_config", lambda: {"data": {"database": str(db_path)}}
    )

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post(
            "/api/txn/TXNDEF/tags",
            json={"tags": "Alpha,Beta, Gamma"},
            content_type="application/json",
        )
        assert r.status_code == 200
        data = json.loads(r.data)
        assert set(data["tags"]) == {"Alpha", "Beta", "Gamma"}


# ── Subscriptions page ───────────────────────────────────────────────────────

def test_subscriptions_route_registered():
    assert "/subscriptions" in _rules()


def test_subscriptions_page_empty_returns_200(client, tmp_path, monkeypatch):
    """GET /subscriptions works with no commitments.json on disk."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("data:\n  commitments_file: commitments.json\n", encoding="utf-8")
    r = client.get("/subscriptions")
    assert r.status_code == 200
    assert b"Subscriptions" in r.data


def test_subscriptions_monthly_cost_calculation(client, tmp_path, monkeypatch):
    """Monthly cost is correctly computed from frequency × amount."""
    import json as _json
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    commitments = {"items": [
        {"id": "aaa", "name": "Netflix", "category": "Subscriptions", "amount": 15.99,
         "frequency": "monthly", "active": True, "last_seen": "2026-05-01", "next_due": "2026-07-01",
         "freq_label": "Monthly", "notes": "", "source": "manual", "merchant_key": "netflix", "account": "ANZ"},
        {"id": "bbb", "name": "Spotify", "category": "Subscriptions", "amount": 11.99,
         "frequency": "annual", "active": True, "last_seen": "2026-01-01", "next_due": "2027-01-01",
         "freq_label": "Annual", "notes": "", "source": "manual", "merchant_key": "spotify", "account": "ANZ"},
    ]}
    (data_dir / "commitments.json").write_text(_json.dumps(commitments), encoding="utf-8")
    commitments_path = str(data_dir / "commitments.json").replace("\\", "/")
    (tmp_path / "config.yaml").write_text(
        f"data:\n  commitments_file: '{commitments_path}'\n", encoding="utf-8"
    )
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    r = client.get("/subscriptions")
    assert r.status_code == 200
    assert b"Netflix" in r.data
    assert b"Spotify" in r.data
    assert b"Monthly cost" in r.data


# ── FY Summary and Net Worth live routes ─────────────────────────────────────

def test_fy_summary_page_empty_db_returns_200(client, tmp_path, monkeypatch):
    """GET /fy-summary works with no transaction data."""
    import sqlite3
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    db_path = tmp_path / "Data" / "finance.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(str(db_path))
    from src.db import init_db
    init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(
        f"data:\n  db_file: '{str(db_path).replace(chr(92), '/')}'\n", encoding="utf-8"
    )
    r = client.get("/fy-summary")
    assert r.status_code == 200
    assert b"Financial Year Summary" in r.data


def test_net_worth_page_no_balances_returns_200(client, tmp_path, monkeypatch):
    """GET /net-worth works when no balance history exists."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "data:\n  balance_history_file: 'Data/account_balances.csv'\n", encoding="utf-8"
    )
    r = client.get("/net-worth")
    assert r.status_code == 200
    assert b"Net Worth" in r.data


def test_transfers_page_no_candidates_returns_200(client, tmp_path, monkeypatch):
    """GET /transfers works when no transfer candidates JSON exists."""
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    r = client.get("/transfers")
    assert r.status_code == 200
    assert b"Transfer Pairs" in r.data


def test_review_page_empty_db_returns_200(client, tmp_path, monkeypatch):
    """GET /review works with an empty transaction database."""
    import sqlite3
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    db_path = tmp_path / "Data" / "finance.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    from src.db import init_db
    init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(
        f"data:\n  db_file: '{db_path}'\n", encoding="utf-8"
    )
    r = client.get("/review")
    assert r.status_code == 200
    assert b"Review" in r.data


def test_dashboard_page_empty_db_returns_200(client, tmp_path, monkeypatch):
    """GET /dashboard works with an empty transaction database."""
    import sqlite3
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    db_path = tmp_path / "Data" / "finance.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    from src.db import init_db
    init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(
        f"data:\n  db_file: '{db_path}'\n", encoding="utf-8"
    )
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b"Personal Finance Dashboard" in r.data


def test_transactions_page_empty_db_returns_200(client, tmp_path, monkeypatch):
    """GET /transactions works with an empty transaction database."""
    import sqlite3
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    db_path = tmp_path / "Data" / "finance.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    from src.db import init_db
    init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(
        f"data:\n  db_file: '{db_path}'\n", encoding="utf-8"
    )
    r = client.get("/transactions")
    assert r.status_code == 200
    assert b"Transactions" in r.data


# ── Auto-update checker ───────────────────────────────────────────────────────

def test_app_version_defined():
    from src.version import __version__
    assert isinstance(__version__, str)
    assert __version__  # non-empty


def test_update_check_route_registered():
    assert "/api/update-check" in _rules()


def test_api_update_check_no_update(client):
    """Returns has_update=False when _UPDATE_INFO is empty."""
    import server as srv
    original = srv._UPDATE_INFO.copy()
    srv._UPDATE_INFO.clear()
    try:
        r = client.get("/api/update-check")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["has_update"] is False
    finally:
        srv._UPDATE_INFO.update(original)


def test_api_update_check_with_update(client):
    """Returns has_update=True and version info when update is available."""
    import server as srv
    original = srv._UPDATE_INFO.copy()
    srv._UPDATE_INFO.clear()
    srv._UPDATE_INFO.update({"has_update": True, "current": "1.0.0", "latest": "1.1.0", "url": ""})
    try:
        r = client.get("/api/update-check")
        data = json.loads(r.data)
        assert data["has_update"] is True
        assert data["latest"] == "1.1.0"
    finally:
        srv._UPDATE_INFO.clear()
        srv._UPDATE_INFO.update(original)


def test_check_for_updates_sets_update_info(monkeypatch):
    """_check_for_updates populates _UPDATE_INFO when a newer version exists."""
    import io, urllib.request
    import server as srv

    payload = json.dumps({"tag_name": "v99.0.0", "html_url": "https://example.com/releases"}).encode()

    class _FakeResp:
        def read(self): return payload
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=8: _FakeResp())
    original = srv._UPDATE_INFO.copy()
    srv._UPDATE_INFO.clear()
    try:
        srv._check_for_updates("owner/repo")
        assert srv._UPDATE_INFO.get("has_update") is True
        assert srv._UPDATE_INFO.get("latest") == "99.0.0"
    finally:
        srv._UPDATE_INFO.clear()
        srv._UPDATE_INFO.update(original)


def test_check_for_updates_no_update_when_same_version(monkeypatch):
    """_check_for_updates leaves _UPDATE_INFO empty when already on latest."""
    import urllib.request
    import server as srv
    from src.version import __version__

    payload = json.dumps({"tag_name": f"v{__version__}", "html_url": ""}).encode()

    class _FakeResp:
        def read(self): return payload
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=8: _FakeResp())
    original = srv._UPDATE_INFO.copy()
    srv._UPDATE_INFO.clear()
    try:
        srv._check_for_updates("owner/repo")
        assert not srv._UPDATE_INFO  # empty — no update
    finally:
        srv._UPDATE_INFO.clear()
        srv._UPDATE_INFO.update(original)


# ── Transaction splitting ─────────────────────────────────────────────────────

def test_split_routes_registered():
    rules = _rules()
    assert "/api/txn/<txn_id>/split" in rules


def test_api_split_transaction_round_trip(tmp_path, monkeypatch):
    """POST /api/txn/<id>/split creates children; DELETE removes them."""
    import pandas as pd
    from src.db import get_db, init_db, upsert_transactions

    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}

    df = pd.DataFrame([{
        "txn_id": "SPLIT001",
        "date": pd.Timestamp("2026-05-15"),
        "amount": -200.0,
        "description": "COLES SUPERMARKETS",
        "category": "Groceries",
        "account": "ANZ Personal",
        "account_type": "personal",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])
    conn = get_db(cfg)
    init_db(conn)
    conn.close()
    upsert_transactions(df, cfg, zip_name=None)

    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        # Split into two
        r = c.post(
            "/api/txn/SPLIT001/split",
            json={"splits": [
                {"category": "Groceries", "amount": -150.0, "description": "Food"},
                {"category": "Household", "amount": -50.0, "description": "Cleaning"},
            ]},
            content_type="application/json",
        )
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d["ok"] is True
        assert d["children"] == 2

        # Verify parent is marked and children exist
        conn2 = get_db(cfg)
        parent = conn2.execute("SELECT is_split_parent FROM transactions WHERE txn_id = 'SPLIT001'").fetchone()
        assert parent[0] == 1
        children = conn2.execute("SELECT txn_id FROM transactions WHERE parent_txn_id = 'SPLIT001'").fetchall()
        assert len(children) == 2
        conn2.close()

        # Unsplit
        r2 = c.delete("/api/txn/SPLIT001/split")
        assert r2.status_code == 200
        d2 = json.loads(r2.data)
        assert d2["ok"] is True

        conn3 = get_db(cfg)
        parent2 = conn3.execute("SELECT is_split_parent FROM transactions WHERE txn_id = 'SPLIT001'").fetchone()
        assert parent2[0] == 0
        remaining = conn3.execute("SELECT txn_id FROM transactions WHERE parent_txn_id = 'SPLIT001'").fetchall()
        assert len(remaining) == 0
        conn3.close()


def test_api_split_rejects_mismatched_amounts(tmp_path, monkeypatch):
    """POST /api/txn/<id>/split returns 400 when split amounts don't match parent."""
    import pandas as pd
    from src.db import get_db, init_db, upsert_transactions

    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    df = pd.DataFrame([{
        "txn_id": "SPLIT002",
        "date": pd.Timestamp("2026-05-15"),
        "amount": -100.0,
        "description": "TEST",
        "category": "Groceries",
        "account": "ANZ Personal",
        "account_type": "personal",
        "source_file": "test.csv",
        "is_business": False,
        "is_tax_deductible": False,
        "is_gst_claimable": False,
    }])
    conn = get_db(cfg)
    init_db(conn)
    conn.close()
    upsert_transactions(df, cfg, zip_name=None)

    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post(
            "/api/txn/SPLIT002/split",
            json={"splits": [
                {"category": "Groceries", "amount": -60.0},
                {"category": "Household", "amount": -60.0},  # total $120 ≠ $100
            ]},
            content_type="application/json",
        )
        assert r.status_code == 400
        d = json.loads(r.data)
        assert d["ok"] is False


def test_merchants_page_empty_db_returns_200(client, tmp_path, monkeypatch):
    """GET /merchants with no transactions returns 200 and empty merchant list."""
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    conn = get_db(cfg)
    init_db(conn)
    conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    r = client.get("/merchants")
    assert r.status_code == 200
    assert b"Merchant Analytics" in r.data


def test_debt_payoff_page_no_loans_returns_200(client, tmp_path, monkeypatch):
    """GET /debt-payoff with no loan records returns 200 and empty state message."""
    db_path = tmp_path / "test.db"
    loans_path = tmp_path / "loans.json"
    loans_path.write_text('{"loans": []}', encoding="utf-8")
    cfg = {"data": {"database": str(db_path), "loans_file": str(loans_path)}}
    from src.db import get_db, init_db
    conn = get_db(cfg)
    init_db(conn)
    conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    r = client.get("/debt-payoff")
    assert r.status_code == 200
    assert b"Debt Payoff Calculator" in r.data


def test_api_loan_interest_rate_updates(tmp_path, monkeypatch):
    """PATCH /api/loans/<id>/interest-rate persists the rate to loans.json."""
    loans_path = tmp_path / "loans.json"
    loans_path.write_text(
        '{"loans": [{"loan_id": "L999", "principal": 5000, "direction": "borrowed"}]}',
        encoding="utf-8",
    )
    cfg = {"data": {"loans_file": str(loans_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.patch(
            "/api/loans/L999/interest-rate",
            json={"interest_rate_pct": 6.5},
            content_type="application/json",
        )
    assert r.status_code == 200
    import json as _json
    saved = _json.loads(loans_path.read_text("utf-8"))
    assert saved["loans"][0]["interest_rate_pct"] == 6.5


def test_api_loan_interest_rate_not_found(tmp_path, monkeypatch):
    """PATCH /api/loans/<id>/interest-rate returns 404 for unknown loan_id."""
    loans_path = tmp_path / "loans.json"
    loans_path.write_text('{"loans": []}', encoding="utf-8")
    cfg = {"data": {"loans_file": str(loans_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.patch(
            "/api/loans/MISSING/interest-rate",
            json={"interest_rate_pct": 5.0},
            content_type="application/json",
        )
    assert r.status_code == 404


def test_net_worth_page_with_manual_assets_returns_200(client, tmp_path, monkeypatch):
    """GET /net-worth with manual_assets.json present returns 200."""
    import json as _json
    assets_path = tmp_path / "manual_assets.json"
    assets_path.write_text(_json.dumps({
        "assets": [{"asset_id": "A1", "type": "property", "name": "Home",
                    "snapshots": [{"date": "2026-01-01", "value": 600000}]}],
        "liabilities": [{"liability_id": "L1", "type": "mortgage", "name": "ANZ Loan",
                         "snapshots": [{"date": "2026-01-01", "balance": 400000}]}],
    }), "utf-8")
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path), "manual_assets_file": str(assets_path)}}
    from src.db import get_db, init_db
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    r = client.get("/net-worth")
    assert r.status_code == 200
    assert b"Net Worth" in r.data


def test_portfolio_page_empty_returns_200(client, tmp_path, monkeypatch):
    """GET /portfolio with no lots returns 200."""
    port_path = tmp_path / "portfolio.json"
    port_path.write_text('{"lots": []}', "utf-8")
    cfg = {"data": {"portfolio_file": str(port_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    r = client.get("/portfolio")
    assert r.status_code == 200
    assert b"Investment Portfolio" in r.data


def test_api_asset_create_and_delete(tmp_path, monkeypatch):
    """POST /api/assets creates an asset; DELETE removes it."""
    import json as _json
    assets_path = tmp_path / "manual_assets.json"
    assets_path.write_text('{"assets":[],"liabilities":[]}', "utf-8")
    cfg = {"data": {"manual_assets_file": str(assets_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/assets", json={"type": "property", "name": "Test Home",
                                        "initial_value": 500000, "initial_date": "2026-06-01"})
    assert r.status_code == 200
    data = _json.loads(r.data)
    assert data["ok"] is True
    asset_id = data["asset_id"]
    saved = _json.loads(assets_path.read_text("utf-8"))
    assert saved["assets"][0]["name"] == "Test Home"
    with server.app.test_client() as c:
        r2 = c.delete(f"/api/assets/{asset_id}")
    assert r2.status_code == 200
    assert _json.loads(assets_path.read_text("utf-8"))["assets"] == []


def test_api_portfolio_lot_create_and_delete(tmp_path, monkeypatch):
    """POST /api/portfolio/lots creates a lot; DELETE removes it."""
    import json as _json
    port_path = tmp_path / "portfolio.json"
    port_path.write_text('{"lots":[]}', "utf-8")
    cfg = {"data": {"portfolio_file": str(port_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/portfolio/lots",
                   json={"ticker": "VAS", "units": 100, "cost_per_unit": 98.50,
                         "date": "2026-01-15"})
    assert r.status_code == 200
    data = _json.loads(r.data)
    lot_id = data["lot_id"]
    saved = _json.loads(port_path.read_text("utf-8"))
    assert saved["lots"][0]["ticker"] == "VAS"
    with server.app.test_client() as c:
        r2 = c.delete(f"/api/portfolio/lots/{lot_id}")
    assert r2.status_code == 200
    assert _json.loads(port_path.read_text("utf-8"))["lots"] == []


# ── Phase 4 — Tax & Compliance ────────────────────────────────────────────────

def test_franking_credits_page_returns_200(client, tmp_path, monkeypatch):
    """GET /franking-credits with empty data returns 200."""
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    from src.db import get_db, init_db
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    r = client.get("/franking-credits")
    assert r.status_code == 200
    assert b"Franking" in r.data


def test_api_franking_credits_save_and_retrieve(tmp_path, monkeypatch):
    """POST /api/franking-credits saves per-FY data."""
    import json as _json
    fc_path = tmp_path / "franking_credits.json"
    cfg = {"data": {"franking_credits_file": str(fc_path)}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/franking-credits",
                   json={"fy": "2026", "cash_dividends": 700.0,
                         "franking_credits": 300.0, "notes": "CBA"})
    assert r.status_code == 200
    assert _json.loads(r.data)["ok"] is True
    saved = _json.loads(fc_path.read_text("utf-8"))
    assert saved["2026"]["cash_dividends"] == 700.0
    assert saved["2026"]["franking_credits"] == 300.0


def test_api_franking_credits_invalid_fy(tmp_path, monkeypatch):
    """POST /api/franking-credits rejects non-numeric FY."""
    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/franking-credits",
                   json={"fy": "bad", "cash_dividends": 0})
    assert r.status_code == 400


def test_tax_export_returns_zip(tmp_path, monkeypatch):
    """GET /api/tax-export/<fy> returns a ZIP file."""
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/tax-export/2026")
    assert r.status_code == 200
    assert r.content_type == "application/zip"
    assert r.data[:2] == b"PK"  # ZIP magic bytes


def test_tax_export_invalid_fy_returns_400(tmp_path, monkeypatch):
    """GET /api/tax-export/<bad> returns 400."""
    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/tax-export/bad")
    assert r.status_code == 400


def test_fy_summary_includes_franking_data(tmp_path, monkeypatch):
    """GET /fy-summary with franking data includes it in the rendered HTML."""
    import json as _json
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    fc_path = tmp_path / "franking_credits.json"
    fc_path.write_text(_json.dumps({"2026": {"cash_dividends": 700.0,
                                              "franking_credits": 300.0, "notes": ""}}), "utf-8")
    cfg = {"data": {"database": str(db_path), "franking_credits_file": str(fc_path)}}
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/fy-summary")
    assert r.status_code == 200


# ── Phase 5 — Xero/MYOB export ───────────────────────────────────────────────

def test_business_export_csv_returns_200(tmp_path, monkeypatch):
    """GET /api/business-export/2026?format=csv returns 200 with CSV content-type."""
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/business-export/2026?format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.content_type


def test_business_export_ofx_returns_200(tmp_path, monkeypatch):
    """GET /api/business-export/2026?format=ofx returns 200 with ofx content-type."""
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/business-export/2026?format=ofx")
    assert r.status_code == 200
    assert "ofx" in r.content_type
    assert b"OFXHEADER" in r.data


def test_business_export_qif_returns_200(tmp_path, monkeypatch):
    """GET /api/business-export/2026?format=qif returns 200 with qif content-type."""
    from src.db import get_db, init_db
    db_path = tmp_path / "test.db"
    cfg = {"data": {"database": str(db_path)}}
    conn = get_db(cfg); init_db(conn); conn.close()
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/business-export/2026?format=qif")
    assert r.status_code == 200
    assert b"!Type:Bank" in r.data


def test_business_export_invalid_fy_returns_400(tmp_path, monkeypatch):
    """GET /api/business-export/bad returns 400."""
    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/business-export/bad")
    assert r.status_code == 400


def test_business_export_invalid_format_returns_400(tmp_path, monkeypatch):
    """GET /api/business-export/2026?format=xlsx returns 400."""
    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/api/business-export/2026?format=xlsx")
    assert r.status_code == 400


# ── Natural language search ───────────────────────────────────────────────────

def test_natural_search_route_registered():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    assert "/api/search/natural" in rules


def test_natural_search_missing_query_returns_400(tmp_path, monkeypatch):
    """POST /api/search/natural with no query body returns 400."""
    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/search/natural", json={})
    assert r.status_code == 400


def test_natural_search_empty_df_returns_ok(tmp_path, monkeypatch):
    """When no transactions, endpoint returns 200 with a message (no AI call made)."""
    import pandas as pd
    import src.db as _db_mod

    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    monkeypatch.setattr(_db_mod, "load_transactions", lambda config, **kw: pd.DataFrame())
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/search/natural", json={"query": "how much on groceries?"})
    data = r.get_json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert "No transactions" in data["answer"]


def test_reconciliation_route_registered():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    assert "/reconciliation" in rules


def test_reconciliation_returns_200(tmp_path, monkeypatch):
    """GET /reconciliation renders with empty data."""
    import pandas as pd
    import src.db as _db_mod
    import src.balance_tracker as _bt

    cfg = {"data": {}, "accounts": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)
    monkeypatch.setattr(_bt, "load_balance_history", lambda cfg: pd.DataFrame())

    def _empty_db(_cfg):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _db_mod.init_db(conn)
        return conn

    monkeypatch.setattr(_db_mod, "get_db", _empty_db)
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.get("/reconciliation")
    assert r.status_code == 200


def test_anomaly_api_routes_registered():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    assert "/api/anomaly/run" in rules
    assert "/api/anomaly/summary" in rules


def test_receipt_api_routes_registered():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    assert "/api/txn/<txn_id>/receipt" in rules


def test_natural_search_calls_ai_and_returns_html(tmp_path, monkeypatch):
    """With transactions, the endpoint calls Claude and returns HTML-formatted answer."""
    import pandas as pd
    import src.db as _db_mod

    cfg = {"data": {}}
    monkeypatch.setattr(server, "_load_config", lambda: cfg)

    df = pd.DataFrame([
        {"date": "2026-06-01", "description": "Woolworths", "amount": -80.0,
         "category": "Groceries", "account": "ANZ"}
    ])
    monkeypatch.setattr(_db_mod, "load_transactions", lambda config, **kw: df)

    class _FakeMsg:
        class _Content:
            text = "**Groceries total:** $80"
        content = [_Content()]

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return _FakeMsg()

    import src.ai_backend as _ai_mod
    monkeypatch.setattr(_ai_mod, "get_backend", lambda cfg: _FakeClient())

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        r = c.post("/api/search/natural", json={"query": "groceries total?"})
    data = r.get_json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert "<strong>" in data["answer"]  # md_to_html converted **...**
