"""
Shared shell-readiness probe used by both terminal backends (tmux + iTerm).

Types an echo command carrying a unique marker into a freshly-opened shell
and polls the screen for that marker to appear on its own line, proving the
shell is alive and accepting input.

Motivation: a startup prompt that consumes exactly one keystroke (e.g.
oh-my-zsh's periodic "Would you like to update? [Y/n]" prompt, which does a
single-character `read -r -k 1`) can eat the first character of the probe
command. If the probe is only ever sent once, the marker never appears and
the caller times out with a misleading "shell not ready" error - when in
fact the fix is simply to send the probe again once the blocking prompt has
been answered. `probe_shell_ready` resends periodically so a single swallowed
keystroke doesn't cause a spurious spawn failure.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

SHELL_READY_MARKER = "MANIPLE_READY_7f3a9c"


async def probe_shell_ready(
    send_probe: Callable[[], Awaitable[None]],
    read_screen: Callable[[], Awaitable[str]],
    *,
    timeout_seconds: float,
    poll_interval: float = 0.1,
    resend_after: float = 2.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Send a readiness probe and poll for the marker, resending on stall.

    Args:
        send_probe: async zero-arg callable that types the marker echo
            command plus Enter into the target shell.
        read_screen: async zero-arg callable returning the current screen
            text. Exceptions are swallowed (treated as "no marker yet").
        timeout_seconds: total time budget before giving up.
        poll_interval: delay between screen reads.
        resend_after: if this many seconds elapse since the last send with
            no marker seen, resend the probe (handles a swallowed keystroke
            from a startup prompt like oh-my-zsh's update check).
        clock: injectable monotonic clock (for deterministic tests).
        sleep: injectable async sleep (for deterministic tests).

    Returns:
        True as soon as a screen line, stripped, equals SHELL_READY_MARKER;
        False if timeout_seconds elapses with no match.
    """
    start_time = clock()
    last_send_time = start_time
    await send_probe()

    while (clock() - start_time) < timeout_seconds:
        try:
            content = await read_screen()
        except Exception:
            content = ""

        for line in content.splitlines():
            if line.strip() == SHELL_READY_MARKER:
                return True

        now = clock()
        if (now - last_send_time) >= resend_after:
            await send_probe()
            last_send_time = now

        await sleep(poll_interval)

    return False


def screen_tail(text: str, n: int = 8) -> str:
    """Return the last `n` non-blank lines of `text`, joined by newlines."""
    non_blank = [line for line in text.splitlines() if line.strip()]
    return "\n".join(non_blank[-n:])
