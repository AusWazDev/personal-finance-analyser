"""Generic CSV bank profile management.

A profile maps CSV column names to the standard transaction schema so that
statements from unsupported banks can be imported without writing a custom parser.
Profiles are keyed by a stable fingerprint of the CSV header row and stored in
Data/bank_profiles.json.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "Data/bank_profiles.json"


def _path(config: dict) -> Path:
    return Path(config.get("data", {}).get("bank_profiles_file", _DEFAULT_PATH))


def headers_key(headers: list[str]) -> str:
    """Stable key derived from CSV column names (lower-cased, order-preserving)."""
    return "|".join(h.strip().lower() for h in headers if h.strip())


def load_profiles(config: dict) -> dict:
    """Return all saved profiles as {key: profile_dict}. Empty dict if none saved."""
    p = _path(config)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning(f"Could not load bank profiles from {p}")
        return {}


def save_profiles(profiles: dict, config: dict) -> None:
    p = _path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def find_profile(headers: list[str], config: dict) -> dict | None:
    """Return the saved profile matching these CSV headers, or None."""
    key = headers_key(headers)
    return load_profiles(config).get(key)


def save_profile(key: str, profile: dict, config: dict) -> None:
    """Insert or replace a single profile."""
    profiles = load_profiles(config)
    profiles[key] = profile
    save_profiles(profiles, config)


def delete_profile(key: str, config: dict) -> bool:
    """Remove a profile by key. Returns True if it existed."""
    profiles = load_profiles(config)
    if key not in profiles:
        return False
    del profiles[key]
    save_profiles(profiles, config)
    return True
