#!/usr/bin/env python3
"""
Local server for Personal Finance Analyser.

Usage:
    python server.py

Then open http://localhost:5100 in your browser.
The 'Start Import' button on the dashboard triggers a full finance_analyser.py run
and streams the output live to the browser.

Requires Flask:
    pip install flask
"""

import json
import locale
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from functools import wraps
from datetime import datetime as _dt, date as _date
from pathlib import Path

from src.utils import (
    EXCLUDE_FROM_SPEND as _EXCLUDE_FROM_SPEND,
    VALID_CATEGORIES as _VALID_CATEGORIES,
    CATEGORY_COLORS as _CATEGORY_COLORS,
)
from src.version import __version__ as _APP_VERSION
from src.db import (
    get_db, init_db, open_db,
    load_transactions, update_transaction, upsert_transactions,
    seed_accounts, get_accounts, upsert_account,
    load_covered_months, run_data_quality_checks,
    update_transactions_bulk, upsert_basiq_transactions,
)

logger = logging.getLogger(__name__)

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore not installed — SSL uses certifi bundle

locale.setlocale(locale.LC_TIME, "")

import yaml

# When bundled by PyInstaller, __file__ is inside the _MEIPASS temp bundle.
# All read-only assets (templates, static) live there; user-writable data
# (config.yaml, Data/) lives beside the .exe in the install directory.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(sys._MEIPASS)          # read-only bundle (templates, static, src, docs)
    BASE_DIR    = Path(sys.executable).parent  # install dir (config.yaml, Data/)
else:
    _BUNDLE_DIR = Path(__file__).parent
    BASE_DIR    = _BUNDLE_DIR

REPORTS_DIR = BASE_DIR / "reports"
PORT = 5100

try:
    from flask import Flask, Response, flash, jsonify, make_response, redirect, render_template, request, send_from_directory, session, stream_with_context
except ImportError:
    print("Flask is required. Install it with:")
    print("  pip install flask")
    sys.exit(1)

app = Flask(
    __name__,
    template_folder=str(_BUNDLE_DIR / "templates"),
    static_folder=str(_BUNDLE_DIR / "static"),
)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.jinja_env.globals["active_section"] = "data"  # default; overridden per route


@app.context_processor
def _inject_tabs():
    from src.utils import NAV_TABS as _NAV_TABS, SETTINGS_TABS as _SETTINGS_TABS
    from src.modules import is_enabled as _is_enabled
    cfg = _load_config()
    return {
        "nav_tabs": [
            (label, href, key)
            for label, href, key, mod in _NAV_TABS
            if _is_enabled(mod, cfg)
        ],
        "settings_tabs": [
            (label, href, key)
            for label, href, key, mod in _SETTINGS_TABS
            if _is_enabled(mod, cfg)
        ],
        "config_issues": _CONFIG_ISSUES,
    }
app.secret_key = os.urandom(24)  # random per-process; override via server.secret_key in config.yaml
_import_lock = threading.Lock()
_pipeline_status: dict = {
    "running": False, "trigger": None,
    "started_at": None, "finished_at": None, "error": None,
}
_CONFIG_ISSUES: list[str] = []  # populated at startup; injected into every page
_UPDATE_INFO: dict = {}  # populated by background thread if update_check_repo is configured

# ── Config & shared path helper ──────────────────────────────────────────────

_CONFIG_CACHE: dict = {}
_CONFIG_PATH: str = ""
_CONFIG_MTIME: float = 0.0


def _load_config() -> dict:
    """Load config.yaml, caching in memory until the file changes on disk."""
    global _CONFIG_CACHE, _CONFIG_PATH, _CONFIG_MTIME
    path = BASE_DIR / "config.yaml"
    path_str = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}  # file missing
    if path_str == _CONFIG_PATH and mtime == _CONFIG_MTIME:
        return _CONFIG_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            _CONFIG_CACHE = yaml.safe_load(f) or {}
        _CONFIG_PATH = path_str
        _CONFIG_MTIME = mtime
        return _CONFIG_CACHE
    except Exception:
        return {}  # invalid YAML or unreadable — do not update cache


def _data_path(config_key: str, default: str) -> Path:
    """Resolve a data-file path from config, falling back to default."""
    rel = _load_config().get("data", {}).get(config_key, default)
    return BASE_DIR / rel


def _check_for_updates(repo: str) -> None:
    """Background thread: check GitHub releases API and store result in _UPDATE_INFO."""
    global _UPDATE_INFO
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"finance-analyser/{_APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        tag = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", "")
        if not tag:
            return
        # Compare versions as tuples of ints (e.g. "1.2.3" → (1, 2, 3))
        def _v(s: str):
            try:
                return tuple(int(x) for x in s.split("."))
            except ValueError:
                return (0,)
        if _v(tag) > _v(_APP_VERSION):
            _UPDATE_INFO = {"current": _APP_VERSION, "latest": tag, "url": html_url, "has_update": True}
    except Exception as exc:
        logger.debug(f"Update check failed (network or rate-limited): {exc}")


def _load_run_metrics() -> dict:
    """Load run_metrics.json; returns zeros if absent or unreadable."""
    p = _data_path("run_metrics_file", "Data/run_metrics.json")
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {"api_errors": 0, "paypal_unmatched": 0}


def _load_budget_status() -> dict:
    """Compare current-period category spend against budgets from Data/budgets.json.

    Respects per-category period type (monthly vs fortnightly).
    Returns {"over": [...], "near": [...], "all": [...]} where near = 80–99% of limit.
    Each entry: {"category", "actual", "limit", "pct", "projected", "days_elapsed",
                 "days_in_period", "days_to_over", "period_type", "period_label"}.
    """
    import calendar as _cal
    from src.budgets import (
        load_budgets as _load_budgets,
        get_effective_budget as _geb,
        load_period_settings as _load_periods,
        current_fortnight_window as _fw,
    )
    cfg = _load_config()
    budgets = _load_budgets(cfg)
    if not budgets:
        return {"over": [], "near": [], "all": []}

    today = _date.today()
    month_prefix = today.strftime("%Y-%m")
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    days_elapsed_month = today.day

    # Fortnightly window
    fn_start, fn_end = _fw(today)
    days_in_fn = 14
    days_elapsed_fn = (today - fn_start).days + 1

    periods = _load_periods(cfg)

    try:
        from src.db import open_db as _open_db, init_db as _init_db
        with _open_db(cfg) as conn:
            _init_db(conn)

            # Monthly spend totals
            monthly_rows = conn.execute(
                "SELECT category, SUM(ABS(amount)) FROM transactions "
                "WHERE date LIKE ? AND amount < 0 GROUP BY category",
                (f"{month_prefix}%",),
            ).fetchall()
            monthly_spend = {r[0]: float(r[1]) for r in monthly_rows}

            # Fortnightly spend totals
            fn_rows = conn.execute(
                "SELECT category, SUM(ABS(amount)) FROM transactions "
                "WHERE date >= ? AND date <= ? AND amount < 0 GROUP BY category",
                (fn_start.isoformat(), fn_end.isoformat()),
            ).fetchall()
            fn_spend = {r[0]: float(r[1]) for r in fn_rows}

            over, near, all_cats = [], [], []
            for cat in budgets:
                period_type = periods.get(cat, "monthly")
                effective = _geb(conn, cat, month_prefix, cfg)
                base_limit = effective["effective"] or effective["base"]
                if not base_limit:
                    continue

                if period_type == "fortnightly":
                    # For fortnightly, compare against half the monthly limit
                    limit = round(base_limit / 2, 2)
                    actual = fn_spend.get(cat, 0.0)
                    days_elapsed = days_elapsed_fn
                    days_in_period = days_in_fn
                    period_label = f"{fn_start.strftime('%d %b')}–{fn_end.strftime('%d %b')}"
                else:
                    limit = base_limit
                    actual = monthly_spend.get(cat, 0.0)
                    days_elapsed = days_elapsed_month
                    days_in_period = days_in_month
                    period_label = today.strftime("%B %Y")

                pct = actual / limit if limit > 0 else 0.0
                period_fraction = days_elapsed / days_in_period
                projected = round(actual / period_fraction, 2) if period_fraction > 0 else actual
                daily_rate = actual / days_elapsed if days_elapsed > 0 else 0.0
                if daily_rate > 0 and actual < limit:
                    days_to_over: float | None = round((limit - actual) / daily_rate, 1)
                elif actual >= limit:
                    days_to_over = 0.0
                else:
                    days_to_over = None

                entry = {
                    "category":       cat,
                    "actual":         round(actual, 2),
                    "limit":          limit,
                    "pct":            round(pct * 100, 1),
                    "projected":      projected,
                    "days_elapsed":   days_elapsed,
                    "days_in_period": days_in_period,
                    "days_to_over":   days_to_over,
                    "period_type":    period_type,
                    "period_label":   period_label,
                    # Keep legacy key for templates that already reference days_in_month
                    "days_in_month":  days_in_period,
                }
                all_cats.append(entry)
                if pct >= 1.0:
                    over.append(entry)
                elif pct >= 0.8:
                    near.append(entry)
    except Exception as exc:
        logger.warning(f"Budget status check failed: {exc}")
        return {"over": [], "near": [], "all": []}

    return {"over": over, "near": near, "all": all_cats}


# ── Module guard ─────────────────────────────────────────────────────────────

def require_module(key: str):
    """Decorator: redirect to / with a flash message if the module is disabled."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            from src.modules import is_enabled as _is_enabled
            if not _is_enabled(key, _load_config()):
                flash("That feature is currently disabled. Enable it in Settings → Modules.", "warning")
                return redirect("/")
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ── User settings (name, preferences persisted outside config.yaml) ──────────

def _load_user_settings() -> dict:
    path = BASE_DIR / "Data" / "user_settings.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_user_settings(updates: dict) -> None:
    path = BASE_DIR / "Data" / "user_settings.json"
    data = _load_user_settings()
    data.update(updates)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── First-run setup guard ─────────────────────────────────────────────────────

@app.before_request
def _require_setup():
    """Redirect to /setup when modules.json is absent (first run).

    Skipped entirely in test mode so existing tests aren't affected.
    """
    if app.testing:
        return
    if request.path.startswith("/setup") or request.path.startswith("/static/") or request.path == "/favicon.ico":
        return
    modules_path = _data_path("modules_file", "Data/modules.json")
    if not modules_path.exists():
        return redirect("/setup")


# ── Local auth ────────────────────────────────────────────────────────────────

import hashlib as _hashlib


def _stored_password_hash() -> str:
    """Return server.password_hash from config, or '' if auth is disabled."""
    return (_load_config().get("server") or {}).get("password_hash", "") or ""


def _check_password(plain: str, stored: str) -> bool:
    """Verify plain text against a pbkdf2:sha256:iters:salt_hex:hash_hex string."""
    import hmac as _hmac
    try:
        _, algo, iters, salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        dk = _hashlib.pbkdf2_hmac(algo, plain.encode("utf-8"), salt, int(iters))
        return _hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


@app.before_request
def _require_auth():
    stored = _stored_password_hash()
    if not stored:
        return
    if request.path.startswith("/login") or request.path == "/favicon.ico":
        return
    if request.path.startswith("/setup"):
        return
    if request.path.startswith("/static/"):
        return
    if session.get("authed"):
        return
    return redirect("/login")


@app.route("/login", methods=["GET"])
def login_page():
    error = request.args.get("error", "")
    return render_template("login.html", error=error)


@app.route("/login", methods=["POST"])
def login_submit():
    stored = _stored_password_hash()
    password = request.form.get("password", "")
    if stored and _check_password(password, stored):
        session["authed"] = True
        return redirect("/")
    return redirect("/login?error=1")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── SSE job streaming helper ──────────────────────────────────────────────────

def _stream_job(script_args: list, success_msg: str, fail_msg: str) -> "Response":
    """Run a subprocess and stream its stdout as SSE. Uses the import lock."""
    if not _import_lock.acquire(blocking=False):
        def _busy():
            yield "data: A job is already running — please wait.\n\n"
            yield "event: error\ndata: busy\n\n"
        return Response(stream_with_context(_busy()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache"})

    def _run():
        try:
            env = _build_env()
            proc = subprocess.Popen(
                [sys.executable] + script_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(BASE_DIR), env=env,
            )
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()
            if proc.returncode == 0:
                yield "data: \n\n"
                yield f"data: ✓ {success_msg}\n\n"
                yield "event: done\ndata: ok\n\n"
            else:
                yield "data: \n\n"
                yield f"data: ✗ {fail_msg} (exit code {proc.returncode}).\n\n"
                yield "event: error\ndata: failed\n\n"
        except Exception as exc:
            yield f"data: Error: {exc}\n\n"
            yield "event: error\ndata: exception\n\n"
        finally:
            _import_lock.release()

    return Response(stream_with_context(_run()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Merchant rules helpers ────────────────────────────────────────────────────

_CAT_GROUPS = [
    ("Income", ["Board & Lodging", "Business Reimbursement", "Family Loan Received", "Gifts Received", "Income", "Interest Income"]),
    ("Expenditure", sorted([
        "Bank Fees & Charges", "Bank Interest Charged", "Business Expense",
        "Dining Out", "Donations", "Education", "Entertainment", "Family Loan Repayment",
        "Gifts Given", "Groceries", "Health", "Housing", "Insurance", "Investment",
        "Miscellaneous", "Personal Care", "Subscriptions", "Transfers", "Transport", "Travel", "Utilities",
    ])),
]
def _load_rules() -> dict:
    p = _data_path("merchant_rules_file", "data/merchant_rules.json")
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_rules(rules: dict) -> None:
    p = _data_path("merchant_rules_file", "data/merchant_rules.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, sort_keys=True)


def _rule_obj(merchant: str, val, source: str) -> dict:
    """Normalise a rule value (str or dict) to a display object."""
    if isinstance(val, dict):
        return {
            "merchant": merchant,
            "category": val.get("category", ""),
            "sub_category": val.get("sub_category", ""),
            "source": source,
        }
    return {"merchant": merchant, "category": str(val), "sub_category": "", "source": source}



# ── Superseded pairs helpers ──────────────────────────────────────────────────

def _load_superseded() -> list:
    p = _data_path("superseded_pairs_file", "data/superseded_pairs.json")
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_superseded(pairs: list) -> None:
    p = _data_path("superseded_pairs_file", "data/superseded_pairs.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(pairs, f, indent=2)


def _build_env() -> dict:
    """Return os.environ enriched with ANTHROPIC_API_KEY if not already present.

    On Windows, User environment variables set via SetEnvironmentVariable are stored
    in the registry and only inherited by processes started AFTER the variable was set.
    We read the value fresh each time via PowerShell so the server never needs a restart.
    """
    env = os.environ.copy()
    if env.get("ANTHROPIC_API_KEY"):
        return env

    # Ask PowerShell for the User-scope env var (most reliable on Windows)
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')"],
                capture_output=True, text=True, timeout=5,
            )
            key_val = result.stdout.strip()
            if key_val:
                os.environ["ANTHROPIC_API_KEY"] = key_val  # cache for process lifetime
                env["ANTHROPIC_API_KEY"] = key_val
                return env
        except Exception as exc:
            logger.warning(f"Failed to read ANTHROPIC_API_KEY from Windows User env: {exc}")

    # Fall back to config.yaml
    key_val = (_load_config().get("anthropic_api_key") or "").strip()
    if key_val:
        os.environ["ANTHROPIC_API_KEY"] = key_val  # cache for process lifetime
        env["ANTHROPIC_API_KEY"] = key_val

    return env


@app.route("/")
def index():
    return redirect("/dashboard")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/help")
def help_page():
    embed = request.args.get("embed") == "1"
    tab = request.args.get("tab", "dashboard")
    anchor = request.args.get("anchor", "")
    return render_template("help.html", active_tab="help", active_section="help",
                           embed=embed, initial_tab=tab, initial_anchor=anchor)


@app.route("/docs/<path:filename>")
def serve_doc(filename):
    docs_dir = _BUNDLE_DIR / "docs"
    resp = send_from_directory(docs_dir, filename)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/reports/<path:filename>")
def serve_report(filename):
    resp = send_from_directory(REPORTS_DIR, filename)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers.pop("ETag", None)
    resp.headers.pop("Last-Modified", None)
    return resp


@app.route("/api/start-import")
def start_import():
    """SSE endpoint: runs finance_analyser.py and streams stdout to the browser."""
    return _stream_job(
        [str(_BUNDLE_DIR / "finance_analyser.py")],
        "Import complete.",
        "Import failed",
    )


@app.route("/api/apply-override", methods=["POST"])
def api_apply_override():
    """Apply category override(s) directly to master CSV, cache, and overrides file."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500

    data = request.get_json()
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected JSON array of override entries"}), 400

    try:
        # Lazy import so server starts fast even if src deps are missing
        from src.review_applier import apply_entries
        result = apply_entries(data, config)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/save-note", methods=["POST"])
