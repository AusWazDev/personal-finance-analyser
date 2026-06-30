"""
Basiq Open Banking API client.

Handles server-to-server auth, bank connections, and CDR transaction fetching.
Requires BASIQ_API_KEY set as a Windows User environment variable.
State (user_id, connections, last_sync) persisted to Data/basiq_state.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASIQ_API = "https://au-api.basiq.io"
_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}


# ── Configuration ──────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True if the Basiq API key environment variable is set."""
    return bool(os.environ.get("BASIQ_API_KEY"))


def _get_api_key() -> str:
    key = os.environ.get("BASIQ_API_KEY", "")
    if not key:
        raise RuntimeError("BASIQ_API_KEY environment variable not set")
    return key


# ── Authentication ─────────────────────────────────────────────────────────────

def get_app_token() -> str:
    """Return a valid server-access token, refreshing if near expiry."""
    now = time.time()
    if _TOKEN_CACHE["token"] and now < (_TOKEN_CACHE["expires_at"] - 60):
        return _TOKEN_CACHE["token"]

    creds = base64.b64encode(f"{_get_api_key()}:".encode()).decode()
    req = urllib.request.Request(
        f"{_BASIQ_API}/token",
        data=b"scope=SERVER_ACCESS",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "basiq-version": "3.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Basiq token error {exc.code}: {body}") from exc

    _TOKEN_CACHE["token"] = data["access_token"]
    _TOKEN_CACHE["expires_at"] = now + float(data.get("expires_in", 3600))
    return _TOKEN_CACHE["token"]


# ── Low-level request helper ───────────────────────────────────────────────────

