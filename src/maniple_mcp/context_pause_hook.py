"""Generator for the self-contained context-pause PreToolUse hook script.

The hook script is written to disk by `build_stop_hook_settings_file()` in
iterm_utils.py and invoked by Claude Code as `python3 <path> <threshold>
<window_tokens> <max_tokens>` for every tool call (no matcher). It must
remain stdlib-only and standalone -- it never imports maniple_mcp, since it
runs inside a worker's project directory/venv, which may not have this
package installed.

Behavior (see PLAN in the maniple context-pause feature spec):
- Allowlisted tools (Write, Read, TodoWrite) always pass -- this is the
  "graceful wrap-up" escape hatch so a worker can save a handoff file.
- Otherwise, scan the transcript JSONL for the LAST main-chain (non
  sidechain) assistant message with a `message.usage` object and compute
  used = input_tokens + cache_read_input_tokens + cache_creation_input_tokens.
- The effective window is model-aware: if that same message's `model` id
  contains "haiku" (case-insensitive), the window is capped at 200K tokens
  (Haiku 4.5's real context window as of the 2026-07 model catalog);
  otherwise the configured window_tokens is used as-is (1M by default,
  matching current Opus/Sonnet/Fable models). See _effective_window().
- The effective token limit is min(threshold * effective_window, max_tokens)
  -- a flat threshold fraction is too generous on today's 1M-token windows
  (75% would be 750K tokens), so max_tokens caps the absolute token count.
  See _effective_limit().
- If used >= effective_limit, deny the tool call via PreToolUse JSON output
  on stdout, with a reason instructing the worker to write a handoff and end
  its turn.
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

Invoked as: python3 context_pause_hook.py <threshold> <window_tokens> <max_tokens>
Reads the Claude Code PreToolUse hook JSON payload from stdin.
"""
import json
import os
import sys

ALLOWLISTED_TOOLS = {"Write", "Read", "TodoWrite"}

# Only the tail of the transcript is scanned first, for efficiency -- these
# files can reach tens of MB, and a full parse on every single tool call
# would make cumulative cost quadratic over a session. Usage-bearing
# assistant lines are frequent, so ~1MB is generous; if no usage entry turns
# up in the tail, _last_main_chain_usage() falls back to a full scan so
# correctness is preserved for short/sparse transcripts.
_TAIL_READ_BYTES = 1_000_000


def _scan_lines_for_usage(lines):
    """Return (used, model) from the last non-sidechain assistant message
    with a usage object among `lines`, or (None, None) if none is found.

    `model` is the raw `message.model` string (or None if absent) from that
    same entry -- used by the caller to special-case Haiku's smaller window.
    """
    used = None
    model = None
    for line in lines:
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
        model = message.get("model")
    return used, model


def _last_main_chain_usage(transcript_path):
    """Return (used, model) from the last non-sidechain assistant message
    with a usage object in the transcript, or (None, None) if not found.

    Mirrors the tail-read pattern used elsewhere in maniple for large JSONL
    session files: seek to the last _TAIL_READ_BYTES of the file, discard
    the (possibly partial) first line, and scan only that tail.
    """
    file_size = os.path.getsize(transcript_path)
    read_size = min(file_size, _TAIL_READ_BYTES)

    with open(transcript_path, "rb") as f:
        truncated = file_size > read_size
        if truncated:
            f.seek(file_size - read_size)
            f.readline()  # Discard the partial first line from the seek.
        tail_bytes = f.read()

    tail_lines = tail_bytes.decode("utf-8", errors="replace").splitlines()
    used, model = _scan_lines_for_usage(tail_lines)
    if used is not None or not truncated:
        # Either found in the tail, or the tail read already covered the
        # whole file (nothing more to gain from a fallback scan).
        return used, model

    # Fallback: no usage entry in the tail (e.g. a short burst of tool-only
    # activity after the last usage-bearing assistant message pushed it out
    # of the tail window). Do a full scan so correctness never regresses.
    with open(transcript_path, "r") as f:
        return _scan_lines_for_usage(f)


# Effective context window for Haiku models, which is smaller than the
# config default (see _effective_window below).
_HAIKU_WINDOW_TOKENS = 200_000


def _effective_window(window_tokens, model):
    """Return the context window to use for this transcript.

    Model catalog as of 2026-07: Haiku 4.5 has a 200K-token context window;
    every other current model (Opus 4.8/4.7/4.6, Sonnet 5, Sonnet 4.6,
    Fable 5) defaults to 1M. Rather than maintain a full model->window map
    (which would need updating for every future release), this only
    special-cases Haiku by a case-insensitive substring match on the model
    id and otherwise trusts the configured window_tokens -- config is the
    override point for future models that don't fit this rule.
    """
    if model and "haiku" in model.lower():
        return min(window_tokens, _HAIKU_WINDOW_TOKENS)
    return window_tokens


def _effective_limit(threshold, window, max_tokens):
    """Return the effective token count that triggers a pause.

    A flat threshold fraction of the window is too generous on today's
    1M-token windows (75% would be 750K tokens, far past the point a
    worker can still usefully write a handoff), so the absolute token
    count is capped at max_tokens regardless of window size. On a smaller
    window (e.g. Haiku's real 200K), the threshold fraction alone stays
    below the cap and controls as before.
    """
    return min(threshold * window, max_tokens)


def main(argv):
    try:
        threshold = float(argv[0])
        window_tokens = int(argv[1])
        max_tokens = int(argv[2])
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
        used, model = _last_main_chain_usage(transcript_path)
    except (OSError, ValueError):
        return 0

    if used is None:
        return 0

    window = _effective_window(window_tokens, model)
    if window <= 0:
        return 0

    effective_limit = _effective_limit(threshold, window, max_tokens)
    if effective_limit <= 0 or used < effective_limit:
        return 0

    percent = round(used / window * 100)
    reason = (
        f"Context usage is at {percent}% of your context window "
        f"({used} tokens, limit {round(effective_limit)} tokens). "
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