def api_save_note():
    """Save or clear a user note for a single transaction."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    data = request.get_json()
    txn_id = (data.get("txn_id") or "").strip()
    note = str(data.get("note") or "")
    if not txn_id:
        return jsonify({"error": "txn_id required"}), 400
    try:
        from src.review_applier import save_note
        result = save_note(txn_id, note, config)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tags", methods=["GET"])
def api_get_tags():
    """Return sorted list of all unique tags in use across transactions."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    try:
        with open_db(config) as conn:
            init_db(conn)
            rows = conn.execute(
                "SELECT tags FROM transactions WHERE tags IS NOT NULL AND tags != ''"
            ).fetchall()
        tag_set = set()
        for (raw,) in rows:
            for t in (raw or "").split(","):
                t = t.strip()
                if t:
                    tag_set.add(t)
        return jsonify({"tags": sorted(tag_set, key=str.lower)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/txn/<txn_id>/tags", methods=["POST"])
def api_set_txn_tags(txn_id):
    """Set (replace) the tags for a single transaction."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    data = request.get_json() or {}
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    cleaned = sorted({t.strip()[:50] for t in raw_tags if t.strip()})
    tags_str = ",".join(cleaned)
    try:
        updated = update_transaction(txn_id, {"tags": tags_str}, config)
        return jsonify({"ok": updated, "tags": cleaned})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/tags")
def tags_page():
    """Live tag summary page — all tags with spend totals and transaction counts."""
    config = _load_config()
    if not config:
        return "config.yaml not found", 500
    with open_db(config) as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT tags, amount FROM transactions WHERE tags IS NOT NULL AND tags != ''"
        ).fetchall()

    summary: dict = {}
    for (raw, amount) in rows:
        for t in (raw or "").split(","):
            t = t.strip()
            if not t:
                continue
            if t not in summary:
                summary[t] = {"tag": t, "count": 0, "spend": 0.0, "income": 0.0}
            summary[t]["count"] += 1
            if amount < 0:
                summary[t]["spend"] += abs(amount)
            else:
                summary[t]["income"] += amount

    items = sorted(summary.values(), key=lambda x: x["spend"], reverse=True)
    return render_template("tags.html", items=items, active_section="data", active_tab="tags")


@app.route("/year-comparison")
def year_comparison_page():
    """Year-on-year spending comparison — current period vs same period last year."""
    import calendar as _cal
    import pandas as pd

    config = _load_config()
    today = _date.today()
    view = request.args.get("view", "month")

    if view == "fy":
        fy = int(request.args.get("period", today.year if today.month >= 7 else today.year - 1))
        this_since = f"{fy}-07-01"
        this_until = f"{fy + 1}-06-30"
        prev_since = f"{fy - 1}-07-01"
        prev_until = f"{fy}-06-30"
        period_label = f"FY{fy + 1}"
        prev_label   = f"FY{fy}"
        period_param = str(fy)
        prev_param   = str(fy - 1)
        next_param   = str(fy + 1)
    else:
        raw = request.args.get("period", today.strftime("%Y-%m"))
        try:
            y, m = (int(p) for p in raw.split("-"))
        except (ValueError, AttributeError):
            y, m = today.year, today.month
        last_day = _cal.monthrange(y, m)[1]
        this_since = f"{y}-{m:02d}-01"
        this_until = f"{y}-{m:02d}-{last_day:02d}"
        prev_last   = _cal.monthrange(y - 1, m)[1]
        prev_since  = f"{y - 1}-{m:02d}-01"
        prev_until  = f"{y - 1}-{m:02d}-{prev_last:02d}"
        period_label = f"{_cal.month_name[m]} {y}"
        prev_label   = f"{_cal.month_name[m]} {y - 1}"
        period_param = f"{y}-{m:02d}"
        prev_param   = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
        next_param   = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"

    df_this = load_transactions(config, since=this_since, until=this_until)
    df_prev = load_transactions(config, since=prev_since, until=prev_until)

    def _spend_by_cat(df: "pd.DataFrame") -> dict:
        if df.empty:
            return {}
        mask = ~df["category"].isin(_EXCLUDE_FROM_SPEND) & (df["amount"] < 0)
        if not mask.any():
            return {}
        return (
            df.loc[mask]
            .groupby("category")["amount"]
            .sum()
            .abs()
            .round(2)
            .to_dict()
        )

    this_totals = _spend_by_cat(df_this)
    prev_totals = _spend_by_cat(df_prev)
    all_cats = sorted(set(this_totals) | set(prev_totals))

    rows = []
    for cat in all_cats:
        ta = round(this_totals.get(cat, 0.0), 2)
        pa = round(prev_totals.get(cat, 0.0), 2)
        delta = round(ta - pa, 2)
        pct   = round(delta / pa * 100, 1) if pa > 0 else None
        rows.append({
            "category": cat,
            "this_amt": ta,
            "prev_amt": pa,
            "delta":    delta,
            "pct":      pct,
            "color":    _CATEGORY_COLORS.get(cat, "#8D8D8D"),
        })
    rows.sort(key=lambda r: r["this_amt"], reverse=True)

    total_this  = round(sum(r["this_amt"] for r in rows), 2)
    total_prev  = round(sum(r["prev_amt"] for r in rows), 2)
    total_delta = round(total_this - total_prev, 2)
    total_pct   = round(total_delta / total_prev * 100, 1) if total_prev > 0 else None

    # 12-month grouped trend chart (month view) or 12-month FY bars (fy view)
    if view == "month":
        months_12 = []
        cy, cm = y, m
        for _ in range(12):
            months_12.append((cy, cm))
            if cm == 1:
                cy, cm = cy - 1, 12
            else:
                cm -= 1
        months_12.reverse()

        oldest_y, oldest_m = months_12[0]
        trend_start = f"{oldest_y - 1}-{oldest_m:02d}-01"
        df_trend = load_transactions(config, since=trend_start, until=this_until)

        def _monthly(df: "pd.DataFrame") -> dict:
            if df.empty:
                return {}
            mask = ~df["category"].isin(_EXCLUDE_FROM_SPEND) & (df["amount"] < 0)
            if not mask.any():
                return {}
            tmp = df.loc[mask].copy()
            tmp["mon"] = pd.to_datetime(tmp["date"]).dt.strftime("%Y-%m")
            return tmp.groupby("mon")["amount"].sum().abs().round(2).to_dict()

        monthly = _monthly(df_trend)
        xlabels   = [f"{_cal.month_abbr[mm]} '{str(my)[2:]}" for my, mm in months_12]
        this_vals = [float(monthly.get(f"{my}-{mm:02d}", 0.0)) for my, mm in months_12]
        prev_vals = [float(monthly.get(f"{my - 1}-{mm:02d}", 0.0)) for my, mm in months_12]
        chart_series_this = period_label
        chart_series_prev = prev_label

        # Per-category monthly breakdown for the trend picker (24 months of data)
        all_months_24 = [f"{my}-{mm:02d}" for my, mm in months_12] + \
                        [f"{my - 1}-{mm:02d}" for my, mm in months_12]
        cat_monthly_json: str | None
        if not df_trend.empty:
            mask_cat = ~df_trend["category"].isin(_EXCLUDE_FROM_SPEND) & (df_trend["amount"] < 0)
            if mask_cat.any():
                tmp2 = df_trend.loc[mask_cat].copy()
                tmp2["mon"] = pd.to_datetime(tmp2["date"]).dt.strftime("%Y-%m")
                cat_monthly_raw = (
                    tmp2.groupby(["category", "mon"])["amount"]
                    .sum().abs().round(2)
                    .reset_index()
                    .rename(columns={"amount": "v"})
                )
                cat_monthly_dict: dict = {}
                for _, row2 in cat_monthly_raw.iterrows():
                    cat_monthly_dict.setdefault(row2["category"], {})[row2["mon"]] = float(row2["v"])
                # Only pass categories that appear in the comparison table
                trend_cats = [r["category"] for r in rows]
                cat_monthly_json = json.dumps({
                    "months": [f"{_cal.month_abbr[int(m[5:7])]} '{m[2:4]}" for m in sorted(set(all_months_24))],
                    "months_raw": sorted(set(all_months_24)),
                    "by_cat": {c: cat_monthly_dict.get(c, {}) for c in trend_cats},
                    "colors": {r["category"]: r["color"] for r in rows},
                })
            else:
                cat_monthly_json = None
        else:
            cat_monthly_json = None
    else:
        # FY view: per-category grouped bar for this FY vs prior FY
        xlabels   = [r["category"] for r in rows if r["this_amt"] > 0 or r["prev_amt"] > 0]
        this_vals = [r["this_amt"] for r in rows if r["this_amt"] > 0 or r["prev_amt"] > 0]
        prev_vals = [r["prev_amt"] for r in rows if r["this_amt"] > 0 or r["prev_amt"] > 0]
        chart_series_this = period_label
        chart_series_prev = prev_label
        cat_monthly_json = None

    chart_json = json.dumps({
        "xlabels":   xlabels,
        "this_vals": this_vals,
        "prev_vals": prev_vals,
        "series_this": chart_series_this,
        "series_prev": chart_series_prev,
    })

    return render_template(
        "year_comparison.html",
        active_section="data",
        active_tab="year_comparison",
        view=view,
        period_label=period_label,
        prev_label=prev_label,
        period_param=period_param,
        prev_param=prev_param,
        next_param=next_param,
        rows=rows,
        total_this=total_this,
        total_prev=total_prev,
        total_delta=total_delta,
        total_pct=total_pct,
        chart_json=chart_json,
        cat_monthly_json=cat_monthly_json,
    )


@app.route("/api/search/natural", methods=["POST"])
def api_natural_search():
    """Natural language transaction search using Claude.

    Body: {"query": "...", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
    Returns: {"answer": "<html>", "matches": [txn_id, ...]}
    """
    from src.db import load_transactions as _lt
    body  = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400

    config = _load_config()
    since  = body.get("from") or None
    until  = body.get("to")   or None
    df     = _lt(config, since=since, until=until)
    if df.empty:
        return jsonify({"ok": True, "answer": "<p>No transactions to search.</p>", "matches": []})

    # Build a compact summary: first 500 rows, relevant columns only
    cols = [c for c in ["date", "description", "amount", "category", "account"] if c in df.columns]
    sample = df[cols].head(500).to_csv(index=False)
    prompt = (
        f"You are a personal finance assistant. The user has asked:\n\n"
        f'"{query}"\n\n'
        f"Here are their recent transactions (CSV, up to 500 rows):\n\n"
        f"{sample}\n\n"
        f"Answer the question concisely. If relevant, list matching transactions "
        f"by date and description. Use Markdown. Do not hallucinate data."
    )
    try:
        from src.ai_backend import get_backend as _get_ai
        client = _get_ai(config)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        from src.utils import md_to_html as _md
        answer_html = _md(msg.content[0].text)
    except Exception as exc:
        logger.warning(f"Natural search error: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "answer": answer_html, "matches": []})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Receive uploaded statement files and save to Raw Data."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500

    input_dir = Path(config["data"]["input_dir"])
    input_dir.mkdir(parents=True, exist_ok=True)

    _ALLOWED_EXTS = {".csv", ".CSV", ".pdf", ".PDF", ".html", ".HTML"}
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "no files"}), 400

    saved = []
    for fobj in files:
        name = Path(fobj.filename).name if fobj.filename else ""
        if not name or Path(name).suffix not in _ALLOWED_EXTS:
            continue
        dest = input_dir / name
        fobj.save(str(dest))
        saved.append(name)

    unknown_csvs: list[str] = []
    unknown_noncsvs: list[str] = []
    if saved:
        import fnmatch as _fnm, re as _re
        from src.parsers import _detect_file_type as _dft
        for _name in saved:
            _fp = input_dir / _name
            _sfx = _fp.suffix.lower()
            if _sfx == ".csv":
                try:
                    _ftype, _ = _dft(_fp, config)
                    if _ftype == "unknown_csv":
                        unknown_csvs.append(_name)
                except Exception:
                    pass
            elif _sfx in (".pdf", ".html"):
                _norm = _re.sub(r"\s*\(\d+\)$", "", _fp.stem) + _fp.suffix
                _matched = any(
                    _fnm.fnmatch(_norm, acct.get("file_pattern", ""))
                    for acct in config.get("accounts", {}).values()
                    if acct.get("file_pattern")
                )
                if not _matched:
                    unknown_noncsvs.append(_name)
        # Only auto-import if all files have known accounts
        # (unknown PDFs/HTMLs would be stored with inferred account names, creating orphan rows)
        if not unknown_noncsvs:
            _run_pipeline_bg("upload")
    from src.bank_profiles import load_profiles as _load_profiles
    _profiles = _load_profiles(config)
    profile_list = sorted(
        [{"key": k, "account": v.get("account") or k[:30]} for k, v in _profiles.items()],
        key=lambda x: x["account"].lower(),
    )
    config_accounts = sorted(
        [{"key": k, "name": v.get("display_name") or v.get("name") or k}
         for k, v in config.get("accounts", {}).items()],
        key=lambda x: x["name"].lower(),
    )
    return jsonify({"ok": True, "saved": saved, "count": len(saved),
                    "unknown_csvs": unknown_csvs, "unknown_noncsvs": unknown_noncsvs,
                    "bank_profiles": profile_list, "config_accounts": config_accounts})


@app.route("/api/upload/assign-accounts", methods=["POST"])
def api_upload_assign_accounts():
    """Register file headers as bank profile aliases after the user picks a profile."""
    import pandas as pd
    from src.bank_profiles import headers_key as _hk, load_profiles, save_profile
    config = _load_config()
    if not config:
        return jsonify({"ok": False, "error": "config not found"}), 500

    data        = request.get_json() or {}
    assignments = [a for a in data.get("assignments", []) if a.get("profile_key")]
    input_dir   = Path(config.get("data", {}).get("input_dir", "Data/Raw Data"))
    assigned    = 0

    # Handle PDF/HTML account assignments (save to file_account_overrides.json)
    account_assignments = [a for a in data.get("account_assignments", []) if a.get("account_key")]
    if account_assignments:
        _overrides_path = _data_path("file_account_overrides_file", "Data/file_account_overrides.json")
        try:
            _overrides = json.loads(_overrides_path.read_text(encoding="utf-8")) if _overrides_path.exists() else {}
        except Exception:
            _overrides = {}
        for _aa in account_assignments:
            _fn = (_aa.get("filename") or "").strip()
            _ak = (_aa.get("account_key") or "").strip()
            if _fn and _fn == Path(_fn).name and _ak:
                _overrides[_fn] = _ak
                assigned += 1
        try:
            _overrides_path.write_text(json.dumps(_overrides, indent=2), encoding="utf-8")
        except Exception:
            pass

    for a in assignments:
        filename    = (a.get("filename")    or "").strip()
        profile_key = (a.get("profile_key") or "").strip()
        # Security: reject any path traversal
        if not filename or filename != Path(filename).name:
            continue
        filepath = input_dir / filename
        if not filepath.exists():
            continue
        profiles = load_profiles(config)
        target   = profiles.get(profile_key)
        if not target:
            continue
        try:
            df         = pd.read_csv(str(filepath), nrows=0, encoding_errors="ignore")
            actual_key = _hk(list(df.columns))
        except Exception:
            continue
        if actual_key not in profiles:
            save_profile(actual_key, target, config)
        assigned += 1

    if assigned > 0:
        _run_pipeline_bg("upload")
    return jsonify({"ok": True, "assigned": assigned})


@app.route("/api/refresh-reports")
def api_refresh_reports():
    """SSE — regenerate all reports from existing SQLite data (no new import)."""
    return _stream_job(
        [str(_BUNDLE_DIR / "finance_analyser.py"),
         "--no-categorise", "--no-archive", "--no-recommend"],
        "Charts refreshed.",
        "Refresh failed",
    )


@app.route("/api/seed-merchant-rules", methods=["POST"])
def api_seed_merchant_rules():
    """Auto-populate merchant_rules.json from historical categorised transactions."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500

    body = request.get_json() or {}
    threshold = float(body.get("threshold", 0.80))
    min_count = int(body.get("min_count", 2))

    try:
        import pandas as pd

        with open_db(config) as conn:
            init_db(conn)
            rows = conn.execute(
                "SELECT upper(trim(substr(description,1,80))) as key, category, txn_id "
                "FROM transactions WHERE category != 'Miscellaneous' AND description IS NOT NULL"
            ).fetchall()

        if not rows:
            return jsonify({"ok": True, "added": 0, "backfilled": 0})

        df = pd.DataFrame([dict(r) for r in rows])
        total = df.groupby("key").size().rename("total")
        freq = df.groupby(["key", "category"]).size().rename("count").reset_index()
        freq = freq.join(total, on="key")
        freq["confidence"] = freq["count"] / freq["total"]
        good = freq[(freq["confidence"] >= threshold) & (freq["total"] >= min_count)]
        best = (good.sort_values("confidence", ascending=False)
                    .drop_duplicates("key")[["key", "category"]])

        existing = _load_rules()
        new_rules: dict = {}
        for _, row in best.iterrows():
            if row["key"] not in existing:
                new_rules[row["key"]] = row["category"]

        if new_rules:
            merged = {**existing, **new_rules}
            _save_rules(merged)

        # Backfill Miscellaneous transactions matching the new rules
        backfilled = 0
        if new_rules:
            with open_db(config) as conn2:
                for key, cat in new_rules.items():
                    ids = [r["txn_id"] for r in conn2.execute(
                        "SELECT txn_id FROM transactions "
                        "WHERE category = 'Miscellaneous' AND instr(upper(trim(description)), ?) > 0",
                        (key,),
                    ).fetchall()]
                    if ids:
                        ph = ",".join("?" * len(ids))
                        conn2.execute(f"UPDATE transactions SET category = ? WHERE txn_id IN ({ph})",
                                      [cat] + ids)
                        backfilled += len(ids)
                conn2.commit()

        return jsonify({"ok": True, "added": len(new_rules), "backfilled": backfilled})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/transactions/<txn_id>", methods=["PATCH"])
def api_update_transaction(txn_id: str):
    """Auto-save a single field change on a transaction row."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    data = request.get_json() or {}
    if not data:
        return jsonify({"ok": False, "error": "no fields provided"}), 400
    try:
        updated = update_transaction(txn_id, data, config)
        return jsonify({"ok": updated})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/txn/<txn_id>/split", methods=["POST"])
def api_split_transaction(txn_id: str):
    """Split a transaction into multiple categorised child rows.

    Body: {"splits": [{"category": str, "sub_category": str, "amount": float,
                        "description": str}, ...]}
    Splits must sum (in absolute value) to the parent transaction's amount.
    """
    cfg = _load_config()
    if not cfg:
        return jsonify({"error": "config.yaml not found"}), 500
    body = request.get_json(silent=True) or {}
    splits = body.get("splits", [])
    if not splits or len(splits) < 2:
        return jsonify({"ok": False, "error": "at least 2 splits required"}), 400

    with open_db(cfg) as conn:
        init_db(conn)
        try:
            parent = conn.execute(
                "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
            ).fetchone()
            if not parent:
                return jsonify({"ok": False, "error": "transaction not found"}), 404
            if parent["is_split_parent"]:
                return jsonify({"ok": False, "error": "already split — delete existing splits first"}), 400

            parent_amount = float(parent["amount"])
            split_total = sum(float(s.get("amount", 0)) for s in splits)
            if abs(abs(split_total) - abs(parent_amount)) > 0.015:
                return jsonify({"ok": False, "error":
                    f"splits total ${abs(split_total):.2f} ≠ transaction ${abs(parent_amount):.2f}"}), 400

            # Create child rows
            for i, s in enumerate(splits, start=1):
                child_id = f"{txn_id}_S{i}"
                child_amt = float(s.get("amount", 0))
                # Ensure child amounts have same sign as parent
                if parent_amount < 0 and child_amt > 0:
                    child_amt = -child_amt
                elif parent_amount > 0 and child_amt < 0:
                    child_amt = -child_amt
                conn.execute(
                    "INSERT OR REPLACE INTO transactions "
                    "(txn_id, date, amount, description, payee_name, reference, note, "
                    " account, account_type, category, sub_category, is_business, "
                    " is_tax_deductible, is_gst_claimable, tags, source_file, zip_source, "
                    " parent_txn_id, is_split_parent) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        child_id,
                        parent["date"],
                        child_amt,
                        s.get("description") or str(parent["description"] or ""),
                        parent["payee_name"],
                        parent["reference"],
                        parent["note"],
                        parent["account"],
                        parent["account_type"],
                        s.get("category") or str(parent["category"] or ""),
                        s.get("sub_category") or "",
                        parent["is_business"],
                        parent["is_tax_deductible"],
                        parent["is_gst_claimable"],
                        parent["tags"] or "",
                        parent["source_file"],
                        parent["zip_source"],
                        txn_id,
                    ),
                )
            # Mark parent as split
            conn.execute(
                "UPDATE transactions SET is_split_parent = 1 WHERE txn_id = ?", (txn_id,)
            )
            conn.commit()
            return jsonify({"ok": True, "children": len(splits)})
        except Exception as exc:
            conn.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/txn/<txn_id>/split", methods=["DELETE"])
def api_unsplit_transaction(txn_id: str):
    """Remove all split children and restore the parent transaction."""
    cfg = _load_config()
    if not cfg:
        return jsonify({"error": "config.yaml not found"}), 500
    with open_db(cfg) as conn:
        init_db(conn)
        try:
            conn.execute(
                "DELETE FROM transactions WHERE parent_txn_id = ?", (txn_id,)
            )
            conn.execute(
                "UPDATE transactions SET is_split_parent = 0 WHERE txn_id = ?", (txn_id,)
            )
            conn.commit()
            return jsonify({"ok": True})
        except Exception as exc:
            conn.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/recategorise")
def api_recategorise():
    """SSE endpoint: runs finance_analyser.py --recategorise-all and streams output."""
    return _stream_job(
        [str(_BUNDLE_DIR / "finance_analyser.py"), "--recategorise-all"],
        "Recategorisation complete.",
        "Recategorisation failed",
    )


@app.route("/api/run-metrics")
def api_run_metrics():
    """Return latest import run error counts as JSON."""
    data = _load_run_metrics()
    cfg = _load_config()
    try:
        from src.loans import load_loans, find_unlinked_loan_transactions
        with open_db(cfg) as conn:
            init_db(conn)
            raw_loans = load_loans(cfg).get("loans", [])
            data["unlinked_loans"] = len(find_unlinked_loan_transactions(conn, raw_loans))
    except Exception as exc:
        logger.warning(f"run-metrics: unlinked loans check failed: {exc}")
        data["unlinked_loans"] = 0
    try:
        from src.commitment_detector import load_commitments, get_upcoming
        data["upcoming_due_soon"] = len(get_upcoming(load_commitments(cfg), days_ahead=5))
    except Exception as exc:
        logger.warning(f"run-metrics: upcoming commitments check failed: {exc}")
        data["upcoming_due_soon"] = 0
    try:
        accounts = _get_account_staleness(cfg)
        data["stale_accounts"] = sum(
            1 for a in accounts
            if a["status"] in ("stale", "overdue") and not a["is_closed"] and not a["is_cdr"]
        )
    except Exception as exc:
        logger.warning(f"run-metrics: stale accounts check failed: {exc}")
        data["stale_accounts"] = 0
    return jsonify(data)


@app.route("/api/insights")
def api_insights():
    """Return triggered financial insight alerts as JSON."""
    from src.insights import compute_insights
    cfg = _load_config()
    try:
        with open_db(cfg) as conn:
            init_db(conn)
            results = compute_insights(conn, cfg)
    except Exception as exc:
        logger.warning(f"Insights computation failed: {exc}")
        results = []
    return jsonify(results)


@app.route("/api/pipeline-status")
def api_pipeline_status():
    """Return current background pipeline state as JSON."""
    return jsonify(_pipeline_status)


@app.route("/api/budget-status")
def api_budget_status():
    """Return current-period over/near-budget categories as JSON."""
    return jsonify(_load_budget_status())


@app.route("/api/budgets/periods", methods=["GET"])
def api_get_budget_periods():
    """Return fortnightly period settings {category: "monthly"|"fortnightly"}."""
    from src.budgets import load_period_settings as _lps
    return jsonify(_lps(_load_config()))


@app.route("/api/budgets/periods", methods=["POST"])
def api_save_budget_periods():
    """Save fortnightly period settings. Body: {category: "monthly"|"fortnightly", ...}."""
    from src.budgets import save_period_settings as _sps
    body = request.get_json(force=True) or {}
    _sps(body, _load_config())
    return jsonify({"ok": True})


@app.route("/api/anomaly/run", methods=["POST"])
def api_run_anomaly_detection():
    """Re-run anomaly detection over all transactions."""
    from src.anomaly_detector import detect_anomalies as _detect
    cfg = _load_config()
    with open_db(cfg) as conn:
        init_db(conn)
        newly_flagged = _detect(conn)
    return jsonify({"ok": True, "newly_flagged": len(newly_flagged)})


@app.route("/api/anomaly/summary")
def api_anomaly_summary():
    """Return list of anomalous transactions."""
    from src.anomaly_detector import anomaly_summary as _summary
    cfg = _load_config()
    with open_db(cfg) as conn:
        init_db(conn)
        return jsonify({"ok": True, "anomalies": _summary(conn)})


@app.route("/api/txn/<txn_id>/receipt", methods=["POST"])
def api_upload_receipt(txn_id: str):
    """Attach a receipt file to a transaction. Multipart: field name 'receipt'."""
    cfg = _load_config()
    if "receipt" not in request.files:
        return jsonify({"ok": False, "error": "no receipt field"}), 400
    f = request.files["receipt"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    receipts_dir = BASE_DIR / "Data" / "Receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(f.filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf", ".heic", ".webp"):
        return jsonify({"ok": False, "error": "unsupported file type"}), 400

    filename = f"{txn_id}{ext}"
    dest = receipts_dir / filename
    f.save(str(dest))
    rel_path = f"Data/Receipts/{filename}"

    with open_db(cfg) as conn:
        init_db(conn)
        conn.execute(
            "UPDATE transactions SET receipt_path = ? WHERE txn_id = ?",
            (rel_path, txn_id),
        )
        conn.commit()

    return jsonify({"ok": True, "path": rel_path})


@app.route("/api/txn/<txn_id>/receipt", methods=["DELETE"])
def api_delete_receipt(txn_id: str):
    """Remove the receipt attachment for a transaction."""
    cfg = _load_config()
    with open_db(cfg) as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT receipt_path FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        if not row or not row[0]:
            return jsonify({"ok": False, "error": "no receipt"}), 404
        path = BASE_DIR / row[0]
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        conn.execute(
            "UPDATE transactions SET receipt_path = NULL WHERE txn_id = ?", (txn_id,)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/txn/<txn_id>/receipt", methods=["GET"])
def api_get_receipt(txn_id: str):
    """Serve the receipt file for a transaction."""
    from flask import send_file
    cfg = _load_config()
    with open_db(cfg) as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT receipt_path FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
    if not row or not row[0]:
        return jsonify({"error": "no receipt"}), 404
    path = BASE_DIR / row[0]
    if not path.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(path))


@app.route("/reconciliation")
def reconciliation_page():
    """Account reconciliation — compare DB running balance against statement snapshots."""
    from src.balance_tracker import load_balance_history as _lbh
    cfg = _load_config()
    balances_df = _lbh(cfg)

    accounts_cfg = cfg.get("accounts", {})
    results: list[dict] = []

    with open_db(cfg) as conn:
        init_db(conn)
        acct_rows = conn.execute(
            "SELECT DISTINCT account FROM transactions WHERE account IS NOT NULL ORDER BY account"
        ).fetchall()
        account_names = [r[0] for r in acct_rows]

    for acct in account_names:
        with open_db(cfg) as conn:
            # Sum all transactions for this account (running total from DB)
            db_total = conn.execute(
                "SELECT SUM(amount) FROM transactions WHERE account = ? "
                "AND (is_split_parent = 0 OR is_split_parent IS NULL)",
                (acct,),
            ).fetchone()[0] or 0.0

        # Latest balance snapshot for this account
        if balances_df is not None and not balances_df.empty:
            snap = balances_df[balances_df["account"] == acct]
        else:
            snap = None

        if snap is not None and not snap.empty:
            latest = snap.sort_values("date").iloc[-1]
            snap_balance = float(latest["balance"])
            snap_date = str(latest["date"])[:10]
            # Compute DB balance as of the snapshot date
            with open_db(cfg) as conn:
                db_as_of = conn.execute(
                    "SELECT SUM(amount) FROM transactions WHERE account = ? AND date <= ? "
                    "AND (is_split_parent = 0 OR is_split_parent IS NULL)",
                    (acct, snap_date),
                ).fetchone()[0] or 0.0
            variance = round(snap_balance - db_as_of, 2)
            status = "ok" if abs(variance) < 0.02 else ("warning" if abs(variance) < 10 else "error")
        else:
            snap_balance = None
            snap_date = None
            db_as_of = db_total
            variance = None
            status = "no_snapshot"

        acct_cfg = accounts_cfg.get(acct, {})
        results.append({
            "account":       acct,
            "friendly_name": acct_cfg.get("friendly_name", acct),
            "snap_balance":  snap_balance,
            "snap_date":     snap_date,
            "db_balance":    round(db_as_of, 2),
            "variance":      variance,
            "status":        status,
            "db_total":      round(db_total, 2),
        })

    results.sort(key=lambda r: (r["status"] != "error", r["status"] != "warning", r["account"]))
    return render_template(
        "reconciliation.html",
        active_section="data",
        active_tab="reconciliation",
        results=results,
    )


@app.route("/api/update-check")
def api_update_check():
    """Return update availability info (populated by background thread at startup)."""
    return jsonify(_UPDATE_INFO if _UPDATE_INFO else {"has_update": False, "current": _APP_VERSION})


@app.route("/recommendations")
@require_module("recommendations")
def recommendations_page():
    from datetime import date
    from src.utils import md_to_html as _md_to_html
    md_path = REPORTS_DIR / "recommendations.md"
    if md_path.exists():
        content_html = _md_to_html(md_path.read_text(encoding="utf-8"))
    else:
        content_html = "<p><em>No recommendations generated yet. Upload a statement file to trigger an import.</em></p>"
    return render_template(
        "recommendations.html",
        content=content_html,
        today=date.today().isoformat(),
        active_tab="recommendations",
    )


@app.route("/superseded-pairs")
@require_module("transfers")
def superseded_pairs_page():
    pairs = _load_superseded()
    return render_template("superseded_pairs.html", pairs=pairs, cat_groups=_CAT_GROUPS, active_tab="superseded_pairs")


@app.route("/api/superseded-pairs", methods=["GET"])
def api_get_superseded_pairs():
    return jsonify({"pairs": _load_superseded()})


@app.route("/api/superseded-pairs", methods=["POST"])
def api_add_superseded_pair():
    from datetime import date as _date
    data = request.get_json()
    replaced = (data.get("replaced") or "").strip().upper()
    by = (data.get("by") or "").strip().upper()
    category = (data.get("category") or "").strip()
    note = (data.get("note") or "").strip()
    if not replaced or not by:
        return jsonify({"ok": False, "error": "replaced and by are required"}), 400
    pairs = _load_superseded()
    if any(p["replaced"] == replaced and p["by"] == by for p in pairs):
        return jsonify({"ok": False, "error": "pair already exists"}), 400
    new_id = max((p.get("id", 0) for p in pairs), default=0) + 1
    pairs.append({
        "id": new_id,
        "replaced": replaced,
        "by": by,
        "category": category,
        "note": note,
        "added": _date.today().isoformat(),
    })
    _save_superseded(pairs)
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/superseded-pairs/<int:pair_id>", methods=["DELETE"])
def api_delete_superseded_pair(pair_id: int):
    pairs = _load_superseded()
    before = len(pairs)
    pairs = [p for p in pairs if p.get("id") != pair_id]
    if len(pairs) == before:
        return jsonify({"ok": False, "error": "pair not found"}), 404
    _save_superseded(pairs)
    return jsonify({"ok": True})


@app.route("/merchant-rules")
@app.route("/settings/merchant-rules")
def merchant_rules_page():
    cfg = _load_config()
    builtin = cfg.get("merchant_categories", {})
    user = _load_rules()
    all_rules = [_rule_obj(m, v, "custom") for m, v in sorted(user.items())]
    all_rules += [
        _rule_obj(m, v, "builtin")
        for m, v in sorted(builtin.items())
        if m not in user
    ]
    return render_template("merchant_rules.html", cat_groups=_CAT_GROUPS, rules=all_rules,
                           subcats=_get_merged_subcats(),
                           active_tab="merchant_rules", active_section="settings")


# ── Settings — Accounts ───────────────────────────────────────────────────────

@app.route("/settings")
def settings_index():
    from flask import redirect
    return redirect("/settings/accounts")


@app.route("/settings/accounts")
def settings_accounts():
    cfg = _load_config()
    try:
        with open_db(cfg) as conn:
            init_db(conn)
            seed_accounts(conn, cfg)
    except Exception as exc:
        logger.warning(f"Account seeding failed: {exc}")
    accounts = get_accounts(cfg)
    return render_template("settings/accounts.html", accounts=accounts,
                           active_tab="accounts", active_section="settings")


@app.route("/api/accounts", methods=["GET"])
def api_get_accounts():
    cfg = _load_config()
    return jsonify(get_accounts(cfg))


@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    account_name = (body.get("account_name") or "").strip()
    if not account_name:
        return jsonify({"ok": False, "error": "account_name is required"}), 400
    fields = {k: v for k, v in body.items() if k != "account_name"}
    upsert_account(cfg, account_name, fields)
    return jsonify({"ok": True})


@app.route("/api/accounts/<path:account_name>", methods=["PUT"])
def api_update_account(account_name: str):
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    upsert_account(cfg, account_name, body)
    return jsonify({"ok": True})


# ── First-run setup wizard ───────────────────────────────────────────────────

@app.route("/setup", methods=["GET"])
def setup_page():
    from src.modules import DEFAULT_MODULES as _DM
    # Wizard only shown once — redirect away if already complete
    modules_path = _data_path("modules_file", "Data/modules.json")
    if modules_path.exists():
        return redirect("/")
    return render_template("setup.html", module_keys=list(_DM.keys()))


@app.route("/setup", methods=["POST"])
def setup_submit():
    from src.modules import save_modules as _sm, DEFAULT_MODULES as _DM
    cfg = _load_config()
    # Persist display name if provided
    user_name = request.form.get("user_name", "").strip()
    if user_name:
        _save_user_settings({"user_name": user_name})
    modules = {k: (k in request.form) for k in _DM}
    cfg.setdefault("data", {})["modules_file"] = str(
        _data_path("modules_file", "Data/modules.json")
    )
    _sm(modules, cfg)
    return redirect("/")


@app.route("/api/setup/test-api-key")
def api_test_api_key():
    """Check whether ANTHROPIC_API_KEY env var is set and reachable."""
    import os as _os
    key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY environment variable is not set."})
    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return jsonify({"ok": True, "message": "API key is valid and connected."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


# ── Settings — Budgets ────────────────────────────────────────────────────────

@app.route("/settings/budgets")
@require_module("budgets")
def settings_budgets():
    from src.budgets import load_budgets as _lb, load_rollover_settings as _lrs, load_period_settings as _lps
    from src.utils import SUBCATS as _SUBCATS
    cfg = _load_config()
    current = _lb(cfg)
    rollover = _lrs(cfg)
    period_settings = _lps(cfg)
    _SKIP = {"Transfers", "Investment", "Family Loan Repayment", "Family Loan Received",
             "Income", "Interest Income", "Gifts Received", "Business Reimbursement"}
    all_cats = sorted(c for c in _SUBCATS if c not in _SKIP)
    status = _load_budget_status()
    burn = {e["category"]: e for e in status.get("all", [])}
    return render_template(
        "settings/budgets.html",
        current_budgets=current,
        rollover_settings=rollover,
        period_settings=period_settings,
        all_cats=all_cats,
        burn_rate=burn,
        active_tab="budgets",
        active_section="settings",
    )


@app.route("/api/budgets", methods=["GET"])
def api_get_budgets():
    from src.budgets import load_budgets as _lb
    cfg = _load_config()
    return jsonify({"budgets": _lb(cfg)})


@app.route("/api/budgets", methods=["POST"])
def api_save_budgets():
    from src.budgets import save_budgets as _sb
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    raw = body.get("budgets", {})
    budgets = {}
    for cat, val in raw.items():
        try:
            v = float(val)
            if v > 0:
                budgets[cat] = v
        except (TypeError, ValueError):
            pass
    raw_rollover = body.get("rollover")
    rollover = {k: bool(v) for k, v in raw_rollover.items()} if isinstance(raw_rollover, dict) else None
    _sb(budgets, cfg, rollover=rollover)
    return jsonify({"ok": True, "saved": len(budgets)})


@app.route("/api/budgets/suggest")
def api_suggest_budgets():
    from src.budgets import suggest_budgets as _sg
    cfg = _load_config()
    try:
        months = max(1, min(36, int(request.args.get("months", 3))))
    except (TypeError, ValueError):
        months = 3
    try:
        from src.db import open_db as _open_db, init_db as _init_db
        with _open_db(cfg) as conn:
            _init_db(conn)
            result = _sg(conn, months=months)
        return jsonify({"ok": True, "suggestions": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/settings/modules")
def settings_modules():
    from src.modules import load_modules as _lm, DEFAULT_MODULES as _DEFAULT_MODULES
    cfg = _load_config()
    current = _lm(cfg)
    return render_template(
        "settings/modules.html",
        modules=current,
        module_keys=list(_DEFAULT_MODULES.keys()),
        active_tab="modules",
        active_section="settings",
    )


@app.route("/api/modules", methods=["POST"])
def api_save_modules():
    from src.modules import save_modules as _sm, DEFAULT_MODULES as _DEFAULT_MODULES
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    raw = body.get("modules", {})
    modules = {k: bool(raw.get(k, False)) for k in _DEFAULT_MODULES}
    _sm(modules, cfg)
    return jsonify({"ok": True, "saved": sum(1 for v in modules.values() if v)})


@app.route("/settings/bank-profiles")
def settings_bank_profiles():
    from src.bank_profiles import load_profiles as _lbp
    cfg = _load_config()
    raw = _lbp(cfg)
    profiles = [{"key": k, **v} for k, v in raw.items()]
    return render_template(
        "settings/bank_profiles.html",
        active_section="settings",
        active_tab="bank_profiles",
        profiles=profiles,
    )


@app.route("/api/bank-profiles", methods=["POST"])
def api_save_bank_profile():
    from src.bank_profiles import save_profile as _sbp, headers_key as _hkey
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    raw_headers = body.get("headers_csv", "")
    if not raw_headers:
        return jsonify({"error": "headers_csv is required"}), 400
    hdrs = [h.strip() for h in raw_headers.split(",") if h.strip()]
    if not hdrs:
        return jsonify({"error": "no valid headers found"}), 400
    key = _hkey(hdrs)
    profile = {
        "bank_name":        body.get("bank_name", ""),
        "display_name":     body.get("display_name", ""),
        "account_type":     body.get("account_type", "transaction"),
        "date_col":         body.get("date_col", ""),
        "date_format":      body.get("date_format", ""),
        "amount_col":       body.get("amount_col", ""),
        "credit_col":       body.get("credit_col", ""),
        "debit_col":        body.get("debit_col", ""),
        "description_col":  body.get("description_col", ""),
        "negate_amounts":   bool(body.get("negate_amounts", False)),
        "skip_rows":        int(body.get("skip_rows", 0)),
        "headers_csv":      raw_headers,
        "created_at":       str(_date.today()),
    }
    _sbp(key, profile, cfg)
    return jsonify({"ok": True, "key": key})


@app.route("/api/bank-profiles/<key>", methods=["DELETE"])
def api_delete_bank_profile(key: str):
    from src.bank_profiles import delete_profile as _dbp
    cfg = _load_config()
    found = _dbp(key, cfg)
    return jsonify({"ok": found})


@app.route("/settings/encryption")
def settings_encryption():
    from src.db_crypto import encryption_status as _enc_status
    cfg = _load_config()
    status = _enc_status(cfg)
    return render_template(
        "settings/encryption.html",
        active_section="settings",
        active_tab="encryption",
        status=status,
    )


@app.route("/api/encryption/passphrase", methods=["POST"])
def api_set_encryption_passphrase():
    from src.db_crypto import set_passphrase as _sp, encryption_status as _enc_status
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    phrase = (body.get("passphrase") or "").strip()
    if not phrase:
        return jsonify({"error": "passphrase is required"}), 400
    saved_to = _sp(phrase, cfg)
    status = _enc_status(cfg)
    return jsonify({"ok": True, "saved_to": saved_to, "status": status})


@app.route("/api/encryption/passphrase", methods=["DELETE"])
def api_delete_encryption_passphrase():
    from src.db_crypto import delete_passphrase as _dp, encryption_status as _enc_status
    cfg = _load_config()
    _dp(cfg)
    status = _enc_status(cfg)
    return jsonify({"ok": True, "status": status})


@app.route("/api/encryption/migrate", methods=["POST"])
def api_encrypt_database():
    """Encrypt the existing plain database. Requires SQLCipher + passphrase set."""
    from src.db_crypto import (
        encryption_status as _enc_status,
        encrypt_existing_db as _enc_db,
        get_passphrase as _gp,
        sqlcipher_available as _sc_ok,
    )
    cfg = _load_config()
    if not _sc_ok():
        return jsonify({"error": "sqlcipher3 is not installed"}), 400
    phrase = _gp(cfg)
    if not phrase:
        return jsonify({"error": "no passphrase set — set one first"}), 400
    try:
        from src.db import _db_path
        db_path = _db_path(cfg)
        if not db_path.exists():
            return jsonify({"error": f"database not found: {db_path}"}), 404
        _enc_db(db_path, phrase)
        logger.info(f"Database encrypted: {db_path}")
        return jsonify({"ok": True, "path": str(db_path), "status": _enc_status(cfg)})
    except Exception as exc:
        logger.error(f"Database encryption failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/encryption/decrypt", methods=["POST"])
def api_decrypt_database():
    """Decrypt the database back to plain SQLite and remove the passphrase."""
    from src.db_crypto import (
        encryption_status as _enc_status,
        decrypt_existing_db as _dec_db,
        delete_passphrase as _del_p,
        get_passphrase as _gp,
        sqlcipher_available as _sc_ok,
    )
    cfg = _load_config()
    if not _sc_ok():
        return jsonify({"error": "sqlcipher3 is not installed"}), 400
    phrase = _gp(cfg)
    if not phrase:
        return jsonify({"error": "no passphrase set — database may not be encrypted"}), 400
    try:
        from src.db import _db_path
        db_path = _db_path(cfg)
        _dec_db(db_path, phrase)
        _del_p(cfg)
        logger.info(f"Database decrypted: {db_path}")
        return jsonify({"ok": True, "status": _enc_status(cfg)})
    except Exception as exc:
        logger.error(f"Database decryption failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/detect-csv-columns", methods=["POST"])
def api_detect_csv_columns():
    """Call Claude Haiku to suggest column mapping for an unknown CSV."""
    cfg = _load_config()
    body = request.get_json(silent=True) or {}
    csv_text = body.get("csv_text", "").strip()
    if not csv_text:
        return jsonify({"error": "csv_text is required"}), 400

    lines = [ln for ln in csv_text.splitlines() if ln.strip()][:6]
    if not lines:
        return jsonify({"error": "no content"}), 400

    try:
        from src.ai_backend import get_backend as _get_ai
        client = _get_ai(cfg)
        model = (cfg.get("models") or {}).get("categoriser", "claude-haiku-4-5-20251001")

        sample = "\n".join(lines)
        prompt = (
            "You are a bank statement column mapper. Given CSV headers and sample rows, "
            "identify which columns contain transaction data.\n\n"
            f"CSV sample:\n{sample}\n\n"
            "Respond with JSON only (no prose):\n"
            '{"date_col":"<column>","date_format":"<strftime format>","amount_col":"<column or null>",'
            '"credit_col":"<column or null>","debit_col":"<column or null>",'
            '"description_col":"<column>","negate_amounts":<true|false>}\n\n'
            "Rules:\n"
            "- If a single signed amount column exists, use amount_col and set credit_col/debit_col to null.\n"
            "- If separate credit/debit columns exist, set credit_col and debit_col; set amount_col to null.\n"
            "- negate_amounts=true only when debits appear as positive numbers in the CSV.\n"
            "- date_format must be a Python strftime string such as %d/%m/%Y or %Y-%m-%d."
        )
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = "\n".join(
                ln for ln in text.splitlines()
                if not ln.strip().startswith("```")
            )
        suggestion = json.loads(text)
        return jsonify({"ok": True, "suggestion": suggestion})
    except Exception as exc:
        logger.warning(f"AI column detection failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/merchant-rules", methods=["GET"])
def api_get_merchant_rules():
    cfg = _load_config()
    builtin = cfg.get("merchant_categories", {})
    user = _load_rules()
    rules = [_rule_obj(m, v, "custom") for m, v in sorted(user.items())]
    rules += [
        _rule_obj(m, v, "builtin")
        for m, v in sorted(builtin.items())
        if m not in user
    ]
    return jsonify({"rules": rules})


def _load_custom_subcats() -> dict:
    p = _data_path("custom_subcats_file", "Data/custom_subcats.json")
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _get_merged_subcats() -> dict:
    from src.utils import SUBCATS
    custom = _load_custom_subcats()
    merged = {cat: list(items) for cat, items in SUBCATS.items()}
    for cat, items in custom.items():
        if cat in merged:
            for item in items:
                if item not in merged[cat]:
                    merged[cat].insert(-1, item)  # before "Other"
        else:
            merged[cat] = items
    return merged


@app.route("/api/subcats", methods=["GET"])
def api_get_subcats():
    return jsonify(_get_merged_subcats())


@app.route("/api/subcats/add", methods=["POST"])
def api_add_subcat():
    data = request.get_json()
    category = (data.get("category") or "").strip()
    subcat   = (data.get("subcat")   or "").strip()
    if not category or not subcat:
        return jsonify({"ok": False, "error": "category and subcat required"}), 400
    p = _data_path("custom_subcats_file", "Data/custom_subcats.json")
    custom = _load_custom_subcats()
    custom.setdefault(category, [])
    if subcat not in custom[category]:
        custom[category].append(subcat)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(custom, indent=2), "utf-8")
    return jsonify({"ok": True, "subcats": _get_merged_subcats()})


@app.route("/api/merchant-rules", methods=["POST"])
def api_add_merchant_rule():
    data = request.get_json()
    merchant = (data.get("merchant") or "").strip().upper()
    category = (data.get("category") or "").strip()
    sub_category = (data.get("sub_category") or "").strip()
    if not merchant or not category:
        return jsonify({"ok": False, "error": "merchant and category required"}), 400
    if category not in _VALID_CATEGORIES:
        return jsonify({"ok": False, "error": f"unknown category: {category}"}), 400
    try:
        rules = _load_rules()
        rules[merchant] = (
            {"category": category, "sub_category": sub_category}
            if sub_category else category
        )
        _save_rules(rules)

        # immediately backfill matching historical transactions
        backfilled = 0
        try:
            _cfg = _load_config()
            with open_db(_cfg) as conn:
                init_db(conn)
                ids = [r["txn_id"] for r in conn.execute(
                    "SELECT txn_id FROM transactions WHERE instr(upper(trim(description)), ?) > 0",
                    (merchant,),
                ).fetchall()]
                if ids:
                    _SAFE_COLS = frozenset({"category", "sub_category"})
                    fields = {k: v for k, v in {
                        "category": category, "sub_category": sub_category or None,
                    }.items() if v is not None and k in _SAFE_COLS}
                    ph = ",".join("?" * len(ids))
                    set_clause = ", ".join(f"{k} = ?" for k in fields)
                    conn.execute(
                        f"UPDATE transactions SET {set_clause} WHERE txn_id IN ({ph})",
                        list(fields.values()) + ids,
                    )
                    conn.commit()
                    backfilled = len(ids)
        except Exception as _exc:
            app.logger.warning("Merchant rule backfill failed: %s", _exc)

        return jsonify({"ok": True, "backfilled": backfilled})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/merchant-rules", methods=["DELETE"])
def api_delete_merchant_rule():
    data = request.get_json()
    merchant = (data.get("merchant") or "").strip().upper()
    if not merchant:
        return jsonify({"ok": False, "error": "merchant required"}), 400
    try:
        rules = _load_rules()
        if merchant not in rules:
            return jsonify({"ok": False, "error": "rule not found"}), 404
        del rules[merchant]
        _save_rules(rules)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/commitments")
@require_module("commitments")
def commitments_page():
    from datetime import date, timedelta
    from itertools import groupby as _groupby
    from src.commitment_detector import (
        load_commitments, monthly_committed_total, get_upcoming, FREQUENCY_LABELS,
    )
    cfg = _load_config()

    commitments = load_commitments(cfg)
    items = commitments.get("items", [])

    # Recompute next_due for each item so it's always current
    from src.commitment_detector import _next_due as _cnd
    today = date.today()
    for item in items:
        freq = item.get("frequency", "monthly")
        seed_str = item.get("next_due") or item.get("last_seen", "")
        try:
            seed = date.fromisoformat(seed_str)
            if seed <= today:
                item["next_due"] = _cnd(seed, freq).isoformat()
        except (ValueError, TypeError):
            pass
        item["freq_label"] = FREQUENCY_LABELS.get(freq, freq.title())

    monthly_total = monthly_committed_total(commitments)
    active_count  = sum(1 for i in items if i.get("active", True))
    total_count   = len(items)

    days_ahead = max(1, min(365, request.args.get("days", 90, type=int)))

    # Build grouped upcoming timeline
    upcoming_flat = get_upcoming(commitments, days_ahead=days_ahead)
    for u in upcoming_flat:
        pd_date = date.fromisoformat(u["projected_date"])
        u["days_away"]  = (pd_date - today).days
        u["day_label"]  = pd_date.strftime("%d %b")
        u["freq_label"] = FREQUENCY_LABELS.get(u.get("frequency", "monthly"), "")

    grouped_upcoming = []
    for month_key, grp in _groupby(upcoming_flat, key=lambda x: x["projected_date"][:7]):
        grp = list(grp)
        mo_date = date.fromisoformat(month_key + "-01")
        grouped_upcoming.append({
            "label": mo_date.strftime("%B %Y"),
            "total": sum(i["amount"] for i in grp),
            "entries": grp,
        })

    next_item = upcoming_flat[0] if upcoming_flat else None

    return render_template(
        "commitments.html",
        items=items,
        grouped_upcoming=grouped_upcoming,
        upcoming_count=len(upcoming_flat),
        monthly_total=monthly_total,
        active_count=active_count,
        total_count=total_count,
        next_item=next_item,
        days_ahead=days_ahead,
        cat_groups=_CAT_GROUPS,
        active_tab="commitments",
    )


@app.route("/api/commitments", methods=["GET"])
def api_get_commitments():
    from src.commitment_detector import load_commitments
    cfg = _load_config()
    data = load_commitments(cfg)
    return jsonify({"ok": True, "items": data.get("items", [])})


@app.route("/api/commitments", methods=["POST"])
def api_save_commitment():
    from datetime import date
    import uuid as _uuid
    from src.commitment_detector import load_commitments, save_commitments, FREQUENCIES, _next_due as _cnd
    cfg = _load_config()

    data    = request.get_json()
    item_id = (data.get("id") or "").strip()
    name    = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid amount"}), 400

    freq = data.get("frequency", "monthly")
    if freq not in FREQUENCIES:
        return jsonify({"ok": False, "error": f"unknown frequency: {freq}"}), 400

    # Resolve next_due
    next_due_str = (data.get("next_due") or "").strip()
    if not next_due_str:
        next_due_str = _cnd(date.today(), freq).isoformat()

    commitments = load_commitments(cfg)
    items = commitments.get("items", [])

    if item_id:
        # Update existing
        match = next((i for i in items if i["id"] == item_id), None)
        if not match:
            return jsonify({"ok": False, "error": "item not found"}), 404
        match.update({
            "name":      name,
            "amount":    round(amount, 2),
            "frequency": freq,
            "category":  data.get("category", match.get("category", "Housing")),
            "next_due":  next_due_str,
            "account":   (data.get("account") or "").strip(),
            "notes":     (data.get("notes") or "").strip(),
        })
    else:
        # Add new
        import hashlib
        new_id = hashlib.md5(f"{name}{amount}{freq}".encode()).hexdigest()[:12]
        items.append({
            "id":        new_id,
            "name":      name,
            "amount":    round(amount, 2),
            "frequency": freq,
            "category":  data.get("category", "Housing"),
            "next_due":  next_due_str,
            "account":   (data.get("account") or "").strip(),
            "notes":     (data.get("notes") or "").strip(),
            "source":    data.get("source", "manual"),
            "active":    True,
        })

    save_commitments({"items": items}, cfg)
    return jsonify({"ok": True})


@app.route("/api/commitments", methods=["DELETE"])
def api_delete_commitment():
    from src.commitment_detector import load_commitments, save_commitments
    cfg = _load_config()
    data    = request.get_json()
    item_id = (data.get("id") or "").strip()
    if not item_id:
        return jsonify({"ok": False, "error": "id required"}), 400
    commitments = load_commitments(cfg)
    items = commitments.get("items", [])
    before = len(items)
    items = [i for i in items if i["id"] != item_id]
    if len(items) == before:
        return jsonify({"ok": False, "error": "item not found"}), 404
    save_commitments({"items": items}, cfg)
    return jsonify({"ok": True})


@app.route("/api/commitments/toggle", methods=["POST"])
def api_toggle_commitment():
    from src.commitment_detector import load_commitments, save_commitments
    cfg = _load_config()
    data    = request.get_json()
    item_id = (data.get("id") or "").strip()
    active  = bool(data.get("active", True))
    commitments = load_commitments(cfg)
    match = next((i for i in commitments.get("items", []) if i["id"] == item_id), None)
    if not match:
        return jsonify({"ok": False, "error": "item not found"}), 404
    match["active"] = active
    save_commitments(commitments, cfg)
    return jsonify({"ok": True})


@app.route("/subscriptions")
@require_module("commitments")
def subscriptions_page():
    from datetime import date
    from src.commitment_detector import (
        load_commitments, FREQUENCY_LABELS, MONTHLY_FACTORS as _MONTHLY_FACTORS, _next_due as _cnd,
    )
    cfg = _load_config()
    commitments = load_commitments(cfg)
    items = commitments.get("items", [])

    today = date.today()
    for item in items:
        freq = item.get("frequency", "monthly")
        seed_str = item.get("next_due") or item.get("last_seen", "")
        try:
            seed = date.fromisoformat(seed_str)
            if seed <= today:
                item["next_due"] = _cnd(seed, freq).isoformat()
        except (ValueError, TypeError):
            pass
        item["freq_label"] = FREQUENCY_LABELS.get(freq, freq.title())
        factor = _MONTHLY_FACTORS.get(freq, 1.0)
        item["monthly_cost"] = round(float(item.get("amount", 0)) * factor, 2)
        item["annual_cost"]  = round(item["monthly_cost"] * 12, 2)

    active_items     = [i for i in items if i.get("active", True)]
    cancelled_items  = [i for i in items if not i.get("active", True)]
    active_items.sort(key=lambda i: i["monthly_cost"], reverse=True)
    cancelled_items.sort(key=lambda i: i["monthly_cost"], reverse=True)

    total_monthly = round(sum(i["monthly_cost"] for i in active_items), 2)
    total_annual  = round(total_monthly * 12, 2)

    return render_template(
        "subscriptions.html",
        active_items=active_items,
        cancelled_items=cancelled_items,
        total_monthly=total_monthly,
        total_annual=total_annual,
        cat_groups=_CAT_GROUPS,
        active_tab="subscriptions",
    )


_LOAN_EDIT_KEYS = {
    "loan_id", "name", "direction", "counterparty", "contact_name", "principal",
    "start_date", "category_filter", "description_filter", "receipt_filter", "notes",
    "linked_receipt_txn_ids", "linked_repayment_txn_ids",
}


@app.route("/financial-goals")
@require_module("goals")
def financial_goals_page():
    from src.financial_goals import (
        load_goals, monthly_savings_total, get_upcoming_milestones, GOAL_CATEGORIES,
    )
    from src.loans import load_loans, calculate_loan_position, find_unlinked_loan_transactions

    cfg   = _load_config()
    goals = load_goals(cfg)
    items = goals.get("items", [])

    with open_db(cfg) as conn:
        init_db(conn)

        # Auto-balance: sum of credits to the linked account since goal creation
        from src.financial_goals import calculate_goal_balance as _calc_goal_balance
        for item in items:
            item["auto_current"] = _calc_goal_balance(item, conn)

        milestones    = get_upcoming_milestones(goals, days=365)
        monthly_total = monthly_savings_total(goals)
        active_count  = sum(1 for i in items if i.get("active", True))
        total_count   = len(items)
        total_saved   = sum(float(i.get("current_amount", 0) or 0) for i in items if i.get("active", True))
        total_target  = sum(float(i.get("target_amount", 0) or 0) for i in items if i.get("active", True))
        nearest       = next((m for m in milestones if m.get("pct", 0) < 100), None)

        raw_loans = load_loans(cfg).get("loans", [])
        loans: list[dict] = []
        for loan in raw_loans:
            pos = calculate_loan_position(loan, conn)
            merged = {**loan, **pos}
            merged["edit_data"] = {k: v for k, v in loan.items() if k in _LOAN_EDIT_KEYS}
            loans.append(merged)
        unlinked_txns  = find_unlinked_loan_transactions(conn, raw_loans)

    loans_owe_total  = sum(l["outstanding"] for l in loans if l.get("direction") == "owed_by_me")
    loans_lent_total = sum(l["outstanding"] for l in loans if l.get("direction") == "owed_to_me")
    loans_active     = sum(1 for l in loans if l.get("status") != "complete")
    loan_contacts    = [c["name"] for c in cfg.get("family_loans", {}).get("contacts", [])]

    return render_template(
        "financial_goals.html",
        items=items,
        milestones=milestones,
        monthly_total=monthly_total,
        active_count=active_count,
        total_count=total_count,
        total_saved=total_saved,
        total_target=total_target,
        nearest=nearest,
        goal_categories=GOAL_CATEGORIES,
        active_tab="goals",
        loans=loans,
        loans_owe_total=round(loans_owe_total, 2),
        loans_lent_total=round(loans_lent_total, 2),
        loans_active=loans_active,
        unlinked_count=len(unlinked_txns),
        unlinked_txns=unlinked_txns,
        loan_contacts=loan_contacts,
    )


@app.route("/api/financial-goals", methods=["GET"])
def api_get_financial_goals():
    from src.financial_goals import load_goals
    return jsonify({"ok": True, "items": load_goals(_load_config()).get("items", [])})


@app.route("/api/financial-goals", methods=["POST"])
def api_save_financial_goal():
    import hashlib
    from datetime import date as _date
    from src.financial_goals import load_goals, save_goals, FREQUENCIES
    cfg  = _load_config()
    data = request.get_json()

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        target = float(data.get("target_amount", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid target_amount"}), 400

    freq = data.get("frequency", "monthly")
    if freq not in FREQUENCIES:
        return jsonify({"ok": False, "error": f"unknown frequency: {freq}"}), 400

    goals    = load_goals(cfg)
    items    = goals.get("items", [])
    item_id  = (data.get("id") or "").strip()

    if item_id:
        match = next((i for i in items if i["id"] == item_id), None)
        if not match:
            return jsonify({"ok": False, "error": "goal not found"}), 404
        match.update({
            "name":                name,
            "category":            data.get("category", match.get("category", "Savings")),
            "target_amount":       round(target, 2),
            "current_amount":      round(float(data.get("current_amount", 0) or 0), 2),
            "contribution_amount": round(float(data.get("contribution_amount", 0) or 0), 2),
            "frequency":           freq,
            "target_date":         (data.get("target_date") or "").strip() or None,
            "account":             (data.get("account") or "").strip(),
            "notes":               (data.get("notes") or "").strip(),
        })
    else:
        new_id = hashlib.md5(f"{name}{target}{_date.today()}".encode()).hexdigest()[:12]
        items.append({
            "id":                  new_id,
            "name":                name,
            "category":            data.get("category", "Savings"),
            "target_amount":       round(target, 2),
            "current_amount":      round(float(data.get("current_amount", 0) or 0), 2),
            "contribution_amount": round(float(data.get("contribution_amount", 0) or 0), 2),
            "frequency":           freq,
            "target_date":         (data.get("target_date") or "").strip() or None,
            "account":             (data.get("account") or "").strip(),
            "notes":               (data.get("notes") or "").strip(),
            "active":              True,
            "created_date":        _date.today().isoformat(),
        })

    save_goals({"items": items}, cfg)
    return jsonify({"ok": True})


@app.route("/api/financial-goals/<goal_id>", methods=["DELETE"])
def api_delete_financial_goal(goal_id):
    from src.financial_goals import load_goals, save_goals
    cfg   = _load_config()
    goals = load_goals(cfg)
    items = goals.get("items", [])
    before = len(items)
    items = [i for i in items if i["id"] != goal_id]
    if len(items) == before:
        return jsonify({"ok": False, "error": "goal not found"}), 404
    save_goals({"items": items}, cfg)
    return jsonify({"ok": True})


@app.route("/api/loans", methods=["GET"])
def api_get_loans():
    from src.loans import load_loans
    return jsonify({"ok": True, "loans": load_loans(_load_config()).get("loans", [])})


@app.route("/api/loans", methods=["POST"])
def api_save_loan():
    import hashlib
    from datetime import date as _date
    from src.loans import load_loans, save_loans

    cfg  = _load_config()
    data = request.get_json()

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        principal = float(data.get("principal", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid principal"}), 400
    if principal <= 0:
        return jsonify({"ok": False, "error": "principal must be greater than zero"}), 400

    direction = data.get("direction", "owed_by_me")
    if direction not in ("owed_by_me", "owed_to_me"):
        return jsonify({"ok": False, "error": "invalid direction"}), 400

    loans_data = load_loans(cfg)
    loans      = loans_data.get("loans", [])
    loan_id    = (data.get("loan_id") or "").strip()

    fields = {
        "name":                      name,
        "direction":                 direction,
        "counterparty":              (data.get("counterparty") or "").strip(),
        "contact_name":              (data.get("contact_name") or "").strip(),
        "principal":                 round(principal, 2),
        "start_date":                (data.get("start_date") or "").strip() or None,
        "category_filter":           (data.get("category_filter") or "").strip(),
        "description_filter":        (data.get("description_filter") or "").strip(),
        "receipt_filter":            (data.get("receipt_filter") or "").strip(),
        "notes":                     (data.get("notes") or "").strip(),
        "linked_receipt_txn_ids":    [t for t in (data.get("linked_receipt_txn_ids") or []) if t],
        "linked_repayment_txn_ids":  [t for t in (data.get("linked_repayment_txn_ids") or []) if t],
    }

    if loan_id:
        match = next((l for l in loans if l["loan_id"] == loan_id), None)
        if not match:
            return jsonify({"ok": False, "error": "loan not found"}), 404
        match.update(fields)
    else:
        new_id = hashlib.md5(f"{name}{principal}{_date.today()}".encode()).hexdigest()[:12]
        loans.append({"loan_id": new_id, "created_date": _date.today().isoformat(), **fields})

    save_loans({"loans": loans}, cfg)
    return jsonify({"ok": True})



@app.route("/api/loans/<loan_id>", methods=["DELETE"])
def api_delete_loan(loan_id):
    from src.loans import load_loans, save_loans
    cfg        = _load_config()
    loans_data = load_loans(cfg)
    loans      = loans_data.get("loans", [])
    before     = len(loans)
    loans      = [l for l in loans if l["loan_id"] != loan_id]
    if len(loans) == before:
        return jsonify({"ok": False, "error": "loan not found"}), 404
    save_loans({"loans": loans}, cfg)
    return jsonify({"ok": True})


@app.route("/api/loan-candidates", methods=["POST"])
def api_loan_candidates():
    """Return candidate receipts and repayments for a loan contact."""
    from src.loans import get_loan_candidates
    cfg  = _load_config()
    data = request.get_json()
    contact_name = (data.get("contact_name") or "").strip()
    if not contact_name:
        return jsonify({"ok": False, "error": "contact_name required"}), 400
    with open_db(cfg) as conn:
        init_db(conn)
        result = get_loan_candidates(contact_name, cfg, conn)
    return jsonify({"ok": True, **result})


@app.route("/loans/<loan_id>/statement")
@require_module("loans")
def loan_statement_page(loan_id):
    from src.loans import load_loans, calculate_loan_position
    from datetime import date as _date

    cfg       = _load_config()
    raw_loans = load_loans(cfg).get("loans", [])
    loan      = next((l for l in raw_loans if l["loan_id"] == loan_id), None)
    if not loan:
        return "Loan not found", 404

    with open_db(cfg) as conn:
        init_db(conn)
        position = calculate_loan_position(loan, conn)

    return render_template(
        "loan_statement.html",
        loan=loan,
        position=position,
        generated_date=_date.today().isoformat(),
    )


@app.route("/api/transfer-decision", methods=["POST"])
def api_transfer_decision():
    """Confirm or dismiss a detected transfer pair."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500

    data    = request.get_json()
    pair_id = (data.get("pair_id") or "").strip()
    action  = (data.get("action")  or "").strip()   # "confirm" | "dismiss"
    label   = (data.get("label")   or "Family Loan").strip()

    if not pair_id or action not in ("confirm", "dismiss"):
        return jsonify({"ok": False, "error": "pair_id and action required"}), 400

    try:
        from src.transfer_detector import load_transfer_candidates, save_transfer_candidates
        candidates = load_transfer_candidates(config)
        pair = next((p for p in candidates["pairs"] if p["pair_id"] == pair_id), None)
        if not pair:
            return jsonify({"ok": False, "error": "pair not found"}), 404

        pair["status"] = "confirmed" if action == "confirm" else "dismissed"
        pair["label"]  = label
        save_transfer_candidates(candidates, config)

        if action == "confirm":
            from src.review_applier import apply_entries
            if label == "Family Loan":
                # txn_a is always the debit (repayment), txn_b the credit (received)
                entries = [
                    {"txn_id": pair["txn_a"]["txn_id"], "category": "Family Loan Repayment"},
                    {"txn_id": pair["txn_b"]["txn_id"], "category": "Family Loan Received"},
                ]
            else:
                entries = [
                    {"txn_id": pair["txn_a"]["txn_id"], "category": "Transfers"},
                    {"txn_id": pair["txn_b"]["txn_id"], "category": "Transfers"},
                ]
            result = apply_entries(entries, config)
            ids = [pair["txn_a"]["txn_id"], pair["txn_b"]["txn_id"]]
            update_transactions_bulk(ids, {"sub_category": label}, config)

            loan_link_needed = False
            if label == "Family Loan":
                try:
                    from src.loans import auto_link_transfer_pair as _auto_link
                    linked = _auto_link(
                        pair["txn_a"]["txn_id"],
                        pair["txn_a"].get("description", ""),
                        pair["txn_b"]["txn_id"],
                        pair["txn_b"].get("description", ""),
                        config,
                    )
                    loan_link_needed = not linked
                except Exception:
                    loan_link_needed = True

            return jsonify({"ok": True, "loan_link_needed": loan_link_needed, **result})

        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/override-history", methods=["GET"])
def api_override_history():
    """Return recent override batches for the history/undo UI."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    limit = min(int(request.args.get("limit", 20)), 100)
    try:
        from src.override_history import get_history
        batches = get_history(config, limit=limit)
        return jsonify({"ok": True, "batches": batches})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/undo-override", methods=["POST"])
def api_undo_override():
    """Reverse a previously applied override batch."""
    config = _load_config()
    if not config:
        return jsonify({"error": "config.yaml not found"}), 500
    data = request.get_json()
    batch_id = (data.get("batch_id") or "").strip()
    if not batch_id:
        return jsonify({"ok": False, "error": "batch_id required"}), 400
    try:
        from src.override_history import undo_batch
        result = undo_batch(batch_id, config)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/recommendations-for-period", methods=["POST"])
def api_recommendations_for_period():
    """Generate fresh recommendations for a given date range and return HTML."""
    data = request.get_json()
    from_date = data.get("from")   # "YYYY-MM-DD" or None
    to_date   = data.get("to")     # "YYYY-MM-DD" or None
    label     = data.get("label", "Custom Period")

    try:
        import pandas as pd
        from src.recommendations import generate_recommendations_html

        config = _load_config()
        if not config:
            return jsonify({"ok": False, "error": "config.yaml not found"}), 500

        # Resolve API key the same way as other endpoints
        env_key = _build_env().get("ANTHROPIC_API_KEY", "")
        if env_key and not config.get("anthropic_api_key"):
            config["anthropic_api_key"] = env_key

        from src.archiver import load_master_csv
        df = load_master_csv(config)
        if df.empty:
            return jsonify({"ok": False, "error": "no transactions found"}), 500

        try:
            if from_date:
                df = df[df["date"] >= pd.Timestamp(from_date)]
            if to_date:
                df = df[df["date"] <= pd.Timestamp(to_date)]
        except (ValueError, TypeError) as _e:
            return jsonify({"ok": False, "error": f"Invalid date: {_e}"}), 400

        html_str = generate_recommendations_html(df, config, period_label=label)
        return jsonify({"ok": True, "html": html_str})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/reimbursements")
def reimbursements_page():
    from src.reimbursement_tracker import load_batches, batch_status

    config = _load_config()

    with open_db(config) as conn:
        init_db(conn)

        expense_rows = conn.execute(
            "SELECT txn_id, date, description, amount, category, sub_category "
            "FROM transactions WHERE is_business=1 AND amount<0 ORDER BY date DESC"
        ).fetchall()

        reimb_rows = conn.execute(
            "SELECT txn_id, date, description, amount "
            "FROM transactions WHERE category='Business Reimbursement' AND amount>0 ORDER BY date DESC"
        ).fetchall()

        data = load_batches(config)
        batches = data.get("batches", [])

        batched_ids: set = set()
        linked_reimb_ids: set = set()
        for b in batches:
            batched_ids.update(b.get("expense_txn_ids", []))
            if b.get("reimbursement_txn_id"):
                linked_reimb_ids.add(b["reimbursement_txn_id"])
            b["status"] = batch_status(b)
            received = float(b.get("received_amount") or 0)
            expected = float(b.get("expense_total") or 0)
            b["shortfall"] = round(expected - received, 2) if b.get("reimbursement_txn_id") and received < expected else 0

            if b.get("expense_txn_ids"):
                ph = ",".join("?" * len(b["expense_txn_ids"]))
                b["expenses"] = [dict(r) for r in conn.execute(
                    f"SELECT txn_id, date, description, amount, category FROM transactions "
                    f"WHERE txn_id IN ({ph}) ORDER BY date DESC",
                    b["expense_txn_ids"],
                ).fetchall()]
            else:
                b["expenses"] = []

    unsubmitted = [dict(r) for r in expense_rows if r["txn_id"] not in batched_ids]
    available_reimb = [dict(r) for r in reimb_rows if r["txn_id"] not in linked_reimb_ids]

    has_historical = any(b.get("source") == "historical" for b in batches)

    return render_template(
        "reimbursements.html",
        unsubmitted=unsubmitted,
        batches=sorted(batches, key=lambda b: b.get("submitted_date", ""), reverse=True),
        available_reimb=available_reimb,
        has_historical=has_historical,
        active_tab="reimbursements",
    )


@app.route("/api/reimbursements", methods=["POST"])
def api_create_reimbursement_batch():
    import hashlib
    from datetime import date
    from src.reimbursement_tracker import load_batches, save_batches

    config = _load_config()

    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    txn_ids = body.get("txn_ids") or []
    if not name or not txn_ids:
        return jsonify({"ok": False, "error": "name and txn_ids required"}), 400

    ph = ",".join("?" * len(txn_ids))
    with open_db(config) as conn:
        init_db(conn)
        rows = conn.execute(
            f"SELECT txn_id, amount FROM transactions WHERE txn_id IN ({ph})", txn_ids
        ).fetchall()

    expense_total = round(sum(abs(float(r["amount"])) for r in rows), 2)
    batch_id = hashlib.md5(f"{name}{date.today().isoformat()}".encode()).hexdigest()[:12]

    data = load_batches(config)
    data["batches"].append({
        "id": batch_id,
        "name": name,
        "submitted_date": date.today().isoformat(),
        "expense_txn_ids": txn_ids,
        "expense_total": expense_total,
        "reimbursement_txn_id": None,
        "received_amount": None,
        "received_date": None,
    })
    save_batches(data, config)
    return jsonify({"ok": True, "batch_id": batch_id, "expense_total": expense_total})


@app.route("/api/reimbursements/<batch_id>", methods=["PATCH"])
def api_link_reimbursement(batch_id: str):
    from src.reimbursement_tracker import load_batches, save_batches

    config = _load_config()

    body = request.get_json() or {}
    reimb_txn_id = (body.get("reimbursement_txn_id") or "").strip()
    if not reimb_txn_id:
        return jsonify({"ok": False, "error": "reimbursement_txn_id required"}), 400

    with open_db(config) as conn:
        row = conn.execute(
            "SELECT amount, date FROM transactions WHERE txn_id=?", (reimb_txn_id,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "transaction not found"}), 404

    data = load_batches(config)
    batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if not batch:
        return jsonify({"ok": False, "error": "batch not found"}), 404

    batch["reimbursement_txn_id"] = reimb_txn_id
    batch["received_amount"] = round(float(row["amount"]), 2)
    batch["received_date"] = str(row["date"])[:10]
    save_batches(data, config)
    return jsonify({"ok": True})


@app.route("/api/reimbursements/suggest-historical", methods=["POST"])
def api_suggest_historical():
    from src.reimbursement_tracker import load_batches, suggest_reimbursement_batches

    config = _load_config()

    body = request.get_json() or {}
    exact_tol = float(body.get("exact_tol", 1.00))
    near_pct  = float(body.get("near_pct", 0.05))

    company_name = config.get("business", {}).get("company_name", "").strip().upper()
    if not company_name:
        return jsonify({"ok": False, "error": "business.company_name not set in config.yaml"}), 400

    with open_db(config) as conn:
        init_db(conn)

        expense_rows = conn.execute(
            "SELECT txn_id, date, description, amount, category, sub_category "
            "FROM transactions WHERE is_business=1 AND amount<0 ORDER BY date ASC"
        ).fetchall()

        credit_rows = conn.execute(
            "SELECT txn_id, date, description, amount "
            "FROM transactions "
            "WHERE amount>0 AND instr(upper(description), ?) > 0 ORDER BY date ASC",
            (company_name,),
        ).fetchall()

    data = load_batches(config)
    batched_ids: set = {
        tid for b in data.get("batches", []) for tid in b.get("expense_txn_ids", [])
    }
    linked_ids: set = {
        b["reimbursement_txn_id"] for b in data.get("batches", []) if b.get("reimbursement_txn_id")
    }

    expenses = [dict(r) for r in expense_rows if r["txn_id"] not in batched_ids]
    credits  = [dict(r) for r in credit_rows  if r["txn_id"] not in linked_ids]

    if not expenses:
        return jsonify({"ok": True, "suggestions": [], "message": "No unsubmitted expenses to match"})

    suggestions = suggest_reimbursement_batches(
        expenses, credits, exact_tol=exact_tol, near_pct=near_pct
    )
    return jsonify({"ok": True, "suggestions": suggestions})


@app.route("/api/reimbursements/accept-suggestions", methods=["POST"])
def api_accept_suggestions():
    import hashlib
    from datetime import date
    from src.reimbursement_tracker import load_batches, save_batches

    config = _load_config()

    accepted = (request.get_json() or {}).get("suggestions", [])
    if not accepted:
        return jsonify({"ok": False, "error": "no suggestions provided"}), 400

    data = load_batches(config)
    created = 0
    for i, s in enumerate(accepted):
        txn_ids = s.get("expense_txn_ids", [])
        if not txn_ids:
            continue
        name = (s.get("name") or s.get("suggested_name") or "Historical Batch").strip()
        payment_txn_id = s.get("payment_txn_id")
        payment_amount = s.get("payment_amount")
        payment_date   = s.get("payment_date")
        batch_id = hashlib.md5(f"{name}{date.today().isoformat()}{i}".encode()).hexdigest()[:12]
        data["batches"].append({
            "id":                   batch_id,
            "name":                 name,
            "submitted_date":       date.today().isoformat(),
            "expense_txn_ids":      txn_ids,
            "expense_total":        round(float(s.get("expense_total", 0)), 2),
            "reimbursement_txn_id": payment_txn_id,
            "received_amount":      round(float(payment_amount), 2) if payment_amount is not None else None,
            "received_date":        payment_date,
            "source":               "historical",
        })
        created += 1

    save_batches(data, config)
    return jsonify({"ok": True, "created": created})


@app.route("/api/reimbursements/historical", methods=["DELETE"])
def api_delete_historical_batches():
    """Delete all batches created by the suggestion algorithm (source='historical')."""
    from src.reimbursement_tracker import load_batches, save_batches

    config = _load_config()

    data = load_batches(config)
    before = len(data["batches"])
    data["batches"] = [b for b in data["batches"] if b.get("source") != "historical"]
    deleted = before - len(data["batches"])
    save_batches(data, config)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/reimbursements/<batch_id>", methods=["DELETE"])
def api_delete_reimbursement_batch(batch_id: str):
    from src.reimbursement_tracker import load_batches, save_batches

    config = _load_config()

    data = load_batches(config)
    before = len(data["batches"])
    data["batches"] = [b for b in data["batches"] if b["id"] != batch_id]
    if len(data["batches"]) == before:
        return jsonify({"ok": False, "error": "batch not found"}), 404
    save_batches(data, config)
    return jsonify({"ok": True})


@app.route("/api/cache", methods=["GET"])
def api_get_cache():
    """Return cache entries, optionally filtered by ?q= search term."""
    q = request.args.get("q", "").strip().upper()
    p = _data_path("cache_file", "data/categorisation_cache.json")
    if not p.exists():
        return jsonify({"ok": True, "entries": [], "total": 0})
    with open(p, encoding="utf-8") as f:
        cache = json.load(f)
    entries = [
        {"key": k, "category": v.get("category", ""), "business": v.get("business", False)}
        for k, v in sorted(cache.items())
        if not q or q in k
    ]
    return jsonify({"ok": True, "entries": entries, "total": len(cache)})


@app.route("/api/cache", methods=["DELETE"])
def api_delete_cache():
    """Delete one cache entry by key, or all entries if clear_all=true."""
    data = request.get_json() or {}
    p = _data_path("cache_file", "data/categorisation_cache.json")
    if not p.exists():
        return jsonify({"ok": True, "deleted": 0})
    with open(p, encoding="utf-8") as f:
        cache = json.load(f)

    if data.get("clear_all"):
        deleted = len(cache)
        cache = {}
    else:
        key = (data.get("key") or "").strip()
        if not key or key not in cache:
            return jsonify({"ok": False, "error": "key not found"}), 404
        del cache[key]
        deleted = 1

    with open(p, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/coverage")
@require_module("coverage")
def coverage_page():
    from collections import defaultdict
    from src.parsers import backfill_statement_periods

    config = _load_config()

    # One-time migration: populate statement_periods from archives if table is empty
    backfill_statement_periods(config)

    with open_db(config) as conn:
        rows = conn.execute(
            "SELECT account, strftime('%Y-%m', date) AS month, COUNT(*) AS cnt "
            "FROM transactions GROUP BY account, month ORDER BY account, month"
        ).fetchall()

    data: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        data[row["account"]][row["month"]] = row["cnt"]

    # Months covered by a loaded statement (for distinguishing zero-activity from gaps)
    covered = load_covered_months(config)  # {account: [YYYY-MM, ...]}

    accounts = sorted(data.keys())
    all_months: set[str] = set()
    for m_dict in data.values():
        all_months.update(m_dict.keys())
    # Also include months from statement periods that had zero transactions
    for acct_months in covered.values():
        all_months.update(acct_months)

    months_range: list[str] = []
    if all_months:
        min_m, max_m = min(all_months), max(all_months)
        y, mo = int(min_m[:4]), int(min_m[5:7])
        ey, emo = int(max_m[:4]), int(max_m[5:7])
        while (y, mo) <= (ey, emo):
            months_range.append(f"{y:04d}-{mo:02d}")
            mo += 1
            if mo > 12:
                mo, y = 1, y + 1

    acct_range = {a: (min(m_dict), max(m_dict)) for a, m_dict in data.items()}
    # Extend acct_range to cover statement periods (catches accounts with all-zero months)
    for acct, months in covered.items():
        if months:
            if acct in acct_range:
                acct_range[acct] = (
                    min(acct_range[acct][0], months[0]),
                    max(acct_range[acct][1], months[-1]),
                )
            else:
                acct_range[acct] = (months[0], months[-1])
                if acct not in data:
                    data[acct] = {}
                    accounts = sorted(data.keys())

    _MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    prev_year = None
    month_headers = []
    for m in months_range:
        year = m[:4]
        month_headers.append({
            "value": m,
            "label": _MONTH_NAMES[int(m[5:7]) - 1],
            "year": year,
            "show_year": year != prev_year,
        })
        prev_year = year

    total_gaps = sum(
        1
        for a, m_dict in data.items()
        for m in months_range
        if acct_range.get(a, ("", ""))[0] <= m <= acct_range.get(a, ("", ""))[1]
        and m_dict.get(m, 0) == 0
        and m not in covered.get(a, [])
    )

    return render_template(
        "coverage.html",
        accounts=accounts,
        months_range=months_range,
        data=dict(data),
        acct_range=acct_range,
        covered=covered,
        total_gaps=total_gaps,
        month_headers=month_headers,
    )


@app.route("/capital-gains")
@require_module("business")
def capital_gains_page():
    config = _load_config()
    p = _data_path("capital_gains_file", "Data/capital_gains.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    # Derive FY list from DB (same years as transactions) + any already saved
    try:
        with open_db(config) as conn:
            rows = conn.execute(
                "SELECT DISTINCT CASE WHEN CAST(strftime('%m',date) AS INTEGER) >= 7 "
                "THEN CAST(strftime('%Y',date) AS INTEGER)+1 "
                "ELSE CAST(strftime('%Y',date) AS INTEGER) END AS fy "
                "FROM transactions ORDER BY fy DESC"
            ).fetchall()
            db_fys = [str(r[0]) for r in rows]
    except Exception:
        db_fys = []
    all_fys = sorted(set(db_fys) | set(data.keys()), reverse=True)
    from datetime import date as _date
    _today = _date.today()
    current_fy = str(_today.year + 1 if _today.month >= 7 else _today.year)
    return render_template("capital_gains.html", fys=all_fys, data=data,
                           current_fy=current_fy, active_tab="capital_gains")


@app.route("/api/capital-gains", methods=["GET"])
def api_capital_gains_get():
    p = _data_path("capital_gains_file", "Data/capital_gains.json")
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")) if p.exists() else {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/capital-gains", methods=["POST"])
def api_capital_gains_save():
    payload = request.get_json() or {}
    fy  = str(payload.get("fy", "")).strip()
    if not fy.isdigit():
        return jsonify({"ok": False, "error": "invalid fy"}), 400
    fields = {
        "short_term_gains":       float(payload.get("short_term_gains", 0) or 0),
        "long_term_gains":        float(payload.get("long_term_gains", 0) or 0),
        "gross_gains":            float(payload.get("gross_gains", 0) or 0),
        "cgt_discount":           float(payload.get("cgt_discount", 0) or 0),
        "net_gains":              float(payload.get("net_gains", 0) or 0),
        "capital_losses_applied": float(payload.get("capital_losses_applied", 0) or 0),
        "carried_forward_losses": float(payload.get("carried_forward_losses", 0) or 0),
        "notes":                  str(payload.get("notes", "")),
    }
    p = _data_path("capital_gains_file", "Data/capital_gains.json")
    try:
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        existing = {}
    existing[fy] = fields
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/franking-credits")
@require_module("business")
def franking_credits_page():
    config = _load_config()
    p = _data_path("franking_credits_file", "Data/franking_credits.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    try:
        with open_db(config) as conn:
            rows = conn.execute(
                "SELECT DISTINCT CASE WHEN CAST(strftime('%m',date) AS INTEGER) >= 7 "
                "THEN CAST(strftime('%Y',date) AS INTEGER)+1 "
                "ELSE CAST(strftime('%Y',date) AS INTEGER) END AS fy "
                "FROM transactions ORDER BY fy DESC"
            ).fetchall()
            db_fys = [str(r[0]) for r in rows]
    except Exception:
        db_fys = []
    all_fys = sorted(set(db_fys) | set(data.keys()), reverse=True)
    from datetime import date as _date
    _today = _date.today()
    current_fy = str(_today.year + 1 if _today.month >= 7 else _today.year)
    return render_template(
        "franking_credits.html", fys=all_fys, data=data,
        current_fy=current_fy, active_tab="franking_credits",
    )


@app.route("/api/franking-credits", methods=["POST"])
def api_franking_credits_save():
    payload = request.get_json() or {}
    fy = str(payload.get("fy", "")).strip()
    if not fy.isdigit():
        return jsonify({"ok": False, "error": "invalid fy"}), 400
    fields = {
        "cash_dividends":   float(payload.get("cash_dividends", 0) or 0),
        "franking_credits": float(payload.get("franking_credits", 0) or 0),
        "notes":            str(payload.get("notes", "")),
    }
    p = _data_path("franking_credits_file", "Data/franking_credits.json")
    try:
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        existing = {}
    existing[fy] = fields
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/tax-export/<fy>")
def api_tax_export(fy: str):
    """Generate and download a ZIP tax package for the given Australian FY."""
    import csv
    import io
    import zipfile
    from datetime import date as _date
    from src.db import load_transactions as _load_txns, open_db as _open_db, init_db as _init_db
    from src.tax_estimator import estimate_income_tax, estimate_hecs_repayment

    if not fy.isdigit() or len(fy) != 4:
        return jsonify({"error": "invalid fy"}), 400
    fy_int = int(fy)
    config = _load_config()

    # Load transactions filtered to this FY via SQL date bounds
    df = _load_txns(config, since=f"{fy_int - 1}-07-01", until=f"{fy_int}-06-30")
    fy_df = df

    _INCOME_CATS = {"Income", "Board & Lodging", "Interest Income", "Business Reimbursement", "Family Loan Received"}
    _TAXABLE_CATS = {"Income", "Interest Income"}
    inc_df = fy_df[fy_df["category"].isin(_INCOME_CATS) & (fy_df["amount"] > 0)] if not fy_df.empty else fy_df
    taxable_income = float(inc_df[inc_df["category"].isin(_TAXABLE_CATS)]["amount"].sum()) if not inc_df.empty else 0.0

    has_biz = "is_business" in fy_df.columns if not fy_df.empty else False
    has_tax = "is_tax_deductible" in fy_df.columns if not fy_df.empty else False
    has_gst = "is_gst_claimable" in fy_df.columns if not fy_df.empty else False
    biz_df  = fy_df[fy_df["is_business"] & (fy_df["amount"] < 0)].sort_values("date") if has_biz and not fy_df.empty else fy_df.iloc[0:0]
    tax_df  = fy_df[fy_df["is_tax_deductible"] & (fy_df["amount"] < 0)].sort_values("date") if has_tax and not fy_df.empty else fy_df.iloc[0:0]
    gst_df  = fy_df[fy_df["is_gst_claimable"] & (fy_df["amount"] < 0)].sort_values("date") if has_gst and not fy_df.empty else fy_df.iloc[0:0]
    total_biz = float(biz_df["amount"].abs().sum()) if not biz_df.empty else 0.0
    total_tax = float(tax_df["amount"].abs().sum()) if not tax_df.empty else 0.0
    total_gst = float(gst_df["amount"].abs().sum()) if not gst_df.empty else 0.0

    # Load CG and franking data
    cg_path = _data_path("capital_gains_file", "Data/capital_gains.json")
    try:
        cg_data = json.loads(cg_path.read_text(encoding="utf-8")).get(fy, {}) if cg_path.exists() else {}
    except Exception:
        cg_data = {}
    fc_path = _data_path("franking_credits_file", "Data/franking_credits.json")
    try:
        fc_data = json.loads(fc_path.read_text(encoding="utf-8")).get(fy, {}) if fc_path.exists() else {}
    except Exception:
        fc_data = {}

    tax_est = estimate_income_tax(taxable_income, fy_int) if taxable_income > 0 else None
    hecs = estimate_hecs_repayment(taxable_income, fy_int) if taxable_income > 0 else None

    def df_to_csv(frame, cols):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for _, row in frame.iterrows():
            w.writerow([str(row.get(c, "")) for c in cols])
        return buf.getvalue().encode("utf-8")

    def rows_to_csv(headers, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        for row in rows:
            w.writerow(row)
        return buf.getvalue().encode("utf-8")

    def summary_txt():
        lines = [
            f"PERSONAL FINANCE ANALYSER — TAX PACKAGE FY{fy}",
            f"Generated: {_date.today().isoformat()}",
            f"Period: 1 Jul {fy_int - 1} – 30 Jun {fy_int}",
            "",
            "INCOME",
            f"  Taxable Income:          ${taxable_income:>12,.2f}",
            "",
            "DEDUCTIONS",
            f"  Business Expenses:       ${total_biz:>12,.2f}",
            f"  Tax-Deductible Items:    ${total_tax:>12,.2f}",
            f"  GST-Claimable Expenses:  ${total_gst:>12,.2f}",
            "",
        ]
        if cg_data:
            gross = float(cg_data.get("gross_gains", 0))
            disc  = float(cg_data.get("cgt_discount", 0))
            net   = float(cg_data.get("net_gains", 0))
            lines += [
                "CAPITAL GAINS",
                f"  Gross Capital Gains:     ${gross:>12,.2f}",
                f"  CGT Discount (50%):     (${disc:>12,.2f})",
                f"  Net Taxable CGT:         ${net:>12,.2f}",
                "",
            ]
        if fc_data:
            cash = float(fc_data.get("cash_dividends", 0))
            cred = float(fc_data.get("franking_credits", 0))
            lines += [
                "DIVIDENDS & FRANKING CREDITS",
                f"  Cash Dividends:          ${cash:>12,.2f}",
                f"  Franking Credits:        ${cred:>12,.2f}",
                f"  Grossed-Up Total:        ${cash + cred:>12,.2f}",
                "",
            ]
        if tax_est:
            lines += [
                "ESTIMATED INCOME TAX (ATO brackets — estimate only)",
                f"  Gross Tax:               ${tax_est['gross_tax']:>12,.2f}",
                f"  LITO:                   (${tax_est['lito']:>12,.2f})",
                f"  Medicare Levy (2%):      ${tax_est['medicare_levy']:>12,.2f}",
                f"  Total Tax Payable:       ${tax_est['total_tax']:>12,.2f}",
                f"  Effective Rate:          {tax_est['effective_rate_pct']:>11.1f}%",
                f"  Net Income After Tax:    ${tax_est['net_income']:>12,.2f}",
                "",
            ]
        if hecs:
            lines += [
                "HECS/HELP REPAYMENT ALERT",
                f"  Threshold:               ${hecs['threshold']:>12,}",
                f"  Repayment Rate:          {hecs['rate_pct']:>11.1f}%",
                f"  Estimated Repayment:     ${hecs['repayment']:>12,.2f}",
                "",
            ]
        lines += [
            "DISCLAIMER",
            "  Estimates only. Verify all figures with ATO and your tax professional.",
            "  Income tax brackets: ATO Stage 3 cuts (from 1 Jul 2024 / FY2025).",
            "  HECS thresholds: ATO 2024-25 rates — confirm current year at ato.gov.au.",
        ]
        return "\n".join(lines).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"README.txt", (
            f"Personal Finance Analyser — FY{fy} Tax Package\n"
            f"Generated: {_date.today().isoformat()}\n\n"
            "Files:\n"
            "  summary.txt            — Income, deductions, estimated tax\n"
            "  gst_claimable.csv      — GST-flagged business transactions\n"
            "  business_expenses.csv  — Reimbursable business expenses\n"
            "  tax_deductions.csv     — Tax-deductible personal items\n"
            "  capital_gains.csv      — Capital gains worksheet\n"
            "  franking_credits.csv   — Dividend and franking credit summary\n\n"
            "DISCLAIMER: Estimates only. Not tax advice.\n"
        ).encode())
        zf.writestr("summary.txt", summary_txt())
        txn_cols = ["date", "description", "category", "account", "amount"]
        zf.writestr("gst_claimable.csv",     df_to_csv(gst_df, txn_cols))
        zf.writestr("business_expenses.csv", df_to_csv(biz_df, txn_cols))
        zf.writestr("tax_deductions.csv",    df_to_csv(tax_df, txn_cols))
        cg_headers = ["short_term_gains", "long_term_gains", "gross_gains", "cgt_discount",
                      "capital_losses_applied", "net_gains", "carried_forward_losses", "notes"]
        zf.writestr("capital_gains.csv", rows_to_csv(
            cg_headers, [[cg_data.get(h, "") for h in cg_headers]] if cg_data else [],
        ))
        fc_headers = ["cash_dividends", "franking_credits", "grossed_up_total", "notes"]
        fc_row = []
        if fc_data:
            cash = float(fc_data.get("cash_dividends", 0))
            cred = float(fc_data.get("franking_credits", 0))
            fc_row = [[cash, cred, round(cash + cred, 2), fc_data.get("notes", "")]]
        zf.writestr("franking_credits.csv", rows_to_csv(fc_headers, fc_row))

    buf.seek(0)
    response = make_response(buf.read())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f'attachment; filename="tax_package_FY{fy}.zip"'
    return response


@app.route("/api/business-export/<fy>")
def api_business_export(fy: str):
    """Export business-flagged transactions for a FY as OFX, QIF, or CSV.

    Query param: format=ofx|qif|csv (default csv).
    Query param: filter=business|gst|both (default both — union of biz + gst-claimable rows).
    """
    from src.biz_export import generate_ofx, generate_qif, generate_csv
    from src.db import load_transactions as _load_txns

    if not fy.isdigit() or len(fy) != 4:
        return jsonify({"error": "invalid fy"}), 400
    fy_int = int(fy)
    fmt    = request.args.get("format", "csv").lower()
    filt   = request.args.get("filter", "both").lower()
    if fmt not in ("ofx", "qif", "csv"):
        return jsonify({"error": "format must be ofx, qif, or csv"}), 400

    config = _load_config()
    df = _load_txns(config, since=f"{fy_int - 1}-07-01", until=f"{fy_int}-06-30")
    if df.empty:
        rows = []
    else:
        fy_df = df
        mask = fy_df["amount"] < 0
        if filt == "business":
            if "is_business" in fy_df.columns:
                mask &= fy_df["is_business"]
        elif filt == "gst":
            if "is_gst_claimable" in fy_df.columns:
                mask &= fy_df["is_gst_claimable"]
        else:  # both
            sub = fy_df[mask].copy()
            biz_mask = sub["is_business"] if "is_business" in sub.columns else False
            gst_mask = sub["is_gst_claimable"] if "is_gst_claimable" in sub.columns else False
            mask = mask & (biz_mask | gst_mask)
        rows_df = fy_df[mask].sort_values("date")
        rows = rows_df.to_dict("records")

    if fmt == "ofx":
        content  = generate_ofx(rows, fy_int, config).encode("ascii", errors="replace")
        mimetype = "application/x-ofx"
        filename = f"business_FY{fy}.ofx"
    elif fmt == "qif":
        content  = generate_qif(rows, fy_int, config).encode("utf-8")
        mimetype = "application/x-qif"
        filename = f"business_FY{fy}.qif"
    else:
        content  = generate_csv(rows, config)
        mimetype = "text/csv"
        filename = f"business_FY{fy}.csv"

    response = make_response(content)
    response.headers["Content-Type"] = mimetype
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ── Basiq CDR / Open Banking ─────────────────────────────────────────────────

def _basiq_state_path() -> Path:
    return _data_path("basiq_state_file", "Data/basiq_state.json")


def _ensure_basiq_key() -> str:
    """Return BASIQ_API_KEY, reading from User env via PowerShell if not in os.environ."""
    if os.environ.get("BASIQ_API_KEY"):
        return os.environ["BASIQ_API_KEY"]
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetEnvironmentVariable('BASIQ_API_KEY','User')"],
                capture_output=True, text=True, timeout=5,
            )
            val = result.stdout.strip()
            if val:
                os.environ["BASIQ_API_KEY"] = val
                return val
        except Exception:
            pass
    return ""


def _get_account_staleness(config: dict) -> list[dict]:
    """Return staleness info for every unique account display_name in config."""
    from datetime import date as _date, datetime as _dt
    from src.db import open_db as _odb, init_db as _idb

    today = _date.today()

    # Collect unique display_names (preserve order, first-seen wins)
    seen: set[str] = set()
    account_cfgs: list[tuple[str, dict]] = []
    for acct in config.get("accounts", {}).values():
        name = acct.get("display_name", "")
        if name and name not in seen:
            seen.add(name)
            account_cfgs.append((name, acct))

    # Last transaction date per account
    last_dates: dict[str, str] = {}
    try:
        with _odb(config) as conn:
            _idb(conn)
            for row in conn.execute(
                "SELECT account, MAX(date) as last_date FROM transactions GROUP BY account"
            ).fetchall():
                last_dates[row["account"]] = row["last_date"]
    except Exception:
        pass

    # Basiq-connected accounts and closed accounts from state file
    state = {}
    try:
        sp = _basiq_state_path()
        if sp.exists():
            state = json.loads(sp.read_text("utf-8"))
    except Exception:
        pass
    cdr_accounts: set[str] = set(state.get("connected_accounts", []))
    closed_accounts: set[str] = set(state.get("closed_accounts", []))

    result = []
    for name, acct_cfg in account_cfgs:
        last_str = last_dates.get(name)
        days_stale = None
        if last_str:
            try:
                last = _dt.strptime(last_str[:10], "%Y-%m-%d").date()
                days_stale = (today - last).days
            except Exception:
                pass

        if name in closed_accounts:
            status = "closed"
        elif name in cdr_accounts:
            status = "live"
        elif days_stale is None:
            status = "never"
        elif days_stale <= 30:
            status = "current"
        elif days_stale <= 60:
            status = "stale"
        else:
            status = "overdue"

        result.append({
            "name":       name,
            "bank":       acct_cfg.get("bank", ""),
            "type":       acct_cfg.get("type", ""),
            "last_date":  last_str[:10] if last_str else None,
            "days_stale": days_stale,
            "status":     status,
            "is_cdr":     name in cdr_accounts,
            "is_closed":  name in closed_accounts,
        })
    return result


@app.route("/data-sources")
def data_sources_page():
    from src.basiq import is_configured as _basiq_ok, load_state as _load_bstate
    cfg = _load_config()
    _ensure_basiq_key()
    basiq_configured = _basiq_ok()
    state = _load_bstate(_basiq_state_path()) if basiq_configured else {}
    connections = state.get("connections", [])
    accounts = _get_account_staleness(cfg)
    stale_count = sum(1 for a in accounts if a["status"] in ("stale", "overdue", "never")
                      and not a["is_closed"])
    return render_template(
        "data_sources.html",
        basiq_configured=basiq_configured,
        connections=connections,
        last_sync=state.get("last_sync"),
        accounts=accounts,
        stale_count=stale_count,
        active_section="settings",
        active_tab="data_sources",
    )


@app.route("/api/basiq/connect")
def api_basiq_connect():
    """Generate a Basiq consent URL and return it as JSON."""
    _ensure_basiq_key()
    from src.basiq import is_configured as _bq_ok, load_state as _ls, save_state as _ss
    from src.basiq import get_or_create_user, create_auth_link
    cfg = _load_config()
    if not _bq_ok():
        return jsonify({"error": "Basiq not configured — set BASIQ_API_KEY"}), 400
    try:
        state = _ls(_basiq_state_path())
        email = cfg.get("basiq", {}).get("email", "") or cfg.get("server", {}).get("email", "")
        if not email:
            return jsonify({"error": "Set basiq.email in config.yaml"}), 400
        user_id = get_or_create_user(email, state, _basiq_state_path())
        redirect_url = f"http://localhost:{PORT}/api/basiq/callback"
        consent_url = create_auth_link(user_id, redirect_url)
        return jsonify({"consent_url": consent_url})
    except Exception as exc:
        logger.error(f"Basiq connect error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/basiq/callback")
def api_basiq_callback():
    """
    OAuth callback from Basiq after the user completes bank consent.
    Refreshes connections list and redirects back to /data-sources.
    """
    _ensure_basiq_key()
    from src.basiq import (is_configured as _bq_ok, load_state as _ls, save_state as _ss,
                           get_connections, fetch_accounts, build_account_map)
    cfg = _load_config()
    if not _bq_ok():
        return redirect("/data-sources?error=notconfigured")
    try:
        state = _ls(_basiq_state_path())
        user_id = state.get("user_id", "")
        if not user_id:
            return redirect("/data-sources?error=nouser")

        # Refresh connection list
        connections = get_connections(user_id)
        # Fetch accounts to build display_name mapping
        basiq_accounts = fetch_accounts(user_id)
        account_map = build_account_map(basiq_accounts, cfg)
        connected_names = list(set(account_map.values()))

        state["connections"] = [
            {
                "id":               c.get("id", ""),
                "institution":      c.get("institution", {}).get("id", "") if isinstance(c.get("institution"), dict) else c.get("institution", ""),
                "institution_name": c.get("institution", {}).get("name", "") if isinstance(c.get("institution"), dict) else "",
                "status":           c.get("status", ""),
            }
            for c in connections
        ]
        state["connected_accounts"] = connected_names
        _ss(_basiq_state_path(), state)
        logger.info(f"Basiq callback: {len(connections)} connection(s), accounts: {connected_names}")
        return redirect("/data-sources?connected=1")
    except Exception as exc:
        logger.error(f"Basiq callback error: {exc}")
        return redirect(f"/data-sources?error={urllib.parse.quote(str(exc))}")


@app.route("/api/basiq/sync", methods=["POST"])
def api_basiq_sync():
    """
    SSE endpoint: pull CDR transactions since last sync, insert into DB.
    Streams progress lines to the browser.
    """
    _ensure_basiq_key()
    from src.basiq import (is_configured as _bq_ok, load_state as _ls, save_state as _ss,
                           refresh_connections, fetch_accounts, build_account_map,
                           fetch_transactions, map_to_transaction, default_since_date)

    cfg = _load_config()

    def _run():
        if not _bq_ok():
            yield "data: Basiq not configured — set BASIQ_API_KEY environment variable.\n\n"
            yield "event: error\ndata: notconfigured\n\n"
            return

        state = _ls(_basiq_state_path())
        user_id = state.get("user_id", "")
        if not user_id:
            yield "data: No Basiq user found — connect a bank account first.\n\n"
            yield "event: error\ndata: nouser\n\n"
            return

        try:
            yield "data: Refreshing bank connections…\n\n"
            refresh_connections(user_id)

            yield "data: Fetching connected accounts…\n\n"
            basiq_accounts = fetch_accounts(user_id)
            account_map = build_account_map(basiq_accounts, cfg)
            if not account_map:
                yield "data: No accounts matched to PFA config (check BSB/account numbers).\n\n"
                yield "event: error\ndata: nomatch\n\n"
                return
            yield f"data: Matched {len(account_map)} account(s): {', '.join(set(account_map.values()))}\n\n"

            since = default_since_date(state)
            yield f"data: Fetching transactions since {since.isoformat()}…\n\n"
            raw_txns = fetch_transactions(user_id, since=since)
            yield f"data: Received {len(raw_txns)} transaction(s) from Basiq.\n\n"

            mapped = [map_to_transaction(t, account_map) for t in raw_txns]
            mapped = [r for r in mapped if r is not None]
            yield f"data: {len(mapped)} transaction(s) ready to import.\n\n"

            if mapped:
                inserted = upsert_basiq_transactions(mapped, cfg)
                skipped = len(mapped) - inserted
                yield f"data: Inserted {inserted} new transaction(s)"
                if skipped:
                    yield f" ({skipped} duplicate(s) skipped)"
                yield ".\n\n"
            else:
                yield "data: Nothing new to import.\n\n"

            # Update state
            state["last_sync"] = _dt.now().isoformat(timespec="seconds")
            connected_names = list(set(account_map.values()))
            state.setdefault("connected_accounts", [])
            for name in connected_names:
                if name not in state["connected_accounts"]:
                    state["connected_accounts"].append(name)
            _ss(_basiq_state_path(), state)

            yield "data: \n\n"
            yield "data: ✓ Sync complete. Run Reports to categorise new transactions.\n\n"
            yield "event: done\ndata: ok\n\n"

        except Exception as exc:
            logger.error(f"Basiq sync error: {exc}")
            yield f"data: ✗ Sync failed: {exc}\n\n"
            yield "event: error\ndata: exception\n\n"

    return Response(stream_with_context(_run()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/basiq/disconnect/<connection_id>", methods=["POST"])
def api_basiq_disconnect(connection_id: str):
    """Remove a Basiq bank connection."""
    _ensure_basiq_key()
    from src.basiq import (is_configured as _bq_ok, load_state as _ls, save_state as _ss,
                           delete_connection)
    cfg = _load_config()
    if not _bq_ok():
        return jsonify({"error": "Basiq not configured"}), 400
    try:
        state = _ls(_basiq_state_path())
        user_id = state.get("user_id", "")
        if not user_id:
            return jsonify({"error": "no user"}), 400
        delete_connection(user_id, connection_id)
        state["connections"] = [c for c in state.get("connections", [])
                                if c.get("id") != connection_id]
        _ss(_basiq_state_path(), state)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error(f"Basiq disconnect error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/basiq/status")
def api_basiq_status():
    """Return current Basiq connection state as JSON."""
    from src.basiq import is_configured as _bq_ok, load_state as _ls
    _ensure_basiq_key()
    state = _ls(_basiq_state_path()) if _bq_ok() else {}
    return jsonify({
        "configured":         _bq_ok(),
        "user_id":            state.get("user_id"),
        "connections":        state.get("connections", []),
        "connected_accounts": state.get("connected_accounts", []),
        "last_sync":          state.get("last_sync"),
    })


@app.route("/api/account/toggle-closed", methods=["POST"])
def api_account_toggle_closed():
    """Mark an account as closed or reactivate it."""
    from src.basiq import load_state as _ls, save_state as _ss
    body = request.get_json(silent=True) or {}
    name = (body.get("account") or "").strip()
    close = bool(body.get("close", True))
    if not name:
        return jsonify({"error": "account required"}), 400
    state = _ls(_basiq_state_path())
    closed: list[str] = state.get("closed_accounts", [])
    if close and name not in closed:
        closed.append(name)
    elif not close and name in closed:
        closed.remove(name)
    state["closed_accounts"] = closed
    _ss(_basiq_state_path(), state)
    return jsonify({"ok": True, "closed": close})


@app.route("/cash-flow")
def cash_flow_page():
    import uuid as _uuid
    from datetime import date, timedelta
    from src.commitment_detector import load_commitments, get_upcoming, monthly_committed_total, FREQUENCY_LABELS
    from src.db import get_db as _get_db, init_db as _init_db

    config = _load_config()
    commitments = load_commitments(config)
    upcoming_365 = get_upcoming(commitments, 365)
    monthly_total = monthly_committed_total(commitments)

    # Manual one-off entries
    entries_path = _data_path("cashflow_entries_file", "Data/cashflow_entries.json")
    try:
        with open(entries_path, encoding="utf-8") as _f:
            manual_entries = json.load(_f)
    except Exception:
        manual_entries = []
    # Filter to future entries only (within 366 days)
    today = date.today()
    horizon_end = (today + timedelta(days=365)).isoformat()
    manual_entries = [e for e in manual_entries if today.isoformat() <= e.get("date", "") <= horizon_end]

    # Variable spend: last 90 days, excluding income, transfers, investments
    from src.db import open_db as _open_db
    since_90 = (today - timedelta(days=90)).isoformat()
    since_12m = (today - timedelta(days=365)).isoformat()
    commit_keys = {(item.get("merchant_key") or "").upper() for item in commitments.get("items", [])}
    try:
        with _open_db(config) as conn:
            _init_db(conn)
            rows_90 = conn.execute(
                "SELECT date, amount, category, description FROM transactions "
                "WHERE date >= ? AND amount < 0",
                (since_90,),
            ).fetchall()
            # Monthly totals for scenario bounds (last 12 months)
            monthly_rows = conn.execute(
                """SELECT substr(date,1,7) AS ym, SUM(ABS(amount)) AS total
                   FROM transactions
                   WHERE date >= ? AND amount < 0
                   GROUP BY ym ORDER BY ym""",
                (since_12m,),
            ).fetchall()
    except Exception:
        rows_90 = []
        monthly_rows = []

    total_var_spend = 0.0
    for row in rows_90:
        if (row[2] or "") in _EXCLUDE_FROM_SPEND:
            continue
        if (row[3] or "").upper().strip() in commit_keys:
            continue
        total_var_spend += abs(float(row[1]))
    daily_avg = round(total_var_spend / 90, 2)

    # Scenario bounds from monthly history (exclude income-only months)
    valid_totals = sorted(r[1] for r in monthly_rows if r[1] and r[1] > 50)
    if len(valid_totals) >= 3:
        daily_opt  = round(valid_totals[max(0, len(valid_totals)//4)] / 30, 2)
        daily_pess = round(valid_totals[min(len(valid_totals)-1, 3*len(valid_totals)//4)] / 30, 2)
    else:
        daily_opt  = round(daily_avg * 0.80, 2)
        daily_pess = round(daily_avg * 1.25, 2)
    daily_opt  = min(daily_opt,  daily_avg)   # never higher than baseline
    daily_pess = max(daily_pess, daily_avg)   # never lower than baseline

    # Pre-calculate totals for each horizon
    d30  = (today + timedelta(days=30)).isoformat()
    d60  = (today + timedelta(days=60)).isoformat()
    d90  = (today + timedelta(days=90)).isoformat()
    d365 = (today + timedelta(days=365)).isoformat()
    committed_30  = round(sum(float(i["amount"]) for i in upcoming_365 if i["projected_date"] <= d30), 2)
    committed_60  = round(sum(float(i["amount"]) for i in upcoming_365 if i["projected_date"] <= d60), 2)
    committed_90  = round(sum(float(i["amount"]) for i in upcoming_365 if i["projected_date"] <= d90), 2)
    committed_365 = round(sum(float(i["amount"]) for i in upcoming_365), 2)

    manual_30  = round(sum(float(e["amount"]) for e in manual_entries if e.get("date","") <= d30), 2)
    manual_60  = round(sum(float(e["amount"]) for e in manual_entries if e.get("date","") <= d60), 2)
    manual_90  = round(sum(float(e["amount"]) for e in manual_entries if e.get("date","") <= d90), 2)
    manual_365 = round(sum(float(e["amount"]) for e in manual_entries), 2)

    # Build 366-day chart data arrays (cumulative, day 0 = today)
    committed_by_date: dict = {}
    for item in upcoming_365:
        dt = item["projected_date"]
        committed_by_date[dt] = committed_by_date.get(dt, 0.0) + float(item["amount"])
    manual_by_date: dict = {}
    for entry in manual_entries:
        dt = entry.get("date", "")
        manual_by_date[dt] = manual_by_date.get(dt, 0.0) + float(entry.get("amount", 0))

    chart_dates, chart_committed, chart_variable, chart_variable_opt, chart_variable_pess, chart_manual = [], [], [], [], [], []
    cum_committed = 0.0
    cum_manual = 0.0
    for i in range(366):
        ds = (today + timedelta(days=i)).isoformat()
        chart_dates.append(ds)
        cum_committed += committed_by_date.get(ds, 0.0)
        cum_manual    += manual_by_date.get(ds, 0.0)
        chart_committed.append(round(cum_committed, 2))
        chart_manual.append(round(cum_manual, 2))
        chart_variable.append(round(daily_avg * i, 2))
        chart_variable_opt.append(round(daily_opt * i, 2))
        chart_variable_pess.append(round(daily_pess * i, 2))

    return render_template(
        "cash_flow.html",
        upcoming=upcoming_365,
        manual_entries=sorted(manual_entries, key=lambda e: e.get("date", "")),
        daily_avg=daily_avg,
        daily_opt=daily_opt,
        daily_pess=daily_pess,
        monthly_total=monthly_total,
        committed_30=committed_30,   committed_60=committed_60,
        committed_90=committed_90,   committed_365=committed_365,
        manual_30=manual_30,   manual_60=manual_60,
        manual_90=manual_90,   manual_365=manual_365,
        variable_30=round(daily_avg * 30, 2),
        variable_60=round(daily_avg * 60, 2),
        variable_90=round(daily_avg * 90, 2),
        variable_365=round(daily_avg * 365, 2),
        variable_opt_30=round(daily_opt * 30, 2),
        variable_opt_60=round(daily_opt * 60, 2),
        variable_opt_90=round(daily_opt * 90, 2),
        variable_opt_365=round(daily_opt * 365, 2),
        variable_pess_30=round(daily_pess * 30, 2),
        variable_pess_60=round(daily_pess * 60, 2),
        variable_pess_90=round(daily_pess * 90, 2),
        variable_pess_365=round(daily_pess * 365, 2),
        chart_dates=json.dumps(chart_dates),
        chart_committed=json.dumps(chart_committed),
        chart_variable=json.dumps(chart_variable),
        chart_variable_opt=json.dumps(chart_variable_opt),
        chart_variable_pess=json.dumps(chart_variable_pess),
        chart_manual=json.dumps(chart_manual),
        today_str=today.isoformat(),
        freq_labels=FREQUENCY_LABELS,
        active_tab="cash_flow",
    )


@app.route("/api/cashflow-entries", methods=["GET", "POST"])
def api_cashflow_entries():
    import uuid as _uuid
    config = _load_config()
    path = _data_path("cashflow_entries_file", "Data/cashflow_entries.json")
    try:
        with open(path, encoding="utf-8") as _f:
            entries = json.load(_f)
    except Exception:
        entries = []
    if request.method == "GET":
        return jsonify(entries)
    body = request.get_json(force=True) or {}
    name   = str(body.get("name", "")).strip()
    amount = float(body.get("amount", 0))
    date_v = str(body.get("date", "")).strip()
    cat    = str(body.get("category", "Other")).strip()
    if not name or not date_v or amount <= 0:
        return jsonify({"error": "name, date and amount are required"}), 400
    entry = {"id": _uuid.uuid4().hex, "name": name, "amount": round(amount, 2), "date": date_v, "category": cat}
    entries.append(entry)
    with open(path, "w", encoding="utf-8") as _f:
        json.dump(entries, _f, indent=2)
    return jsonify(entry), 201


@app.route("/api/cashflow-entries/<entry_id>", methods=["DELETE"])
def api_cashflow_entry_delete(entry_id):
    config = _load_config()
    path = _data_path("cashflow_entries_file", "Data/cashflow_entries.json")
    try:
        with open(path, encoding="utf-8") as _f:
            entries = json.load(_f)
    except Exception:
        entries = []
    entries = [e for e in entries if e.get("id") != entry_id]
    with open(path, "w", encoding="utf-8") as _f:
        json.dump(entries, _f, indent=2)
    return jsonify({"ok": True})


@app.route("/merchants")
def merchants_page():
    """Merchant-level spend analytics — top merchants by spend, frequency, avg."""
    from datetime import date

    config = _load_config()

    with open_db(config) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT
                UPPER(TRIM(description)) AS mk,
                description,
                category,
                COUNT(*) AS visits,
                ROUND(SUM(ABS(amount)), 2) AS total,
                ROUND(AVG(ABS(amount)), 2) AS avg_amt,
                ROUND(MAX(ABS(amount)), 2) AS max_amt,
                MAX(date) AS last_date,
                MIN(date) AS first_date
            FROM transactions
            WHERE amount < 0
              AND COALESCE(is_split_parent, 0) = 0
            GROUP BY mk
            HAVING total > 0
            ORDER BY total DESC
            """
        ).fetchall()

        today = date.today()
        months_6 = []
        for i in range(5, -1, -1):
            m, y = today.month - i, today.year
            while m <= 0:
                m += 12; y -= 1
            months_6.append(f"{y}-{m:02d}")

        monthly_rows = conn.execute(
            f"""
            SELECT UPPER(TRIM(description)) AS mk, substr(date,1,7) AS ym,
                   ROUND(SUM(ABS(amount)), 2) AS spend
            FROM transactions
            WHERE amount < 0 AND substr(date,1,7) >= ?
              AND COALESCE(is_split_parent, 0) = 0
            GROUP BY mk, ym
            """,
            (months_6[0],),
        ).fetchall()

    monthly_by_mk: dict = {}
    for r in monthly_rows:
        monthly_by_mk.setdefault(r["mk"], {})[r["ym"]] = r["spend"]

    merchants = []
    for row in rows:
        cat = row["category"] or "Miscellaneous"
        if cat in _EXCLUDE_FROM_SPEND:
            continue
        mk = row["mk"]
        sparkline = [monthly_by_mk.get(mk, {}).get(ym, 0) for ym in months_6]
        merchants.append({
            "name":       row["description"] or mk,
            "mk":         mk,
            "category":   cat,
            "visits":     row["visits"],
            "total":      row["total"],
            "avg_amt":    row["avg_amt"],
            "max_amt":    row["max_amt"],
            "last_date":  row["last_date"],
            "first_date": row["first_date"],
            "sparkline":  sparkline,
        })

    cats = sorted({m["category"] for m in merchants})
    return render_template(
        "merchants.html",
        merchants_json=json.dumps(merchants[:300]),
        months_json=json.dumps(months_6),
        cats=cats,
        total_spend=round(sum(m["total"] for m in merchants), 2),
        merchant_count=len(merchants),
        active_tab="merchants",
    )


@app.route("/debt-payoff")
@require_module("loans")
def debt_payoff_page():
    """Snowball / avalanche debt payoff calculator for active borrowed loans."""
    from src.loans import load_loans, calculate_loan_position, payoff_months

    config = _load_config()
    raw_loans = load_loans(config).get("loans", [])

    with open_db(config) as conn:
        init_db(conn)
        loans_data = []
        for loan in raw_loans:
            if loan.get("direction") != "borrowed":
                continue
            pos = calculate_loan_position(loan, conn)
            if pos["status"] == "complete":
                continue
            rate = float(loan.get("interest_rate_pct", 0) or 0)
            outstanding = pos["outstanding"]
            loans_data.append({
                "loan_id":          loan["loan_id"],
                "contact":          loan.get("contact", ""),
                "label":            loan.get("label") or loan.get("contact", "Loan"),
                "principal":        float(loan.get("principal", 0) or 0),
                "outstanding":      outstanding,
                "total_repaid":     pos["total_repaid"],
                "pct":              pos["pct"],
                "interest_rate_pct": rate,
            })

    total_outstanding = round(sum(l["outstanding"] for l in loans_data), 2)
    return render_template(
        "debt_payoff.html",
        loans_json=json.dumps(loans_data),
        total_outstanding=total_outstanding,
        loan_count=len(loans_data),
        active_tab="debt_payoff",
        active_section="data",
    )


@app.route("/api/loans/<loan_id>/interest-rate", methods=["PATCH"])
def api_loan_interest_rate(loan_id: str):
    """Update the interest_rate_pct on a loan record."""
    from src.loans import load_loans, save_loans
    config = _load_config()
    body = request.get_json(force=True) or {}
    try:
        rate = float(body.get("interest_rate_pct", 0))
        if not (0 <= rate <= 100):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "interest_rate_pct must be 0–100"}), 400
    data = load_loans(config)
    loan = next((l for l in data.get("loans", []) if l["loan_id"] == loan_id), None)
    if not loan:
        return jsonify({"ok": False, "error": "loan not found"}), 404
    loan["interest_rate_pct"] = round(rate, 4)
    save_loans(data, config)
    return jsonify({"ok": True})


