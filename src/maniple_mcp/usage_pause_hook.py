"""Generator for the self-contained usage-pause PreToolUse hook script.

Sibling to context_pause_hook.py: same graceful-wrap-up semantics (allowlist
+ PreToolUse deny JSON + fail-open), but for the ACCOUNT's rolling 5-hour
usage window (the Claude plan's session credit quota) instead of context.

The hook script is written to disk by `build_stop_hook_settings_file()` in
iterm_utils.py and invoked by Claude Code as `python3 <path> <threshold>
<state_file> <max_stale_seconds> <scope> <override_dir>` for every tool call
(no matcher). It must remain stdlib-only and standalone -- it never imports
maniple_mcp, since it runs inside a worker's project directory/venv, which
may not have this package installed.

Behavior:
- If scope == "global" and env MANIPLE_WORKER is set, exit 0 immediately --
  a globally-installed hook (see install-global-usage-guard) must no-op
  inside a maniple worker, which already carries its own scoped hook.
- Anti-loophole (checked BEFORE the allowlist): Write/Edit/MultiEdit/
  NotebookEdit targeting a path that resolves inside `override_dir` is
  ALWAYS denied, regardless of usage level -- a session must not be able to
  grant itself an override. Fails open (skips this check) if the relevant
  path field is absent/unparseable.
- Allowlisted tools (Write, Read, TodoWrite) always pass otherwise -- the
  "graceful wrap-up" escape hatch so a worker can save a handoff file.
- Escalating override ladder: reads `<override_dir>/<scope>.json`
  ({"threshold": 0.90|0.95|null, "expires_at": <epoch>}). If valid and not
  expired: threshold null means unlimited (allow, no further check);
  otherwise effective_threshold = max(base_threshold, override.threshold).
  Missing/expired/malformed override -> effective_threshold = base_threshold.
- Otherwise, read `rate_limits.five_hour.used_percentage` from `state_file`
  (a JSON cache of Claude Code's statusline stdin, which carries
  `rate_limits` -- hooks don't receive rate_limits natively). `seven_day` is
  ignored entirely.
- Missing/unreadable state_file, an mtime older than `max_stale_seconds`,
  malformed JSON, or a missing/non-numeric used_percentage -- ALL fail open
  (allow). Stale or absent data must never pause a worker.
- `rate_limits` is only present in the statusline payload for Pro/Max OAuth
  logins; under API-key auth it's simply absent, so this also fails open.
- If used_percentage >= effective_threshold * 100, deny the tool call via
  PreToolUse JSON output on stdout (same shape as context-pause), naming the
  5-hour session window, (best-effort) the local reset time from
  `resets_at`, and how to continue (override_usage_pause tool for worker
  scope -- coordinator may only call it with the user's explicit approval;
  `maniple usage-override` CLI for global scope).
- Fail open (exit 0, no output) on ANY error -- a broken hook must never
  block a worker.
"""

from __future__ import annotations

from ._hook_script_shared import ALLOWLISTED_TOOLS, FAIL_OPEN_MAIN_BLOCK

HOOK_SCRIPT_FILENAME = "usage_pause_hook.py"

# Write-capable tools checked by the anti-loophole guard, and the tool_input
# field each one uses for its target path.
_ANTI_LOOPHOLE_PATH_FIELDS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


