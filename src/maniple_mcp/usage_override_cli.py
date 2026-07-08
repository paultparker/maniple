"""
CLI helpers for `maniple usage-override`.

Thin formatting-free wrappers around usage_override.py, scoped to the
"global" override -- mirrors how config_cli.py backs the `config` CLI
subcommand while server.py::main() only handles argument parsing and
printing.
"""

from __future__ import annotations

from pathlib import Path

from .config import ConfigError, load_config
from . import usage_override

GLOBAL_SCOPE = "global"


def _state_file() -> str:
    # Mirrors override_usage_pause's fallback: a missing/invalid config must
    # never crash the CLI, just fall through to usage_override's own
    # unreadable-state-file handling.
    try:
        return load_config().usage_pause.state_file
    except ConfigError:
        return "/nonexistent-usage-pause-state-file"


def advance_global(*, override_dir: Path | None = None) -> dict:
    """Advance the global override one rung and return the result dict."""

    expires_at = usage_override.resolve_expires_at(_state_file())
    return usage_override.advance_override(GLOBAL_SCOPE, expires_at, override_dir)


def clear_global(*, override_dir: Path | None = None) -> bool:
    """Clear the global override. Returns True if a file was removed."""

    return usage_override.clear_override(GLOBAL_SCOPE, override_dir)


def status_global(*, override_dir: Path | None = None) -> dict:
    """Return the global override's current rung, used_percentage, and expiry.

    `rung` is "base" (no override), 0.90, 0.95, or "unlimited".
    """

    override = usage_override.read_override(GLOBAL_SCOPE, override_dir)
    if override is None:
        rung: float | str = "base"
        expires_at = None
    else:
        rung = "unlimited" if override["threshold"] is None else override["threshold"]
        expires_at = override["expires_at"]

    return {
        "rung": rung,
        "used_percentage": usage_override.read_used_percentage(_state_file()),
        "expires_at": expires_at,
    }


__all__ = ["advance_global", "clear_global", "status_global"]
