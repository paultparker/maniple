"""Shared generation-time fragments for the context-pause and usage-pause
PreToolUse hook script generators (context_pause_hook.py, usage_pause_hook.py).

This module is a generation-time-only helper -- it is never imported by an
emitted hook script. Each generator still renders a fully self-contained,
stdlib-only script (no maniple_mcp imports at runtime), because the fragments
below are spliced into each script's own source text rather than referenced
by import.
"""

from __future__ import annotations

# Tools that remain allowed even once a worker is over its pause threshold,
# so it can still write a brief handoff before ending its turn. Shared by
# both generators; also duplicated verbatim *inside* each emitted script's
# own ALLOWLISTED_TOOLS set, since the running script can't import this
# module.
ALLOWLISTED_TOOLS = ("Write", "Read", "TodoWrite")

# Both hook scripts fail open the same way: run main(), and if IT somehow
# raises, still exit 0 rather than ever blocking a worker on an unhandled
# error. The two generators emit this block byte-for-byte, so it's spliced
# onto the end of each script body instead of being retyped twice.
FAIL_OPEN_MAIN_BLOCK = '''if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Absolute last resort: never let an unexpected error block a worker.
        sys.exit(0)
'''
