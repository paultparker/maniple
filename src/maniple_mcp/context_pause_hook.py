"""Generator for the self-contained context-pause PreToolUse hook script.

The hook script is written to disk by `build_stop_hook_settings_file()` in
iterm_utils.py and invoked by Claude Code as `python3 <path> <threshold>
<window_tokens>` for every tool call (no matcher). It must remain stdlib-only
and standalone -- it never imports maniple_mcp, since it runs inside a
worker's project directory/venv, which may not have this package installed.

Behavior (see PLAN in the maniple context-pause feature spec):
- Allowlisted tools (Write, Read, TodoWrite) always pass -- this is the
  "graceful wrap-up" escape hatch so a worker can save a handoff file.
- Otherwise, scan the transcript JSONL for the LAST main-chain (non
  sidechain) assistant message with a `message.usage` object and compute
  used = input_tokens + cache_read_input_tokens + cache_creation_input_tokens.
- If used / window_tokens >= threshold, deny the tool call via PreToolUse
  JSON output on stdout, with a reason instructing the worker to write a
  handoff and end its turn.
- Fail open (exit 0, no output) on ANY error -- a broken hook must never
  block a worker.
"""

from __future__ import annotations

HOOK_SCRIPT_FILENAME = "context_pause_hook.py"

# Tools that remain allowed even once a worker is over the context-pause
# threshold, so it can still write a brief handoff before ending its turn.
ALLOWLISTED_TOOLS = ("Write", "Read", "TodoWrite")


_HOOK_SCRIPT_SOURCE = '''#!/usr/bin/env python3
"""Maniple context-pause PreToolUse hook (auto-generated, stdlib only).

Blocks a worker's tool calls once its context-window usage crosses a
configured threshold, except for a small allowlist of tools that let it
write a handoff file and end its turn gracefully. Fails open (allows the
call) on any error -- this hook must never break a worker.

Invoked as: python3 context_pause_hook.py <threshold> <window_tokens>
Reads the Claude Code PreToolUse hook JSON payload from stdin.
"""
import json
import sys

ALLOWLISTED_TOOLS = {"Write", "Read", "TodoWrite"}


def _last_main_chain_usage(transcript_path):
    """Return usage tokens (int) from the last non-sidechain assistant
    message with a usage object in the transcript, or None if not found."""
    used = None
    with open(transcript_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("isSidechain"):
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens = usage.get("input_tokens") or 0
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_creation = usage.get("cache_creation_input_tokens") or 0
            used = input_tokens + cache_read + cache_creation
    return used


def main(argv):
    try:
        threshold = float(argv[0])
        window_tokens = int(argv[1])
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

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return 0

    try:
        used = _last_main_chain_usage(transcript_path)
    except (OSError, ValueError):
        return 0

    if used is None or window_tokens <= 0:
        return 0

    fraction = used / window_tokens
    if fraction < threshold:
        return 0

    percent = round(fraction * 100)
    threshold_percent = round(threshold * 100)
    reason = (
        f"Context usage is at {percent}% (>= {threshold_percent}% threshold). "
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
