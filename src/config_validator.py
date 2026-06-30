"""Startup config validation — returns human-readable issue strings."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def validate_config(config: dict, base_dir: Path | None = None) -> list[str]:
    """Return a list of issue strings describing config problems.

    An empty list means the config is valid.  Issues are ordered from most to
    least critical.  base_dir is used to resolve relative paths for existence
    checks; omit it to skip path checks.
    """
    if not config:
        return ["config.yaml is missing or empty — the app cannot run without it"]

    issues: list[str] = []
    data = config.get("data")

    if not data:
        issues.append(
            "Missing required section: 'data' — add data.input_dir and data.output_dir to config.yaml"
        )
        return issues

    if not data.get("input_dir"):
        issues.append("data.input_dir is not set — statements cannot be imported")

    if not data.get("output_dir"):
        issues.append("data.output_dir is not set — reports cannot be generated")

    if base_dir and data.get("input_dir"):
        input_path = base_dir / data["input_dir"]
        if not input_path.exists():
            issues.append(
                f"data.input_dir '{data['input_dir']}' does not exist "
                "(it will be created on first import)"
            )

    accounts = config.get("accounts")
    if not accounts:
        issues.append(
            "No accounts configured — add at least one entry under 'accounts' in config.yaml"
        )
    elif isinstance(accounts, dict):
        for key, acct in accounts.items():
            if not isinstance(acct, dict):
                continue
            if not acct.get("file_pattern"):
                issues.append(f"Account '{key}' is missing 'file_pattern' — it will never match any file")
            if not acct.get("display_name"):
                issues.append(f"Account '{key}' is missing 'display_name'")

    income = config.get("income") or {}
    if not income.get("account_holder_name"):
        issues.append(
            "income.account_holder_name is not set — transfer detection may flag your own payments as income"
        )

    return issues