_HOOK_SCRIPT_BODY = '''#!/usr/bin/env python3
"""Maniple usage-pause PreToolUse hook (auto-generated, stdlib only).

Blocks a worker's tool calls once the ACCOUNT's rolling 5-hour usage window
(the Claude plan's session credit quota, NOT context) crosses an escalating
threshold ladder, except for a small allowlist of tools that let it write a
handoff file and end its turn gracefully. Fails open (allows the call) on
any error -- this hook must never break a worker, and stale/missing data
must never pause one.

Invoked as:
    python3 usage_pause_hook.py <base_threshold> <state_file> \\
        <max_stale_seconds> <scope> <override_dir>
Reads the Claude Code PreToolUse hook JSON payload from stdin.

`state_file` is a cache of Claude Code's statusline stdin JSON (which the
user's statusline command must write on every update -- hooks don't receive
rate_limits natively). `rate_limits` is only present there for Pro/Max OAuth
logins; under API-key auth it's simply absent, so this hook silently no-ops
(fails open) in that case too.

`scope` identifies which override file to consult: a worker's marker_id, or
"global" for the globally-installed hook (see install-global-usage-guard).
Overrides live at <override_dir>/<scope>.json and form an escalating ladder
(granted via the override_usage_pause MCP tool or `maniple usage-override`
CLI): base -> 0.90 -> 0.95 -> unlimited (threshold=null), expiring with the
account's 5-hour window.
"""
import json
import os
import sys
import time

ALLOWLISTED_TOOLS = {"Write", "Read", "TodoWrite"}

ANTI_LOOPHOLE_PATH_FIELDS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def _targets_override_dir(tool_name, tool_input, override_dir):
    """True if tool_name is a write-capable tool whose target path resolves
    inside override_dir. Fails open (returns False) on any missing/
    unparseable path -- this only ever ADDS a deny, never removes one."""
    field = ANTI_LOOPHOLE_PATH_FIELDS.get(tool_name)
    if not field or not isinstance(tool_input, dict):
        return False
    path = tool_input.get(field)
    if not isinstance(path, str) or not path:
        return False
    try:
        target_real = os.path.realpath(path)
        override_real = os.path.realpath(override_dir)
    except Exception:
        return False
    return target_real == override_real or target_real.startswith(
        override_real + os.sep
    )


def _read_override(override_dir, scope):
    """Return {"threshold": ..., "expires_at": ...} for a valid, unexpired
    override, or None if missing/malformed/expired (i.e. "no override")."""
    try:
        with open(os.path.join(override_dir, scope + ".json"), "r") as f:
            data = json.load(f)
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
        return None  # Expired -- treat as no override.

    threshold = data["threshold"]
    if threshold is not None:
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return None

    return {"threshold": threshold, "expires_at": expires_at}


def main(argv):
    try:
        base_threshold = float(argv[0])
        state_file = argv[1]
        max_stale_seconds = float(argv[2])
        scope = argv[3]
        override_dir = argv[4]
    except (IndexError, ValueError):
        return 0

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")

    # A globally-installed hook must no-op inside maniple workers, which
    # already carry their own scoped hook -- worker scope is unaffected.
    if scope == "global" and os.environ.get("MANIPLE_WORKER"):
        return 0

    # Anti-loophole: checked BEFORE the allowlist, since Write is normally
    # allowlisted -- a session must not be able to grant itself an override
    # by writing/editing directly into override_dir.
    if _targets_override_dir(tool_name, tool_input, override_dir):
        reason = (
            "Denied: sessions cannot grant themselves a usage-pause "
            "override by writing into the override directory. Ask the "
            "coordinator to use override_usage_pause, which it may only do "
            "with the user's explicit approval (or run "
            "`maniple usage-override` for the global hook)."
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
        return 0

    if tool_name in ALLOWLISTED_TOOLS:
        return 0

    # Escalating override ladder: null threshold means unlimited (allow
    # immediately, no need to even look at usage). Otherwise the effective
    # threshold is the max of the configured base and the override rung.
    effective_threshold = base_threshold
    override = _read_override(override_dir, scope)
    if override is not None:
        if override["threshold"] is None:
            return 0
        effective_threshold = max(base_threshold, override["threshold"])

    try:
        mtime = os.stat(state_file).st_mtime
    except OSError:
        return 0

    if (time.time() - mtime) > max_stale_seconds:
        return 0  # Stale cache -- never pause a worker on old data.

    try:
        with open(state_file, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0

    if not isinstance(data, dict):
        return 0

    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return 0

    five_hour = rate_limits.get("five_hour")
    if not isinstance(five_hour, dict):
        return 0

    used = five_hour.get("used_percentage")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return 0

    threshold_percent = effective_threshold * 100
    if used < threshold_percent:
        return 0

    reset_note = ""
    resets_at = five_hour.get("resets_at")
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        try:
            import datetime
            reset_time = datetime.datetime.fromtimestamp(resets_at).strftime("%H:%M")
            reset_note = f" Resets at {reset_time} local time."
        except Exception:
            # Best-effort only -- garbage/out-of-range resets_at must not
            # prevent the pause itself.
            reset_note = ""

    if scope == "global":
        continue_hint = " To continue, run: maniple usage-override"
    else:
        continue_hint = (
            " The coordinator can grant a continue with the "
            "override_usage_pause tool, but only with the user's explicit "
            "approval -- ask them first."
        )

    reason = (
        f"Account usage is at {round(used)}% of your Claude plan's 5-hour "
        f"session window (>= {round(threshold_percent)}% threshold)."
        f"{reset_note}{continue_hint} "
        "Pause all work now: use Write to save a brief handoff file (current "
        "state, what's done, next steps) then END YOUR TURN and await the "
        "manager."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


'''

_HOOK_SCRIPT_SOURCE = _HOOK_SCRIPT_BODY + FAIL_OPEN_MAIN_BLOCK


def render_hook_script() -> str:
    """Return the self-contained hook script source, ready to write to disk."""

    return _HOOK_SCRIPT_SOURCE