@app.route("/fy-summary")
def fy_summary_page():
    from src.db import load_transactions as _load_txns
    from src.reporter import prepare_fy_summary_data

    config = _load_config()
    df = _load_txns(config)
    cg_path = _data_path("capital_gains_file", "Data/capital_gains.json")
    try:
        cg_data = json.loads(cg_path.read_text(encoding="utf-8")) if cg_path.exists() else {}
    except Exception:
        cg_data = {}
    fc_path = _data_path("franking_credits_file", "Data/franking_credits.json")
    try:
        franking_data = json.loads(fc_path.read_text(encoding="utf-8")) if fc_path.exists() else {}
    except Exception:
        franking_data = {}
    sections = prepare_fy_summary_data(df, config, cg_data, franking_data)
    return render_template("fy_summary.html", sections=sections, active_tab="fy_summary")


@app.route("/net-worth")
def net_worth_page():
    from src.balance_tracker import load_balance_history
    from src.reporter import prepare_net_worth_data
    from src.manual_assets import (
        load_manual_assets, latest_value, latest_liability_balance,
        total_assets_value, total_liabilities_balance,
        super_projected_balance, compute_net_worth_history, project_net_worth,
        ASSET_TYPE_LABELS, LIABILITY_TYPE_LABELS,
    )

    config = _load_config()
    balances_df = load_balance_history(config)
    bank_data = prepare_net_worth_data(balances_df, config)
    manual_data = load_manual_assets(config)

    # Compute historical net worth series for the enhanced chart
    nw_history = compute_net_worth_history(balances_df, manual_data)

    # Compute per-asset projected / equity data
    assets_display = []
    for a in manual_data.get("assets", []):
        val = latest_value(a)
        entry = {**a, "current_value": val}
        if a.get("type") == "super":
            entry["projected"] = super_projected_balance(a)
        if a.get("type") == "property" and a.get("linked_liability_id"):
            liab = next(
                (l for l in manual_data.get("liabilities", [])
                 if l.get("liability_id") == a["linked_liability_id"]),
                None,
            )
            if liab:
                mortgage = latest_liability_balance(liab)
                equity = max(0.0, val - mortgage)
                lvr = round(mortgage / val * 100, 1) if val else None
                entry.update({"linked_liability": liab, "equity": equity, "lvr": lvr})
        assets_display.append(entry)

    liabilities_display = [
        {**l, "current_balance": latest_liability_balance(l)}
        for l in manual_data.get("liabilities", [])
    ]

    total_bank = bank_data.get("total_balance", 0.0)
    total_manual = total_assets_value(manual_data)
    total_liab = total_liabilities_balance(manual_data)
    current_net_worth = round(total_bank + total_manual - total_liab, 2)

    # Net worth projection (10-year, 3 scenarios)
    monthly_savings = float(request.args.get("monthly_savings") or 0)
    projection = project_net_worth(
        current_net_worth=current_net_worth,
        manual_data=manual_data,
        years=10,
        monthly_savings=monthly_savings,
    )

    return render_template(
        "net_worth.html",
        data=bank_data,
        assets=assets_display,
        liabilities=liabilities_display,
        asset_type_labels=ASSET_TYPE_LABELS,
        liability_type_labels=LIABILITY_TYPE_LABELS,
        total_bank=total_bank,
        total_manual=total_manual,
        total_liab=total_liab,
        net_worth=current_net_worth,
        projection_json=json.dumps(projection),
        monthly_savings=monthly_savings,
        nw_history_json=json.dumps([
            {"date": str(r["date"])[:10], "bank": r["bank"],
             "manual": r["manual"], "liabilities": r["liabilities"],
             "net_worth": r["net_worth"]}
            for _, r in nw_history.iterrows()
        ] if not nw_history.empty else []),
        active_tab="net_worth",
    )


