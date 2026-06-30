"""Tests for src/basiq.py and related server routes."""

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── src/basiq.py unit tests ────────────────────────────────────────────────────

from src.basiq import (
    is_configured,
    _make_txn_id,
    map_to_transaction,
    build_account_map,
    load_state,
    save_state,
    default_since_date,
)


class TestIsConfigured:
    def test_returns_false_when_key_absent(self, monkeypatch):
        monkeypatch.delenv("BASIQ_API_KEY", raising=False)
        assert is_configured() is False

    def test_returns_true_when_key_present(self, monkeypatch):
        monkeypatch.setenv("BASIQ_API_KEY", "server_abc123")
        assert is_configured() is True


class TestMakeTxnId:
    def test_produces_12_char_hex(self):
        tid = _make_txn_id("2026-06-01", -45.67, "WOOLWORTHS 1234", "ANZ Personal")
        assert len(tid) == 12
        assert all(c in "0123456789abcdef" for c in tid)

    def test_uppercase_description(self):
        tid1 = _make_txn_id("2026-06-01", -10.0, "woolworths", "ANZ Personal")
        tid2 = _make_txn_id("2026-06-01", -10.0, "WOOLWORTHS", "ANZ Personal")
        assert tid1 == tid2

    def test_different_amounts_differ(self):
        t1 = _make_txn_id("2026-06-01", -10.0, "WOOLWORTHS", "ANZ Personal")
        t2 = _make_txn_id("2026-06-01", -20.0, "WOOLWORTHS", "ANZ Personal")
        assert t1 != t2

    def test_different_dates_differ(self):
        t1 = _make_txn_id("2026-06-01", -10.0, "WOOLWORTHS", "ANZ Personal")
        t2 = _make_txn_id("2026-06-02", -10.0, "WOOLWORTHS", "ANZ Personal")
        assert t1 != t2


class TestMapToTransaction:
    def _account_map(self):
        return {"acc-001": "ANZ Personal", "acc-002": "ANZ Plus Everyday"}

    def _basiq_txn(self, **overrides):
        base = {
            "id": "txn-abc123",
            "postDate": "2026-06-15",
            "amount": "-45.67",
            "description": "WOOLWORTHS METRO",
            "account": "acc-001",
        }
        base.update(overrides)
        return base

    def test_maps_basic_fields(self):
        row = map_to_transaction(self._basiq_txn(), self._account_map())
        assert row is not None
        assert row["date"] == "2026-06-15"
        assert row["amount"] == -45.67
        assert row["description"] == "WOOLWORTHS METRO"
        assert row["account"] == "ANZ Personal"
        assert row["source_id"] == "txn-abc123"
        assert row["source_file"] == "basiq_sync"

    def test_description_uppercased(self):
        row = map_to_transaction(self._basiq_txn(description="Woolworths Metro"), self._account_map())
        assert row["description"] == "WOOLWORTHS METRO"

    def test_returns_none_for_unknown_account(self):
        row = map_to_transaction(self._basiq_txn(account="acc-unknown"), self._account_map())
        assert row is None

    def test_returns_none_for_missing_post_date(self):
        row = map_to_transaction(self._basiq_txn(postDate=""), self._account_map())
        assert row is None

    def test_returns_none_for_bad_amount(self):
        row = map_to_transaction(self._basiq_txn(amount="not-a-number"), self._account_map())
        assert row is None

    def test_txn_id_consistent_with_manual_import(self):
        row = map_to_transaction(self._basiq_txn(), self._account_map())
        expected_id = _make_txn_id("2026-06-15", -45.67, "WOOLWORTHS METRO", "ANZ Personal")
        assert row["txn_id"] == expected_id

    def test_credit_transaction(self):
        row = map_to_transaction(self._basiq_txn(amount="1234.56"), self._account_map())
        assert row["amount"] == 1234.56


class TestBuildAccountMap:
    def _config(self):
        return {
            "accounts": {
                "anz_personal": {
                    "display_name": "ANZ Personal",
                    "bsb": "012357",
                    "account_number": "595488286",
                    "bank": "ANZ",
                },
                "anz_plus": {
                    "display_name": "ANZ Plus Everyday",
                    "bsb": "014111",
                    "account_number": "434637893",
                    "bank": "ANZ Plus",
                },
            }
        }

    def test_matches_by_bsb_and_account_number(self):
        basiq_accounts = [
            {"id": "acc-001", "bsb": "012357", "accountNo": "595488286", "name": "Transaction"},
        ]
        mapping = build_account_map(basiq_accounts, self._config())
        assert mapping["acc-001"] == "ANZ Personal"

    def test_matches_bsb_with_dashes(self):
        basiq_accounts = [
            {"id": "acc-002", "bsb": "014-111", "accountNo": "434637893", "name": "Everyday"},
        ]
        mapping = build_account_map(basiq_accounts, self._config())
        assert mapping["acc-002"] == "ANZ Plus Everyday"

    def test_excludes_unmatched_accounts(self):
        basiq_accounts = [
            {"id": "acc-999", "bsb": "000000", "accountNo": "000000000", "name": "Unknown"},
        ]
        mapping = build_account_map(basiq_accounts, self._config())
        assert "acc-999" not in mapping

    def test_falls_back_to_name_match(self):
        basiq_accounts = [
            {"id": "acc-001", "bsb": "", "accountNo": "", "name": "anz personal account"},
        ]
        mapping = build_account_map(basiq_accounts, self._config())
        assert mapping.get("acc-001") == "ANZ Personal"

    def test_empty_basiq_accounts(self):
        mapping = build_account_map([], self._config())
        assert mapping == {}


