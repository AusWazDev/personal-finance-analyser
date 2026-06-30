# Session Handoff — Personal Finance Analyser

**Date:** June 2026  
**Tests:** 356 passing  
**Working directory:** `C:\Users\wjl25\OneDrive\Projects\Personal Financial Tracking and Reporting`

---

## What was built this session

### Budget management system (complete)
- **`src/budgets.py`** — new module: `load_budgets(config)`, `save_budgets()`, `suggest_budgets(conn, months)`
- **`Data/budgets.json`** — budgets moved out of `config.yaml` entirely; auto-migrates from config on first load
- **`templates/settings/budgets.html`** — Settings > Budgets page with history-based suggestions (avg monthly spend rounded up to nearest $25), "Use" / "Use all" buttons
- **`server.py`** — new routes: `GET/POST /api/budgets`, `GET /api/budgets/suggest`, `GET /settings/budgets`; `_load_budget_status()` updated to use `load_budgets()`
- **`src/reporter.py`** — `budget_json` line updated to use `load_budgets()` instead of `cfg.get("budgets")`
- **`src/utils.py`** — "Budgets" tab added to `SETTINGS_TABS`
- **`tests/test_budgets.py`** — 14 tests covering load/save/migrate/suggest

### Full feature roadmap (complete)
- **`docs/roadmap.html`** — 44 items across 6 phases, viewable at `http://localhost:5100/docs/roadmap.html`
- Key design decision: module toggle system — 10 toggleable modules so users can hide features they don't need

---

## Current application state

### Tech stack
Python 3.13 / Flask / SQLite (`Data/finance.db`) / pandas / Plotly / Claude Haiku (categorisation) + Sonnet (recommendations)

### Key architecture conventions (must follow)
- `_load_config()` is the single config loader in `server.py` — never inline `yaml.safe_load()`
- `_data_path(config_key, default)` resolves all data file paths — never hardcode
- `load_budgets(config)` from `src/budgets.py` — never `cfg.get("budgets")`
- `SETTINGS_TABS` and `NAV_TABS` in `src/utils.py` are the single source of truth for navigation
- `init_db()` called once at server startup, not inside route handlers
- All new data stores follow the `Data/*.json` pattern with a corresponding `src/*.py` module

### Navigation structure
- **Top nav sections:** Data (default), Settings, Help
- **Data tabs** (from `NAV_TABS` in `src/utils.py`): All Transactions, Review, Transfers, FY Summary, Net Worth, Commitments, Reimbursements, Superseded, Recommendations, Coverage, Capital Gains, Cash Flow, Goals & Loans
- **Settings tabs** (from `SETTINGS_TABS`): Accounts, Merchant Rules, Budgets

### Data files
`Data/finance.db` (SQLite, primary store), `Data/budgets.json`, `Data/loans.json`, `Data/financial_goals.json`, `Data/modules.json` (does not exist yet — to be created), `Data/merchant_rules.json`, `Data/categorisation_cache.json`, `Data/commitments.json`, `Data/transfer_candidates.json`, `Data/payin4_groups.json`, `Data/run_metrics.json`

### Test suite
356 tests in `tests/`. Run with `python -m pytest`. Must pass after every change.

---

## Roadmap — full picture

See `docs/roadmap.html` for the interactive version. Summary:

| Phase | Focus | Items | Status |
|---|---|---|---|
| **0** | Distribution Foundation | 9 | **Next — start here** |
| **1** | UX Modernisation | 9 | Planned |
| **2** | Financial Intelligence | 7 | Planned |
| **3** | Balance Sheet Completeness | 6 | Planned |
| **4** | Tax & Compliance (AU) | 5 | Planned |
| **5** | Ecosystem & Integration | 5 | Planned |
| **B** | Existing Backlog | 3 | Next (small, do alongside Phase 0) |

---

## Phase 0 — Distribution Foundation (next session starts here)

These must be built in this order (each unlocks the next):

### Item 0.1 — Module toggle system ← START HERE
**What:** `src/modules.py` module + `Data/modules.json` store. Nav filtering by enabled modules. Route guard decorator.

**10 toggleable modules:**

| Key | Name | Default on |
|---|---|---|
| `budgets` | Budgets | Yes |
| `business` | Business & Tax | Yes |
| `investments` | Investments | Yes |
| `loans` | Loans | Yes |
| `goals` | Goals | Yes |
| `payin4` | Pay-in-4 / PayPal | Yes |
| `transfers` | Transfer Detection | Yes |
| `commitments` | Commitments | Yes |
| `recommendations` | AI Recommendations | Yes |
| `coverage` | Statement Coverage | Yes |

**Core (always on, never toggleable):** Transactions, Review, Cash Flow, Net Worth, Settings, Health Metrics (future)

**Implementation plan:**
1. **`src/modules.py`** — `load_modules(config)`, `save_modules(modules, config)`, `DEFAULT_MODULES` dict, `is_enabled(key, config)` helper
2. **`NAV_TABS` in `src/utils.py`** — add 4th field (module key or `None` for always-on) to every entry
3. **`SETTINGS_TABS` in `src/utils.py`** — same treatment  
4. **`server.py`** — filter `nav_tabs` and `settings_tabs` Jinja globals through enabled modules at request time (move from module-level assignment into `@app.before_request` or a context processor)
5. **`server.py`** — `require_module(key)` decorator: if module disabled, redirect to `/` with flash message
6. **`server.py`** — `GET /settings/modules` route + `POST /api/modules` to save
7. **`templates/settings/modules.html`** — toggle switch page (same style as `settings/budgets.html`)
8. **`tests/test_modules.py`** — load/save/default/filter tests