@app.route("/api/assets", methods=["POST"])
def api_asset_create():
    """Create a new manual asset."""
    from src.manual_assets import load_manual_assets, save_manual_assets, new_asset_id
    config = _load_config()
    body = request.get_json(force=True) or {}
    asset_type = body.get("type", "other")
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    initial_value = body.get("initial_value")
    initial_date = body.get("initial_date") or str(_date.today())
    snapshot = [{"date": initial_date, "value": float(initial_value)}] if initial_value is not None else []
    asset = {
        "asset_id": new_asset_id(),
        "type": asset_type,
        "name": name,
        "snapshots": snapshot,
        "notes": body.get("notes", ""),
        "linked_liability_id": body.get("linked_liability_id"),
    }
    if asset_type == "super":
        asset.update({
            "employer_sg_rate_pct": body.get("employer_sg_rate_pct"),
            "retirement_age": body.get("retirement_age"),
            "birth_year": body.get("birth_year"),
            "expected_return_pct": body.get("expected_return_pct"),
            "annual_contribution": body.get("annual_contribution"),
        })
    if asset_type == "property":
        asset["address"] = body.get("address", "")
    data = load_manual_assets(config)
    data.setdefault("assets", []).append(asset)
    save_manual_assets(data, config)
    return jsonify({"ok": True, "asset_id": asset["asset_id"]})


