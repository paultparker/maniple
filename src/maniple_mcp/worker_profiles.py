"""
Named worker profiles and per-worker launch-restriction helpers.

A *profile* is a preset bundle of WorkerConfig fields. Currently one exists:

``web-reader``
    A restricted worker for reading untrusted web content. It is launched in
    ``dontAsk`` permission mode and ``--restricted`` with a positive ``--tools``
    list of WebFetch and WebSearch only (no file-read tool), ``mcp__*`` and every
    write/shell/messaging tool
    denied through the per-worker ``--settings`` file, claude.ai connectors
    disabled, and no worktree. It exists so research workers need not run with
    ``skip_permissions``.

Also home to ``deep_merge_settings`` (how ``worker_settings`` is folded into
maniple's own per-worker settings JSON) and ``validate_worker_launch_fields``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

PROFILE_WEB_READER = "web-reader"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WEB_READER_PROFILE: dict[str, Any] = {
    "skip_permissions": False,
    "trust_project_mcp": False,
    "use_worktree": False,
    "extra_args": [
        "--permission-mode",
        "dontAsk",
        # --restricted ignores the user's/project's settings files (so Paul's
        # broad Read(**)/Bash(*) allows don't apply) while the per-worker
        # --settings file still does. --tools is WebFetch,WebSearch ONLY: no
        # file-read tool, so a fetched page can't make the worker read ~/.env
        # (lethal trifecta). --allowedTools is required because dontAsk under
        # --restricted otherwise denies WebFetch to non-pre-approved domains.
        "--restricted",
        "--strict-mcp-config",
        "--tools",
        "WebFetch,WebSearch",
        "--allowedTools",
        "WebFetch,WebSearch",
    ],
    "env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    "worker_settings": {
        "disableClaudeAiConnectors": True,
        "permissions": {
            "defaultMode": "dontAsk",
            "blockReadsOutsideWorkingDirectories": True,
            "deny": [
                "mcp__*",
                "Bash",
                "PowerShell",
                "Monitor",
                "Edit",
                "Write",
                "NotebookEdit",
                "Agent",
                "Skill",
                "Workflow",
                "SendMessage",
                "ListAgents",
                "RemoteTrigger",
                "CronCreate",
                "ScheduleWakeup",
                "PushNotification",
                "SendUserFile",
                "Artifact",
                "EnterWorktree",
            ],
        },
    },
}

PROFILES: dict[str, dict[str, Any]] = {PROFILE_WEB_READER: WEB_READER_PROFILE}


class WorkerConfigError(ValueError):
    """Raised for an invalid worker launch configuration."""


def deep_merge_settings(base: dict, extra: dict) -> dict:
    """Return a new dict: ``extra`` deep-merged into ``base``.

    - dict + dict: merged recursively.
    - list + list: concatenated (base entries first, then extra). This is what
      makes ``hooks.Stop`` etc. APPEND to maniple's own hooks rather than
      replace them, and ``permissions.deny`` accumulate.
    - anything else: ``extra`` wins.

    Neither input is mutated.
    """
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if key in result:
            current = result[key]
            if isinstance(current, dict) and isinstance(value, dict):
                result[key] = deep_merge_settings(current, value)
                continue
            if isinstance(current, list) and isinstance(value, list):
                result[key] = current + copy.deepcopy(value)
                continue
        result[key] = copy.deepcopy(value)
    return result


def validate_worker_launch_fields(config: dict) -> None:
    """Validate worker_settings / extra_args / env / output_file types."""
    ws = config.get("worker_settings")
    if ws is not None and not isinstance(ws, dict):
        raise WorkerConfigError("'worker_settings' must be an object")
    ea = config.get("extra_args")
    if ea is not None and (
        not isinstance(ea, list) or not all(isinstance(a, str) for a in ea)
    ):
        raise WorkerConfigError("'extra_args' must be a list of strings")
    env = config.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise WorkerConfigError("'env' must be an object of string to string")
        for key in env:
            if not _ENV_KEY_RE.match(key):
                raise WorkerConfigError(f"Invalid environment variable name: {key!r}")
    of = config.get("output_file")
    if of is not None:
        if not isinstance(of, str) or not of:
            raise WorkerConfigError("'output_file' must be a non-empty string")
        if not of.startswith("/"):
            raise WorkerConfigError(f"'output_file' must be an absolute path: {of!r}")


# Entries tolerated in a web-reader directory: OS cruft only. Maniple itself
# creates nothing in the project directory (transcripts live under ~/.claude).
_WEB_READER_TOLERATED = {".DS_Store"}


def check_web_reader_dir(path) -> None:
    """Require ``path`` to be an empty dedicated directory (no .git, no files)."""
    from pathlib import Path

    p = Path(path)
    msg = f"web-reader requires an empty dedicated directory (got {p})"
    if not p.is_dir():
        raise WorkerConfigError(msg)
    if (p / ".git").exists() or (p / ".git").is_symlink():
        raise WorkerConfigError(msg)
    if any(e.name not in _WEB_READER_TOLERATED for e in p.iterdir()):
        raise WorkerConfigError(msg)


def expand_profile(config: dict) -> dict:
    """Return ``config`` with its ``profile`` (if any) expanded.

    Explicit fields alongside the profile override scalars and extend
    collections: ``extra_args`` are appended after the profile's, ``env`` is
    merged (explicit wins), ``worker_settings`` is deep-merged (lists
    concatenate). The ``profile`` key is kept so callers can see it.

    Raises WorkerConfigError for an unknown profile, for profile=web-reader
    combined with skip_permissions=True, and for malformed fields.
    """
    validate_worker_launch_fields(config)
    profile_name = config.get("profile")
    if profile_name is None:
        return config
    if profile_name not in PROFILES:
        raise WorkerConfigError(
            f"Unknown profile {profile_name!r}. Known profiles: "
            f"{', '.join(sorted(PROFILES))}"
        )
    if profile_name == PROFILE_WEB_READER and config.get("skip_permissions") is True:
        raise WorkerConfigError(
            "profile='web-reader' cannot be combined with skip_permissions=True "
            "(bypass mode would override the profile's restrictions)"
        )

    preset = copy.deepcopy(PROFILES[profile_name])
    expanded = dict(config)
    for key in ("skip_permissions", "trust_project_mcp", "use_worktree"):
        if expanded.get(key) is None:
            expanded[key] = preset[key]
    expanded["extra_args"] = preset["extra_args"] + list(config.get("extra_args") or [])
    expanded["env"] = {**preset["env"], **(config.get("env") or {})}
    expanded["worker_settings"] = deep_merge_settings(
        preset["worker_settings"], config.get("worker_settings") or {}
    )
    return expanded
