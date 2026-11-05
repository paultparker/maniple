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
import shlex
from typing import Any

from .config import EFFORT_LEVELS

PROFILE_WEB_READER = "web-reader"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Characters that are inert on an unquoted shell line, as --model's value is.
_SAFE_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")

WEB_READER_PROFILE: dict[str, Any] = {
    "skip_permissions": False,
    "trust_project_mcp": False,
    "use_worktree": False,
    "extra_args": [
        "--permission-mode",
        "dontAsk",
        # --restricted ignores the user's/project's settings files (so the user's
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
    # The backends run an unquoted `cd <path> && claude ...`.
    if shlex.quote(str(p)) != str(p):
        raise WorkerConfigError(
            f"web-reader requires a directory path that needs no shell quoting (got {p!r})"
        )
    msg = f"web-reader requires an empty dedicated directory (got {p})"
    if not p.is_dir():
        raise WorkerConfigError(msg)
    if (p / ".git").exists() or (p / ".git").is_symlink():
        raise WorkerConfigError(msg)
    if any(e.name not in _WEB_READER_TOLERATED for e in p.iterdir()):
        raise WorkerConfigError(msg)


# Everything else in a web-reader's config (skip_permissions, trust_project_mcp,
# use_worktree, extra_args, env, plugin_dir, and any field added later) must be
# empty or false. model, effort and worker_settings get their own checks below.
_WEB_READER_ALLOWED_FIELDS = {
    "project_path",
    "profile",
    "agent_type",
    "name",
    "badge",
    "issue_id",
    "prompt",
    "output_file",
    "model",
    "effort",
    "worker_settings",
    "worktree",
}


def _reject_web_reader_overrides(config: dict) -> None:
    """Refuse every explicit field that could weaken the web-reader profile.

    These are allowlists on purpose. Too many CLI flags (``--settings``,
    ``--add-dir``, ``--agents``, a later ``--tools``), settings keys (``hooks``,
    ``apiKeyHelper``, ``permissions.allow``) and env vars (``NODE_OPTIONS``,
    ``CLAUDE_CONFIG_DIR``, ``ANTHROPIC_BASE_URL``) can undo the restrictions for
    a denylist to stay complete as Claude Code grows new ones. ``model`` and
    ``effort`` have their own fields, so nothing legitimate needs ``extra_args``.
    """
    for field, value in config.items():
        if value and field not in _WEB_READER_ALLOWED_FIELDS:
            raise WorkerConfigError(
                f"profile='web-reader' does not allow {field}: "
                "it could override the profile's restrictions"
            )
    # Not a truthiness check: spawn_workers treats an empty dict as "use a worktree".
    if config.get("worktree") not in (None, False):
        raise WorkerConfigError(
            "profile='web-reader' does not allow worktree: "
            "it runs in an empty dedicated directory, with no worktree"
        )

    # model and effort reach the shell line unquoted, so a stray space or
    # semicolon would add a flag or a command.
    model = config.get("model")
    if model and not (isinstance(model, str) and _SAFE_MODEL_RE.fullmatch(model)):
        raise WorkerConfigError(
            "profile='web-reader' requires a model name made of letters, digits "
            "and . _ : / - only"
        )
    effort = config.get("effort")
    if effort and not (isinstance(effort, str) and effort in EFFORT_LEVELS):
        raise WorkerConfigError(
            f"profile='web-reader' requires effort to be one of {sorted(EFFORT_LEVELS)}"
        )

    settings = config.get("worker_settings") or {}
    permissions = settings.get("permissions", {})
    if (
        set(settings) - {"permissions"}
        or not isinstance(permissions, dict)
        or set(permissions) - {"deny"}
        or not isinstance(permissions.get("deny", []), list)
        or not all(isinstance(rule, str) for rule in permissions.get("deny", []))
    ):
        raise WorkerConfigError(
            "profile='web-reader' only allows worker_settings of the form "
            '{"permissions": {"deny": [...]}}: anything else could override the '
            "profile's restrictions"
        )


def expand_profile(config: dict) -> dict:
    """Return ``config`` with its ``profile`` (if any) expanded.

    A profile is a floor, not a set of defaults: explicit fields alongside
    ``web-reader`` may only tighten it, and anything else is rejected rather
    than dropped (see ``_reject_web_reader_overrides``). The ``profile`` key is
    kept so callers can see it.

    Raises WorkerConfigError for an unknown profile, for a field that would
    weaken web-reader, and for malformed fields.
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
    if profile_name == PROFILE_WEB_READER:
        _reject_web_reader_overrides(config)

    preset = copy.deepcopy(PROFILES[profile_name])
    expanded = dict(config)
    for key in ("skip_permissions", "trust_project_mcp", "use_worktree"):
        expanded[key] = preset[key]
    expanded["extra_args"] = preset["extra_args"]
    expanded["env"] = preset["env"]
    expanded["worker_settings"] = deep_merge_settings(
        preset["worker_settings"], config.get("worker_settings") or {}
    )
    return expanded
