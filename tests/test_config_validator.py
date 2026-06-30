"""Tests for src/config_validator.py."""
from pathlib import Path

import pytest

from src.config_validator import validate_config


def _minimal_valid() -> dict:
    return {
        "data": {
            "input_dir": "Data/Raw Data",
            "output_dir": "reports",
        },
        "accounts": {
            "anz": {
                "file_pattern": "ANZ*.csv",
                "display_name": "ANZ Personal",
            }
        },
        "income": {
            "account_holder_name": "TEST USER",
        },
    }


# ── empty / missing config ─────────────────────────────────────────────────────

def test_empty_config_returns_issue():
    issues = validate_config({})
    assert issues
    assert any("missing or empty" in i for i in issues)


def test_none_config_treated_as_empty():
    issues = validate_config(None)  # type: ignore[arg-type]
    assert issues


# ── data section ──────────────────────────────────────────────────────────────

def test_missing_data_section_returns_issue():
    issues = validate_config({"accounts": {}})
    assert any("data" in i for i in issues)


def test_missing_input_dir_returns_issue():
    cfg = _minimal_valid()
    del cfg["data"]["input_dir"]
    issues = validate_config(cfg)
    assert any("input_dir" in i for i in issues)


def test_missing_output_dir_returns_issue():
    cfg = _minimal_valid()
    del cfg["data"]["output_dir"]
    issues = validate_config(cfg)
    assert any("output_dir" in i for i in issues)


def test_nonexistent_input_dir_flagged_when_base_dir_given(tmp_path):
    cfg = _minimal_valid()
    cfg["data"]["input_dir"] = "NoSuchDir/Raw"
    issues = validate_config(cfg, base_dir=tmp_path)
    assert any("does not exist" in i for i in issues)


def test_existing_input_dir_not_flagged(tmp_path):
    (tmp_path / "Raw").mkdir()
    cfg = _minimal_valid()
    cfg["data"]["input_dir"] = "Raw"
    issues = validate_config(cfg, base_dir=tmp_path)
    assert not any("does not exist" in i for i in issues)


def test_no_base_dir_skips_path_check():
    cfg = _minimal_valid()
    cfg["data"]["input_dir"] = "Data/Definitely/Does/Not/Exist"
    issues = validate_config(cfg, base_dir=None)
    assert not any("does not exist" in i for i in issues)


# ── accounts ──────────────────────────────────────────────────────────────────

def test_missing_accounts_section_returns_issue():
    cfg = _minimal_valid()
    del cfg["accounts"]
    issues = validate_config(cfg)
    assert any("account" in i.lower() for i in issues)


def test_empty_accounts_returns_issue():
    cfg = _minimal_valid()
    cfg["accounts"] = {}
    issues = validate_config(cfg)
    assert any("account" in i.lower() for i in issues)


def test_account_missing_file_pattern_flagged():
    cfg = _minimal_valid()
    cfg["accounts"]["anz"]["file_pattern"] = ""
    issues = validate_config(cfg)
    assert any("file_pattern" in i for i in issues)


def test_account_missing_display_name_flagged():
    cfg = _minimal_valid()
    cfg["accounts"]["anz"]["display_name"] = ""
    issues = validate_config(cfg)
    assert any("display_name" in i for i in issues)


# ── income ────────────────────────────────────────────────────────────────────

def test_missing_account_holder_name_returns_issue():
    cfg = _minimal_valid()
    del cfg["income"]["account_holder_name"]
    issues = validate_config(cfg)
    assert any("account_holder_name" in i for i in issues)


def test_missing_income_section_returns_issue():
    cfg = _minimal_valid()
    del cfg["income"]
    issues = validate_config(cfg)
    assert any("account_holder_name" in i for i in issues)


# ── valid config ──────────────────────────────────────────────────────────────

def test_valid_config_returns_no_issues():
    issues = validate_config(_minimal_valid())
    assert issues == []


def test_multiple_problems_all_reported():
    cfg = {"data": {"output_dir": "reports"}}  # missing input_dir, accounts, income
    issues = validate_config(cfg)
    assert len(issues) >= 3
