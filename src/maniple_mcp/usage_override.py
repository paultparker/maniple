"""Shared read/advance/clear/install logic for the usage-pause escalating
override ladder.

This is the single source of truth behind both the `override_usage_pause` /
`clear_usage_override` MCP tools (tools/override_usage_pause.py) and the
`maniple usage-override` / `maniple install-global-usage-guard` CLI
subcommands (server.py::main()) -- mirroring how config_cli.py backs both
the `config` CLI subcommand and its call sites.

Override files live at `<override_dir>/<scope>.json` as
`{"threshold": 0.90 | 0.95 | null, "expires_at": <epoch>}` and are read
directly by the standalone hook script's `_read_override()`
(usage_pause_hook.py) -- keep the shape in sync with that script if it ever
changes.

Ladder: no override (base) -> 0.90 -> 0.95 -> null (unlimited). Each call to
`advance_override` moves one rung; requesting a rung past unlimited is a
no-op that reports `already_unlimited=True` rather than erroring.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# The rungs above "base" (no override file), in order. `None` means
# unlimited (no usage-pause threshold at all).
_LADDER: tuple[float | None, ...] = (0.90, 0.95, None)


def default_override_dir() -> Path:
    """Return the fixed (non-configurable) override directory."""

    return Path.home() / ".maniple" / "usage_override"


def _override_path(scope: str, override_dir: Path | None) -> Path:
    d = override_dir if override_dir is not None else default_override_dir()
    return d / f"{scope}.json"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (temp file + os.replace).

    Mirrors iterm_utils._write_if_changed's temp-file pattern, but always
    writes (no content-diffing) since callers here always have fresh
    content to persist (an override's `expires_at`, or the rendered hook
    script).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` as JSON to `path` atomically (temp file + os.replace)."""

    _atomic_write_text(path, json.dumps(data))


def read_override(scope: str, override_dir: Path | None = None) -> dict | None:
    """Return the current override entry for `scope`, or None if there is
    no valid unexpired override (missing file, malformed, or expired).

    This is the package-code counterpart to usage_pause_hook.py's
    standalone `_read_override()` -- keep behavior identical.
    """

    path = _override_path(scope, override_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "threshold" not in data or "expires_at" not in data:
        return None

    expires_at = data["expires_at"]
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    if expires_at <= time.time():
        return None

    threshold = data["threshold"]
    if threshold is not None and (
        not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
    ):
        return None

    return {"threshold": threshold, "expires_at": expires_at}


def advance_override(
    scope: str, expires_at: float, override_dir: Path | None = None
) -> dict:
    """Advance `scope`'s override one rung up the ladder and persist it.

    Returns {"new_rung": 0.90 | 0.95 | "unlimited", "expires_at": ...,
    "already_unlimited": bool}. If already at the unlimited rung, the file
    is left untouched and `already_unlimited` is True.
    """

    current = read_override(scope, override_dir)
    current_threshold = current["threshold"] if current is not None else "base"

    if current is not None and current_threshold is None:
        return {
            "new_rung": "unlimited",
            "expires_at": current["expires_at"],
            "already_unlimited": True,
        }

    if current_threshold == "base":
        next_threshold = _LADDER[0]
    else:
        idx = _LADDER.index(current_threshold)
        next_threshold = _LADDER[idx + 1]

    path = _override_path(scope, override_dir)
    _atomic_write_json(path, {"threshold": next_threshold, "expires_at": expires_at})

    return {
        "new_rung": "unlimited" if next_threshold is None else next_threshold,
        "expires_at": expires_at,
        "already_unlimited": False,
    }


def clear_override(scope: str, override_dir: Path | None = None) -> bool:
    """Delete `scope`'s override file. Returns True if a file was removed."""

    path = _override_path(scope, override_dir)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _load_state_file(state_file: str) -> dict | None:
    try:
        with open(state_file, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _five_hour_rate_limits(state_file: str) -> dict | None:
    """Return the `rate_limits.five_hour` dict from `state_file`, or None if
    the file/JSON/either level is unreadable, malformed, or wrong-typed."""

    data = _load_state_file(state_file)
    if data is None:
        return None
    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    five_hour = rate_limits.get("five_hour")
    return five_hour if isinstance(five_hour, dict) else None


def resolve_expires_at(state_file: str) -> float:
    """Return `rate_limits.five_hour.resets_at` from `state_file`, falling
    back to `now + 5h` if unreadable, malformed, or missing."""

    fallback = time.time() + 5 * 3600
    five_hour = _five_hour_rate_limits(state_file)
    if five_hour is None:
        return fallback
    resets_at = five_hour.get("resets_at")
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        return fallback
    return resets_at


def read_used_percentage(state_file: str) -> float | None:
    """Return `rate_limits.five_hour.used_percentage` from `state_file`, or
    None if unreadable, malformed, or missing."""

    five_hour = _five_hour_rate_limits(state_file)
    if five_hour is None:
        return None
    used = five_hour.get("used_percentage")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return used


def install_global_usage_guard(
    threshold: float = 0.80, dest_dir: Path | None = None
) -> dict:
    """Write the rendered usage-pause hook script (scope "global") to
    `dest_dir` (default `~/.claude/hooks/`) atomically, and build the
    PreToolUse hooks JSON snippet the user must merge into
    `~/.claude/settings.json` by hand. Never touches settings.json itself.

    Returns {"script_path": Path, "snippet": str}.
    """

    from .config import UsagePauseConfig
    from .usage_pause_hook import render_hook_script

    d = dest_dir if dest_dir is not None else (Path.home() / ".claude" / "hooks")
    script_path = d / "usage-pause-global.py"
    _atomic_write_text(script_path, render_hook_script())

    import shlex

    defaults = UsagePauseConfig()
    override_dir = default_override_dir()
    command = (
        f"python3 {shlex.quote(str(script_path))} {threshold} "
        f"{shlex.quote(defaults.state_file)} {defaults.max_stale_seconds} "
        f"global {shlex.quote(str(override_dir))} || true"
    )
    snippet_obj = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": command},
                    ]
                }
            ]
        }
    }
    snippet = json.dumps(snippet_obj, indent=2)

    return {"script_path": script_path, "snippet": snippet}


__all__ = [
    "default_override_dir",
    "read_override",
    "advance_override",
    "clear_override",
    "resolve_expires_at",
    "read_used_percentage",
    "install_global_usage_guard",
]