@app.route("/api/assets/<asset_id>", methods=["PATCH", "DELETE"])
def api_asset_update(asset_id: str):
    """Update or delete a manual asset."""
    from src.manual_assets import load_manual_assets, save_manual_assets
    config = _load_config()
    data = load_manual_assets(config)
    idx = next((i for i, a in enumerate(data.get("assets", [])) if a["asset_id"] == asset_id), None)
    if idx is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if request.method == "DELETE":
        data["assets"].pop(idx)
        save_manual_assets(data, config)
        return jsonify({"ok": True})
    body = request.get_json(force=True) or {}
    asset = data["assets"][idx]
    for field in ("name", "notes", "linked_liability_id", "address",
                  "employer_sg_rate_pct", "retirement_age", "birth_year",
                  "expected_return_pct", "annual_contribution"):
        if field in body:
            asset[field] = body[field]
    save_manual_assets(data, config)
    return jsonify({"ok": True})


@app.route("/api/assets/<asset_id>/snapshots", methods=["POST"])
def api_asset_snapshot(asset_id: str):
    """Add a value snapshot to an asset."""
    from src.manual_assets import load_manual_assets, save_manual_assets
    config = _load_config()
    body = request.get_json(force=True) or {}
    try:
        value = float(body["value"])
        snap_date = str(body.get("date") or _date.today())
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "value required"}), 400
    data = load_manual_assets(config)
    asset = next((a for a in data.get("assets", []) if a["asset_id"] == asset_id), None)
    if not asset:
        return jsonify({"ok": False, "error": "not found"}), 404
    asset.setdefault("snapshots", []).append({"date": snap_date, "value": value})
    asset["snapshots"].sort(key=lambda s: s["date"])
    save_manual_assets(data, config)
    return jsonify({"ok": True})