def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated Basiq API request."""
    token = get_app_token()
    url = f"{_BASIQ_API}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "basiq-version": "3.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read()
            return json.loads(resp_body.decode()) if resp_body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Basiq {method} {path} → {exc.code}: {body}") from exc


# ── User management ────────────────────────────────────────────────────────────

def get_or_create_user(email: str, state: dict, state_path: Path) -> str:
    """Return stored Basiq user_id, creating a new user if none exists."""
    if state.get("user_id"):
        return state["user_id"]
    data = _request("POST", "/users", {"email": email})
    user_id = data["id"]
    state["user_id"] = user_id
    save_state(state_path, state)
    logger.info(f"Basiq: created user {user_id}")
    return user_id


# ── Consent / connections ──────────────────────────────────────────────────────

def create_auth_link(user_id: str, redirect_url: str | None = None) -> str:
    """Return a Basiq hosted-consent URL for connecting a bank account."""
    body: dict[str, Any] = {"mobile": False}
    if redirect_url:
        body["redirectUrl"] = redirect_url
    data = _request("POST", f"/users/{user_id}/auth_link", body)
    return data["links"]["public"]


def get_connections(user_id: str) -> list[dict]:
    """List all bank connections for the user."""
    data = _request("GET", f"/users/{user_id}/connections")
    return data.get("data", [])


def refresh_connections(user_id: str) -> None:
    """Trigger a CDR refresh of all connections (pulls latest data from banks)."""
    try:
        _request("POST", f"/users/{user_id}/connections/refresh")
    except RuntimeError as exc:
        logger.warning(f"Basiq refresh warning: {exc}")


def delete_connection(user_id: str, connection_id: str) -> None:
    """Disconnect a bank. 404 is treated as success (already gone)."""
    token = get_app_token()
    url = f"{_BASIQ_API}/users/{user_id}/connections/{connection_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "basiq-version": "3.0"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_accounts(user_id: str) -> list[dict]:
    """Return all connected bank accounts from Basiq."""
    data = _request("GET", f"/users/{user_id}/accounts")
    return data.get("data", [])


def fetch_transactions(user_id: str, since: date | None = None) -> list[dict]:
    """
    Fetch all posted transactions, optionally filtered by post date.
    Follows Basiq pagination automatically.
    """
    path = f"/users/{user_id}/transactions?limit=500&filter=transaction.status.eq(posted)"
    if since:
        path += f",transaction.postDate.gteq({since.isoformat()})"

    all_txns: list[dict] = []
    while path:
        data = _request("GET", path)
        all_txns.extend(data.get("data", []))
        next_link = data.get("links", {}).get("next", "")
        if next_link and next_link != f"{_BASIQ_API}{path}":
            path = next_link.replace(_BASIQ_API, "")
        else:
            path = ""

    return all_txns


# ── Transaction mapping ────────────────────────────────────────────────────────

def _make_txn_id(date_str: str, amount: float, description: str, account: str) -> str:
    """Compute txn_id using the same algorithm as the statement parsers."""
    key = f"{date_str}|{amount:.2f}|{description[:50].upper()}|{account}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def map_to_transaction(basiq_txn: dict, account_map: dict[str, str]) -> dict | None:
    """
    Map a Basiq transaction to PFA's internal row format.
    Returns None if the transaction cannot be mapped (unknown account, bad date).
    """
    post_date = (basiq_txn.get("postDate") or "")[:10]
    if len(post_date) != 10:
        return None
    try:
        amount = float(basiq_txn.get("amount", 0))
    except (ValueError, TypeError):
        return None

    basiq_acct_id = basiq_txn.get("account", "")
    account_name = account_map.get(basiq_acct_id, "")
    if not account_name:
        return None

    description = (basiq_txn.get("description") or "").upper().strip()
    return {
        "txn_id":       _make_txn_id(post_date, amount, description, account_name),
        "source_id":    basiq_txn.get("id", ""),
        "date":         post_date,
        "amount":       amount,
        "description":  description[:200],
        "payee_name":   "",
        "reference":    "",
        "note":         "",
        "account":      account_name,
        "account_type": "transaction",
        "category":     "",
        "sub_category": "",
        "is_business":  False,
        "user_note":    "",
        "source_file":  "basiq_sync",
        "zip_source":   "",
    }


def build_account_map(basiq_accounts: list[dict], config: dict) -> dict[str, str]:
    """
    Map Basiq account IDs → PFA display_names.

    Matches by BSB + account_number from config first; falls back to name
    substring match. Unmatched Basiq accounts are excluded from the map.
    """
    config_by_bsb: dict[tuple[str, str], str] = {}
    for _key, acct in config.get("accounts", {}).items():
        bsb = str(acct.get("bsb", "") or "").replace("-", "")
        num = str(acct.get("account_number", "") or "")
        name = acct.get("display_name", "")
        if bsb and num and name:
            config_by_bsb[(bsb, num)] = name

    account_map: dict[str, str] = {}
    for acct in basiq_accounts:
        acct_id = acct.get("id", "")
        bsb = str(acct.get("bsb", "") or "").replace("-", "")
        num = str(acct.get("accountNo", "") or acct.get("account_number", "") or "")

        matched = config_by_bsb.get((bsb, num))
        if matched:
            account_map[acct_id] = matched
            continue

        # Name substring fallback
        acct_name = (acct.get("name") or "").lower()
        for _key, cfg_acct in config.get("accounts", {}).items():
            display = cfg_acct.get("display_name", "")
            if display and (display.lower() in acct_name or acct_name in display.lower()):
                if acct_id not in account_map:
                    account_map[acct_id] = display
                break

    return account_map


# ── State persistence ──────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    """Load basiq_state.json. Returns empty dict if absent or unreadable."""
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception:
        pass
    return {}


def save_state(path: Path, state: dict) -> None:
    """Write basiq_state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# ── Sync helper ────────────────────────────────────────────────────────────────

def default_since_date(state: dict) -> date:
    """Return the date to sync from: day after last_sync, or 90 days ago."""
    last_sync = state.get("last_sync", "")
    if last_sync:
        try:
            from datetime import datetime as _dt
            last = _dt.fromisoformat(last_sync).date()
            return last + timedelta(days=1)
        except Exception:
            pass
    return date.today() - timedelta(days=90)
