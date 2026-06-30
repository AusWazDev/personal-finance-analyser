"""Module toggle system — enables/disables feature modules."""
import json
from datetime import date
from pathlib import Path

_DEFAULT_FILE = "Data/modules.json"

DEFAULT_MODULES: dict[str, bool] = {
    "budgets":         True,
    "business":        True,
    "investments":     True,
    "loans":           True,
    "goals":           True,
    "payin4":          True,
    "transfers":       True,
    "commitments":     True,
    "recommendations": True,
    "coverage":        True,
}


def load_modules(config: dict) -> dict:
    """Return {key: bool} dict of enabled modules.

    Merges saved state with DEFAULT_MODULES so newly-added module keys default to True.
    """
    path = Path(config.get("data", {}).get("modules_file", _DEFAULT_FILE))
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            saved = data.get("modules", {})
            return {k: bool(saved.get(k, v)) for k, v in DEFAULT_MODULES.items()}
        except Exception:
            pass
    return dict(DEFAULT_MODULES)


def save_modules(modules: dict, config: dict) -> None:
    """Persist {key: bool} to Data/modules.json."""
    path = Path(config.get("data", {}).get("modules_file", _DEFAULT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {k: bool(v) for k, v in modules.items() if k in DEFAULT_MODULES}
    path.write_text(
        json.dumps({"modules": cleaned, "updated_at": date.today().isoformat()},
                   indent=2, sort_keys=True),
        "utf-8",
    )


def is_enabled(key: str | None, config: dict) -> bool:
    """Return True if the module is enabled. None key means always-on."""
    if key is None:
        return True
    return load_modules(config).get(key, True)