**NAV_TABS with module keys (how it should look after edit):**
```python
NAV_TABS = [
    ("All Transactions", "/reports/transactions.html", "transactions", None),
    ("Review",           "/reports/review.html",       "review",        None),
    ("Transfers",        "/reports/transfers.html",    "transfers",     "transfers"),
    ("FY Summary",       "/reports/fy_summary.html",   "fy_summary",    "business"),
    ("Net Worth",        "/reports/net_worth.html",    "net_worth",     None),
    ("Commitments",      "/commitments",               "commitments",   "commitments"),
    ("Reimbursements",   "/reimbursements",            "reimbursements","business"),
    ("Superseded",       "/superseded-pairs",          "superseded_pairs", "transfers"),
    ("Recommendations",  "/recommendations",           "recommendations","recommendations"),
    ("Coverage",         "/coverage",                  "coverage",      "coverage"),
    ("Capital Gains",    "/capital-gains",             "capital_gains", "investments"),
    ("Cash Flow",        "/cash-flow",                 "cash_flow",     None),
    ("Goals & Loans",    "/financial-goals",           "goals",         "goals"),
]

SETTINGS_TABS = [
    ("Accounts",       "/settings/accounts",        "accounts",       None),
    ("Merchant Rules", "/settings/merchant-rules",  "merchant_rules", None),
    ("Budgets",        "/settings/budgets",         "budgets",        "budgets"),
    ("Modules",        "/settings/modules",         "modules",        None),
]
```

**`Data/modules.json` format:**
```json
{
  "modules": {
    "budgets": true,
    "business": true,
    "investments": true,
    "loans": true,
    "goals": true,
    "payin4": true,
    "transfers": true,
    "commitments": true,
    "recommendations": true,
    "coverage": true
  },
  "updated_at": "2026-06-16"
}
```

### Item 0.2 — First-run setup wizard
Shown when `Data/modules.json` does not exist. One-page `/setup` route with checkboxes. Saves `modules.json` then redirects to dashboard. Bypasses auth check if set. After this, wizard never shows again.

### Item 0.3 — Versioned schema migrations
Replace ad-hoc `init_db()` migrations with a `schema_version` table. Numbered migration functions applied in sequence. Safe for users upgrading between app versions.

### Item 0.4 — Structured logging
Replace `print()` with Python `logging` module. Log to `Data/app.log` with rotation. INFO/WARNING/ERROR levels.

### Item 0.5 — Startup config validation
On server start, validate config. Show in-app error banner for failures rather than silent mid-import crashes.

### Item 0.6 — Generic CSV importer with AI column detection
User uploads unknown bank CSV → Claude Haiku maps columns → saved as bank profile in `Data/bank_profiles.json`. Future uploads reuse profile.

### Item 0.7 — Pluggable AI backend
Abstract categorisation API behind interface. Claude API (current) or Ollama (local, zero cost). Config: `ai_backend: claude` or `ai_backend: ollama`.

### Item 0.8 — Database encryption at rest
SQLCipher or application-level encryption of `Data/finance.db`. Passphrase from OS keychain. Lower priority for personal use — can defer if not distributing immediately.

---

## Existing backlog (do alongside Phase 0 — all small)

### B.1 — Goals "Use auto amount" button
`calculate_goal_balance()` already computes `auto_current` and displays it inline (cyan dot in goal cards and table view). Just need a "Use" button that calls `POST /api/financial-goals` with the auto value to write it to `current_amount`.
- File: `templates/financial_goals.html` — add button in the auto_current display div
- Route: `POST /api/financial-goals` already exists — just pass `current_amount: auto_current`

### B.2 — Family Loan transfer ↔ loan auto-link
When `api_transfer_decision()` confirms a "Family Loan" pair and `loan_link_needed` is true, auto-find the matching loan record (by contact name matching the transfer label) and add the txn_ids to its `linked_receipt_txn_ids` or `linked_repayment_txn_ids`.
- File: `server.py` — `api_transfer_decision()` — after setting `loan_link_needed`, attempt auto-link
- Module: `src/loans.py` — may need a `link_txn_to_loan(loan_id, txn_id, side)` helper

### B.3 — Upload modal account selector
When content-sniffing fails to identify the account for an uploaded file, show a per-file account dropdown in the upload modal. Currently fails silently.

---

## Files to read at session start (to get context)

- `CLAUDE.md` — full architecture reference (always loaded)
- `src/utils.py` — NAV_TABS, SETTINGS_TABS, SUBCATS (small file, read in full)
- `src/budgets.py` — reference for the modules.py pattern to follow
- `server.py` lines 47–55 — how nav tabs are currently injected into Jinja globals
- `templates/settings/budgets.html` — UI pattern to follow for modules page

---

## Key decisions already made (don't re-litigate)

1. Module toggles live in `Data/modules.json` (not `config.yaml`) — consistent with budgets pattern
2. 10 modules to toggle (list above) — core always-on
3. Settings > Modules is the toggle UI (toggle switches, same dark theme as budgets page)
4. First-run wizard only shown once (absence of `Data/modules.json` triggers it)
5. Nav filtering happens at request time via Jinja globals, not at module level
6. Phase 0 must complete before Phase 1 — module system underpins everything

---

## Start command for next session

> "Continue building the Personal Finance Analyser. We have a full roadmap at docs/roadmap.html. Start with Phase 0, Item 0.1 — the module toggle system. Read CLAUDE.md, src/utils.py, src/budgets.py (as pattern reference), and server.py lines 47–55 first, then implement src/modules.py, update NAV_TABS/SETTINGS_TABS in src/utils.py, wire the nav filtering in server.py, add the Settings > Modules page, and write tests. The detailed spec is in docs/session_handoff.md under Item 0.1."