class TestStateIO:
    def test_load_returns_empty_dict_when_absent(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert state == {}

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "state.json"
        data = {"user_id": "usr-123", "last_sync": "2026-06-28T10:00:00"}
        save_state(p, data)
        loaded = load_state(p)
        assert loaded["user_id"] == "usr-123"
        assert loaded["last_sync"] == "2026-06-28T10:00:00"

    def test_load_returns_empty_on_invalid_json(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("not json", encoding="utf-8")
        assert load_state(p) == {}

    def test_save_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "state.json"
        save_state(p, {"ok": True})
        assert p.exists()


class TestDefaultSinceDate:
    def test_returns_90_days_ago_when_no_last_sync(self):
        since = default_since_date({})
        assert since == date.today() - timedelta(days=90)

    def test_returns_day_after_last_sync(self):
        state = {"last_sync": "2026-06-20T15:00:00"}
        since = default_since_date(state)
        assert since == date(2026, 6, 21)

    def test_handles_invalid_last_sync(self):
        state = {"last_sync": "not-a-date"}
        since = default_since_date(state)
        assert since == date.today() - timedelta(days=90)


# ── Server route tests ─────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Test client with Basiq env var set and state path redirected to tmp."""
    monkeypatch.setenv("BASIQ_API_KEY", "server_test_key")
    import server
    server.app.config["TESTING"] = True
    # Redirect state path to tmp
    monkeypatch.setattr(server, "_basiq_state_path", lambda: tmp_path / "basiq_state.json")
    # Prevent _get_account_staleness DB calls from failing
    monkeypatch.setattr(server, "_get_account_staleness", lambda cfg: [])
    with server.app.test_client() as c:
        yield c


class TestDataSourcesRoute:
    def test_returns_200(self, client):
        r = client.get("/data-sources")
        assert r.status_code == 200

    def test_contains_page_title(self, client):
        r = client.get("/data-sources")
        assert b"Data Sources" in r.data


class TestBasiqStatusRoute:
    def test_returns_json(self, client):
        r = client.get("/api/basiq/status")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "configured" in data
        assert "connections" in data

    def test_configured_true_when_key_set(self, client):
        r = client.get("/api/basiq/status")
        data = json.loads(r.data)
        assert data["configured"] is True


class TestAccountToggleClosed:
    def test_marks_account_closed(self, client, tmp_path, monkeypatch):
        import server
        monkeypatch.setattr(server, "_basiq_state_path", lambda: tmp_path / "state.json")
        r = client.post("/api/account/toggle-closed",
                        json={"account": "ANZ Personal", "close": True},
                        content_type="application/json")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["ok"] is True
        assert data["closed"] is True

    def test_reactivates_account(self, client, tmp_path, monkeypatch):
        import server
        sp = tmp_path / "state.json"
        save_state(sp, {"closed_accounts": ["ANZ Personal"]})
        monkeypatch.setattr(server, "_basiq_state_path", lambda: sp)
        r = client.post("/api/account/toggle-closed",
                        json={"account": "ANZ Personal", "close": False},
                        content_type="application/json")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["closed"] is False

    def test_missing_account_returns_400(self, client):
        r = client.post("/api/account/toggle-closed",
                        json={},
                        content_type="application/json")
        assert r.status_code == 400


class TestBasiqConnectRoute:
    def test_returns_error_when_email_missing(self, client, monkeypatch):
        import server
        monkeypatch.setattr(server, "_load_config", lambda: {"basiq": {}})
        r = client.get("/api/basiq/connect")
        assert r.status_code == 400
        data = json.loads(r.data)
        assert "error" in data

    def test_returns_consent_url_on_success(self, client, monkeypatch):
        import server
        monkeypatch.setattr(server, "_load_config", lambda: {
            "basiq": {"email": "test@example.com"}
        })
        with patch("src.basiq.get_or_create_user", return_value="usr-123"), \
             patch("src.basiq.create_auth_link", return_value="https://consent.basiq.io/home?token=xxx"):
            r = client.get("/api/basiq/connect")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "consent_url" in data
        assert "basiq" in data["consent_url"]