@app.route("/api/liabilities", methods=["POST"])
def api_liability_create():
    """Create a new liability."""
    from src.manual_assets import load_manual_assets, save_manual_assets, new_liability_id
    config = _load_config()
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    initial_balance = body.get("initial_balance")
    initial_date = body.get("initial_date") or str(_date.today())
    snapshot = [{"date": initial_date, "balance": float(initial_balance)}] if initial_balance is not None else []
    liability = {
        "liability_id": new_liability_id(),
        "type": body.get("type", "other"),
        "name": name,
        "linked_asset_id": body.get("linked_asset_id"),
        "interest_rate_pct": body.get("interest_rate_pct"),
        "notes": body.get("notes", ""),
        "snapshots": snapshot,
    }
    data = load_manual_assets(config)
    data.setdefault("liabilities", []).append(liability)
    save_manual_assets(data, config)
    return jsonify({"ok": True, "liability_id": liability["liability_id"]})


@app.route("/api/liabilities/<liability_id>", methods=["PATCH", "DELETE"])
def api_liability_update(liability_id: str):
    """Update or delete a liability."""
    from src.manual_assets import load_manual_assets, save_manual_assets
    config = _load_config()
    data = load_manual_assets(config)
    idx = next(
        (i for i, l in enumerate(data.get("liabilities", [])) if l["liability_id"] == liability_id),
        None,
    )
    if idx is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if request.method == "DELETE":
        data["liabilities"].pop(idx)
        save_manual_assets(data, config)
        return jsonify({"ok": True})
    body = request.get_json(force=True) or {}
    liab = data["liabilities"][idx]
    for field in ("name", "notes", "linked_asset_id", "interest_rate_pct", "type"):
        if field in body:
            liab[field] = body[field]
    save_manual_assets(data, config)
    return jsonify({"ok": True})


