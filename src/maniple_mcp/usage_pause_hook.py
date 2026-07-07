"""Generator for the self-contained usage-pause PreToolUse hook script.

Sibling to context_pause_hook.py: same graceful-wrap-up semantics (allowlist
+ PreToolUse deny JSON + fail-open), but for the ACCOUNT's rolling 5-hour
usage window (the Claude plan's session credit quota) instead of context.

The hook script is written to disk by `build_stop_hook_settings_file()` in
iterm_utils.py and invoked by Claude Code as `python3 <path> <threshold>
<state_file> <max_stale_seconds>` for every tool call (no matcher). It must
remain stdlib-only and standalone -- it never imports maniple_mcp, since it
runs inside a worker's project directory/venv, which may not have this
package installed.

Behavior:
- Allowlisted tools (Write, Read, TodoWrite) always pass -- this is the
  "graceful wrap-up" escape hatch so a worker can save a handoff file.
- Otherwise, read `rate_limits.five_hour.used_percentage` from `state_file`
  (a JSON cache of Claude Code's statusline stdin, which carries
  `rate_limits` -- hooks don't receive rate_limits natively). `seven_day` is
  ignored entirely.
- Missing/unreadable state_file, an mtime older than `max_stale_seconds`,
  malformed JSON, or a missing/non-numeric used_percentage -- ALL fail open
  (allow). Stale or absent data must never pause a worker.
- `rate_limits` is only present in the statusline payload for Pro/Max OAuth
  logins; under API-key auth it's simply absent, so this also fails open.
- If used_percentage >= threshold * 100, deny the tool call via PreToolUse
  JSON output on stdout (same shape as context-pause), naming the 5-hour
  session window and (best-effort) the local reset time from `resets_at`.
- Fail open (exit 0, no output) on ANY error -- a broken hook must never
  block a worker.
"""

from __future__ import annotations

HOOK_SCRIPT_FILENAME = "usage_pause_hook.py"

# Tools that remain allowed even once a worker is over the usage-pause
# threshold, so it can still write a brief handoff before ending its turn.
ALLOWLISTED_TOOLS = ("Write", "Read", "TodoWrite")


_HOOK_SCRIPT_SOURCE = '''#!/usr/bin/env python3
"""Maniple usage-pause PreToolUse hook (auto-generated, stdlib only).

Blocks a worker's tool calls once the ACCOUNT's rolling 5-hour usage window
(the Claude plan's session credit quota, NOT context) crosses a configured
threshold, except for a small allowlist of tools that let it write a
handoff file and end its turn gracefully. Fails open (allows the call) on
any error -- this hook must never break a worker, and stale/missing data
must never pause one.

Invoked as: python3 usage_pause_hook.py <threshold> <state_file> <max_stale_seconds>
Reads the Claude Code PreToolUse hook JSON payload from stdin.

`state_file` is a cache of Claude Code's statusline stdin JSON (which the
user's statusline command must write on every update -- hooks don't receive
rate_limits natively). `rate_limits` is only present there for Pro/Max OAuth
logins; under API-key auth it's simply absent, so this hook silently no-ops
(fails open) in that case too.
"""
import json
import os
import sys
import time

ALLOWLISTED_TOOLS = {"Write", "Read", "TodoWrite"}


def main(argv):
    try:
        threshold = float(argv[0])
        state_file = argv[1]
        max_stale_seconds = float(argv[2])
    except (IndexError, ValueError):
        return 0

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0

    if not isinstance(payload, dict):
        return 0

    if payload.get("tool_name") in ALLOWLISTED_TOOLS:
        return 0

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

    threshold_percent = threshold * 100
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

    reason = (
        f"Account usage is at {round(used)}% of your Claude plan's 5-hour "
        f"session window (>= {round(threshold_percent)}% threshold)."
        f"{reset_note} "
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


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Absolute last resort: never let an unexpected error block a worker.
        sys.exit(0)
'''


def render_hook_script() -> str:
    """Return the self-contained hook script source, ready to write to disk."""

    return _HOOK_SCRIPT_SOURCE
