"""Tests for src/modules.py — load, save, default, filter."""
import json
from pathlib import Path

import pytest

from src.modules import DEFAULT_MODULES, is_enabled, load_modules, save_modules


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(tmp_path, modules_file=None):
    d = {"data": {}}
    if modules_file:
        d["data"]["modules_file"] = str(modules_file)
    return d


# ── load_modules ──────────────────────────────────────────────────────────────

def test_load_returns_all_defaults_when_no_file(tmp_path):
    cfg = _cfg(tmp_path, modules_file=tmp_path / "modules.json")
    result = load_modules(cfg)
    assert result == DEFAULT_MODULES


def test_load_reads_existing_json(tmp_path):
    p = tmp_path / "modules.json"
    p.write_text(json.dumps({"modules": {"budgets": False, "goals": True}}))
    cfg = _cfg(tmp_path, modules_file=p)
    result = load_modules(cfg)
    assert result["budgets"] is False
    assert result["goals"] is True


def test_load_merges_missing_keys_with_defaults(tmp_path):
    p = tmp_path / "modules.json"
    # File only has one key — rest should default to True
    p.write_text(json.dumps({"modules": {"budgets": False}}))
    cfg = _cfg(tmp_path, modules_file=p)
    result = load_modules(cfg)
    assert result["budgets"] is False
    assert result["coverage"] is True  # not in file → defaults to True


def test_load_returns_defaults_on_corrupt_json(tmp_path):
    p = tmp_path / "modules.json"
    p.write_text("not valid json {{{")
    cfg = _cfg(tmp_path, modules_file=p)
    result = load_modules(cfg)
    assert result == DEFAULT_MODULES


# ── save_modules ──────────────────────────────────────────────────────────────

def test_save_writes_json(tmp_path):
    p = tmp_path / "modules.json"
    cfg = _cfg(tmp_path, modules_file=p)
    modules = {**DEFAULT_MODULES, "budgets": False}
    save_modules(modules, cfg)
    data = json.loads(p.read_text())
    assert data["modules"]["budgets"] is False
    assert data["modules"]["goals"] is True


def test_save_includes_updated_at(tmp_path):
    p = tmp_path / "modules.json"
    cfg = _cfg(tmp_path, modules_file=p)
    save_modules(DEFAULT_MODULES, cfg)
    data = json.loads(p.read_text())
    assert "updated_at" in data


def test_save_strips_unknown_keys(tmp_path):
    p = tmp_path / "modules.json"
    cfg = _cfg(tmp_path, modules_file=p)
    modules = {**DEFAULT_MODULES, "nonexistent_module": True}
    save_modules(modules, cfg)
    data = json.loads(p.read_text())
    assert "nonexistent_module" not in data["modules"]


def test_save_roundtrip(tmp_path):
    p = tmp_path / "modules.json"
    cfg = _cfg(tmp_path, modules_file=p)
    modules = {**DEFAULT_MODULES, "recommendations": False, "coverage": False}
    save_modules(modules, cfg)
    loaded = load_modules(cfg)
    assert loaded["recommendations"] is False
    assert loaded["coverage"] is False
    assert loaded["budgets"] is True


# ── is_enabled ────────────────────────────────────────────────────────────────

def test_is_enabled_none_key_always_true(tmp_path):
    cfg = _cfg(tmp_path, modules_file=tmp_path / "modules.json")
    assert is_enabled(None, cfg) is True


def test_is_enabled_returns_true_for_default_modules(tmp_path):
    cfg = _cfg(tmp_path, modules_file=tmp_path / "modules.json")
    for key in DEFAULT_MODULES:
        assert is_enabled(key, cfg) is True


def test_is_enabled_returns_false_when_disabled(tmp_path):
    p = tmp_path / "modules.json"
    p.write_text(json.dumps({"modules": {"budgets": False}}))
    cfg = _cfg(tmp_path, modules_file=p)
    assert is_enabled("budgets", cfg) is False


def test_is_enabled_returns_true_for_unknown_key(tmp_path):
    cfg = _cfg(tmp_path, modules_file=tmp_path / "modules.json")
    assert is_enabled("unknown_key", cfg) is True


# ── nav_tabs filter ───────────────────────────────────────────────────────────

def test_nav_tabs_fourth_field_present():
    from src.utils import NAV_TABS, SETTINGS_TABS
    for tab in NAV_TABS:
        assert len(tab) == 4, f"NAV_TABS entry has wrong length: {tab}"
    for tab in SETTINGS_TABS:
        assert len(tab) == 4, f"SETTINGS_TABS entry has wrong length: {tab}"


def test_nav_tabs_filter_removes_disabled_module(tmp_path):
    from src.utils import NAV_TABS
    p = tmp_path / "modules.json"
    p.write_text(json.dumps({"modules": {"transfers": False}}))
    cfg = _cfg(tmp_path, modules_file=p)
    visible = [
        (label, href, key)
        for label, href, key, mod in NAV_TABS
        if is_enabled(mod, cfg)
    ]
    keys = [key for _, _, key in visible]
    assert "transfers" not in keys
    assert "superseded_pairs" not in keys  # also tagged "transfers"
    assert "transactions" in keys  # always-on


def test_nav_tabs_always_on_tabs_never_filtered(tmp_path):
    from src.utils import NAV_TABS
    # Disable all known modules
    p = tmp_path / "modules.json"
    p.write_text(json.dumps({"modules": {k: False for k in DEFAULT_MODULES}}))
    cfg = _cfg(tmp_path, modules_file=p)
    visible = [
        (label, href, key)
        for label, href, key, mod in NAV_TABS
        if is_enabled(mod, cfg)
    ]
    keys = [key for _, _, key in visible]
    # Core always-on tabs should survive
    assert "transactions" in keys
    assert "review" in keys
    assert "net_worth" in keys
    assert "cash_flow" in keys