@app.route("/api/liabilities/<liability_id>/snapshots", methods=["POST"])
def api_liability_snapshot(liability_id: str):
    """Add a balance snapshot to a liability."""
    from src.manual_assets import load_manual_assets, save_manual_assets
    config = _load_config()
    body = request.get_json(force=True) or {}
    try:
        balance = float(body["balance"])
        snap_date = str(body.get("date") or _date.today())
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "balance required"}), 400
    data = load_manual_assets(config)
    liab = next((l for l in data.get("liabilities", []) if l["liability_id"] == liability_id), None)
    if not liab:
        return jsonify({"ok": False, "error": "not found"}), 404
    liab.setdefault("snapshots", []).append({"date": snap_date, "balance": balance})
    liab["snapshots"].sort(key=lambda s: s["date"])
    save_manual_assets(data, config)
    return jsonify({"ok": True})


@app.route("/portfolio")
@require_module("investments")
def portfolio_page():
    """Investment portfolio — purchase lots and holdings summary with optional live prices."""
    from src.portfolio import load_portfolio, holdings_summary, fetch_prices

    config = _load_config()
    port_data = load_portfolio(config)
    lots = port_data.get("lots", [])
    tickers = list({(l.get("ticker") or "").upper() for l in lots if l.get("ticker")})

    prices: dict = {}
    price_error = False
    if request.args.get("refresh_prices") == "1" and tickers:
        try:
            prices = fetch_prices(tickers)
        except Exception:
            price_error = True

    holdings = holdings_summary(lots, prices)
    total_cost = round(sum(h["cost_basis"] for h in holdings), 2)
    total_value = (
        round(sum(h["current_value"] for h in holdings if h["current_value"] is not None), 2)
        if any(h["current_value"] is not None for h in holdings) else None
    )
    total_pl = (
        round(sum(h["unrealised_pl"] for h in holdings if h["unrealised_pl"] is not None), 2)
        if total_value is not None else None
    )

    return render_template(
        "portfolio.html",
        lots=lots,
        holdings=holdings,
        lots_json=json.dumps(lots),
        total_cost=total_cost,
        total_value=total_value,
        total_pl=total_pl,
        prices_loaded=bool(prices),
        price_error=price_error,
        active_tab="portfolio",
    )


@app.route("/api/portfolio/lots", methods=["POST"])
def api_portfolio_lot_create():
    """Add a purchase lot to the portfolio."""
    from src.portfolio import load_portfolio, save_portfolio, new_lot_id
    config = _load_config()
    body = request.get_json(force=True) or {}
    ticker = (body.get("ticker") or "").upper().strip()
    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400
    try:
        units = float(body["units"])
        cost_per_unit = float(body["cost_per_unit"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "units and cost_per_unit required"}), 400
    lot = {
        "lot_id": new_lot_id(),
        "ticker": ticker,
        "name": body.get("name") or ticker,
        "date": str(body.get("date") or _date.today()),
        "units": units,
        "cost_per_unit": cost_per_unit,
        "note": body.get("note", ""),
    }
    data = load_portfolio(config)
    data.setdefault("lots", []).append(lot)
    save_portfolio(data, config)
    return jsonify({"ok": True, "lot_id": lot["lot_id"]})


@app.route("/api/portfolio/lots/<lot_id>", methods=["DELETE"])
def api_portfolio_lot_delete(lot_id: str):
    """Delete a purchase lot."""
    from src.portfolio import load_portfolio, save_portfolio
    config = _load_config()
    data = load_portfolio(config)
    before = len(data.get("lots", []))
    data["lots"] = [l for l in data.get("lots", []) if l.get("lot_id") != lot_id]
    if len(data["lots"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    save_portfolio(data, config)
    return jsonify({"ok": True})


@app.route("/transactions")
def transactions_page():
    from src.db import load_transactions as _load_txns
    from src.reporter import prepare_transactions_data

    config = _load_config()
    df = _load_txns(config)
    data = prepare_transactions_data(df, config)
    return render_template(
        "transactions.html",
        active_section="data",
        active_tab="transactions",
        **data,
    )


@app.route("/dashboard")
def dashboard_page():
    from src.db import load_transactions as _load_txns
    from src.reporter import prepare_dashboard_data

    config = _load_config()
    from_date = request.args.get("from") or None
    to_date   = request.args.get("to")   or None
    df = _load_txns(config, since=from_date, until=to_date)
    reports_dir = BASE_DIR / "reports"
    data = prepare_dashboard_data(df, config, reports_dir)
    return render_template(
        "dashboard.html",
        active_section="dashboard",
        dash_from=from_date or "",
        dash_to=to_date or "",
        **data,
    )


@app.route("/transfers")
@require_module("transfers")
def transfers_page():
    from src.reporter import prepare_transfers_data

    config = _load_config()
    data = prepare_transfers_data(config)
    return render_template(
        "transfers.html",
        pending_html=data["pending_html"],
        confirmed_html=data["confirmed_html"],
        dismissed_html=data["dismissed_html"],
        pending_count=data["pending_count"],
        confirmed_count=data["confirmed_count"],
        dismissed_count=data["dismissed_count"],
        active_tab="transfers",
    )


@app.route("/review")
def review_page():
    from src.db import load_transactions as _load_txns
    from src.reporter import prepare_review_data

    config = _load_config()
    df = _load_txns(config)
    data = prepare_review_data(df, config)
    return render_template(
        "review.html",
        has_data=data["has_data"],
        rows_html=data["rows_html"],
        num_groups=data["num_groups"],
        num_txns=data["num_txns"],
        cat_options=data["cat_options"],
        active_tab="review",
    )


def _write_pipeline_error(msg: str) -> None:
    """Record a pipeline failure in run_metrics.json so the error badge shows it."""
    p = _data_path("run_metrics_file", "Data/run_metrics.json")
    try:
        existing = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        existing = {}
    existing["pipeline_error"] = msg
    existing["updated_at"] = _dt.now().isoformat(timespec="seconds")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(existing, indent=2), "utf-8")
    except Exception:
        pass


def _clear_pipeline_error() -> None:
    p = _data_path("run_metrics_file", "Data/run_metrics.json")
    try:
        existing = json.loads(p.read_text("utf-8")) if p.exists() else {}
        existing.pop("pipeline_error", None)
        p.write_text(json.dumps(existing, indent=2), "utf-8")
    except Exception:
        pass


def _run_pipeline_bg(trigger: str = "auto") -> bool:
    """Run the full import → categorise → report pipeline in a background thread.

    Returns False immediately if a run is already in progress.
    Stdout is captured to Data/pipeline.log; failures are surfaced via the error badge.
    """
    if not _import_lock.acquire(blocking=False):
        return False

    _pipeline_status.update({
        "running": True, "trigger": trigger,
        "started_at": _dt.now().isoformat(timespec="seconds"),
        "error": None,
    })

    def _worker():
        log_path = BASE_DIR / "Data" / "pipeline.log"
        try:
            env = _build_env()
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "finance_analyser.py")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(BASE_DIR), env=env,
            )
            lines = []
            for line in proc.stdout:
                lines.append(line.rstrip())
            proc.wait()
            log_path.write_text(
                f"[{_dt.now().isoformat(timespec='seconds')}] trigger={trigger}\n"
                + "\n".join(lines), encoding="utf-8",
            )
            if proc.returncode != 0:
                msg = f"exit {proc.returncode}"
                _pipeline_status["error"] = msg
                _write_pipeline_error(msg)
            else:
                _pipeline_status["error"] = None
                _clear_pipeline_error()
        except Exception as exc:
            _pipeline_status["error"] = str(exc)
            _write_pipeline_error(str(exc))
            try:
                log_path.write_text(
                    f"[{_dt.now().isoformat(timespec='seconds')}] trigger={trigger} EXCEPTION: {exc}",
                    encoding="utf-8",
                )
            except Exception:
                pass
        finally:
            _pipeline_status.update({
                "running": False,
                "finished_at": _dt.now().isoformat(timespec="seconds"),
            })
            _import_lock.release()

    threading.Thread(target=_worker, daemon=True, name="pipeline-bg").start()
    return True


def _start_file_watcher(config: dict) -> None:
    """Item 7: Auto-trigger import when files land in Raw Data."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        logger.warning("  watchdog not installed — auto-import on file drop disabled")
        logger.warning("  (pip install watchdog to enable)")
        return

    input_dir = Path(config["data"]["input_dir"])
    input_dir.mkdir(parents=True, exist_ok=True)
    _EXTS = {".csv", ".CSV", ".pdf", ".PDF", ".html", ".HTML"}
    _debounce: dict = {}

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            if Path(event.src_path).suffix not in _EXTS:
                return
            key = event.src_path
            if key in _debounce:
                _debounce[key].cancel()
            t = threading.Timer(3.0, lambda: _run_pipeline_bg("watcher"))
            _debounce[key] = t
            t.start()

    observer = Observer()
    observer.schedule(_Handler(), str(input_dir), recursive=False)
    observer.daemon = True
    observer.start()
    logger.info(f"  File watcher active: {input_dir}")


if __name__ == "__main__":
    REPORTS_DIR.mkdir(exist_ok=True)
    _cfg = _load_config()
    from src.logging_config import setup_logging as _setup_logging
    _setup_logging(_cfg)
    from src.config_validator import validate_config as _validate_config
    _CONFIG_ISSUES[:] = _validate_config(_cfg, BASE_DIR)
    for _issue in _CONFIG_ISSUES:
        logger.warning(f"  Config issue: {_issue}")
    # Use a stable secret key from config if provided (sessions survive restarts)
    _sk = ((_cfg.get("server") or {}).get("secret_key") or "") if _cfg else ""
    if _sk:
        app.secret_key = _sk.encode("utf-8")
    if _cfg:
        try:
            from src.db import open_db as _open_db_startup, init_db as _init_db, seed_accounts as _seed_accounts, run_data_quality_checks as _run_dq
            with _open_db_startup(_cfg) as _conn:
                _init_db(_conn)
                _seed_accounts(_conn, _cfg)
                _run_dq(_conn)
        except Exception:
            pass
        _start_file_watcher(_cfg)
        # Auto-run pipeline at startup if Raw Data has unprocessed files
        def _startup_check():
            time.sleep(2)  # let Flask settle before spawning a subprocess
            try:
                raw_dir = BASE_DIR / _cfg.get("data", {}).get("input_dir", "Data/Raw Data")
                if any(f for f in raw_dir.iterdir() if f.is_file()):
                    _run_pipeline_bg("startup")
            except Exception:
                pass
        threading.Thread(target=_startup_check, daemon=True, name="startup-check").start()
        _repo = ((_cfg.get("server") or {}).get("update_check_repo") or "")
        if _repo:
            threading.Thread(
                target=_check_for_updates, args=(_repo,), daemon=True, name="update-check"
            ).start()
    logger.info(f"\n  Personal Finance Analyser")
    logger.info(f"  Open in browser: http://localhost:{PORT}")
    logger.info(f"  Press Ctrl+C to stop.\n")

    # When running as a bundled exe, open the browser automatically after Flask is up.
    if getattr(sys, "frozen", False):
        import webbrowser as _wb
        def _open_browser():
            time.sleep(1.5)
            _wb.open(f"http://localhost:{PORT}")
        threading.Thread(target=_open_browser, daemon=True, name="open-browser").start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
