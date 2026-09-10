"""Tests for the shared shell-readiness probe (src/maniple_mcp/shell_probe.py)."""

import pytest

from maniple_mcp.shell_probe import SHELL_READY_MARKER, probe_shell_ready, screen_tail


class FakeClock:
    """Deterministic monotonic clock we can advance manually."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


@pytest.mark.asyncio
async def test_marker_appears_after_first_send_returns_true_one_send():
    clock = FakeClock()
    sends = []
    screens = [SHELL_READY_MARKER]

    async def send_probe():
        sends.append(clock.now)

    async def read_screen():
        return screens[-1]

    async def sleep(seconds):
        clock.advance(seconds)

    result = await probe_shell_ready(
        send_probe,
        read_screen,
        timeout_seconds=10.0,
        poll_interval=0.1,
        resend_after=2.5,
        clock=clock,
        sleep=sleep,
    )

    assert result is True
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_swallowed_first_probe_then_second_succeeds():
    clock = FakeClock()
    sends = []
    # Screen state changes over time: starts stuck on the oh-my-zsh prompt
    # with the eaten keystroke, then shows the marker once the probe is
    # resent after the shell has actually consumed the [Y/n] prompt.
    state = {"screen": '[oh-my-zsh] Would you like to update? [Y/n] \ncho "MANIPLE_READY_7f3a9c"\nzsh: command not found: cho'}

    async def send_probe():
        sends.append(clock.now)
        if len(sends) >= 2:
            state["screen"] = SHELL_READY_MARKER

    async def read_screen():
        return state["screen"]

    async def sleep(seconds):
        clock.advance(seconds)

    result = await probe_shell_ready(
        send_probe,
        read_screen,
        timeout_seconds=10.0,
        poll_interval=0.1,
        resend_after=2.5,
        clock=clock,
        sleep=sleep,
    )

    assert result is True
    assert len(sends) == 2
    # Should have succeeded well before the full timeout elapsed.
    assert clock.now < 10.0


@pytest.mark.asyncio
async def test_marker_never_appears_returns_false_after_timeout():
    clock = FakeClock()
    sends = []

    async def send_probe():
        sends.append(clock.now)

    async def read_screen():
        return "nothing here"

    async def sleep(seconds):
        clock.advance(seconds)

    result = await probe_shell_ready(
        send_probe,
        read_screen,
        timeout_seconds=10.0,
        poll_interval=0.1,
        resend_after=2.5,
        clock=clock,
        sleep=sleep,
    )

    assert result is False
    # Should have resent multiple times over the 10s window (>= 2 sends).
    assert len(sends) >= 2


@pytest.mark.asyncio
async def test_line_containing_marker_but_not_equal_does_not_count():
    clock = FakeClock()

    async def send_probe():
        pass

    async def read_screen():
        # The echoed command line itself contains the marker string, but is
        # not equal to it after stripping - this must NOT count as success.
        return f'echo "{SHELL_READY_MARKER}"'

    async def sleep(seconds):
        clock.advance(seconds)
        if clock.now > 1.0:
            # Prevent an infinite loop in case of a logic bug; the timeout
            # check should have already ended the loop by 10s regardless.
            raise AssertionError("probe looped past a safe bound")

    result = await probe_shell_ready(
        send_probe,
        read_screen,
        timeout_seconds=1.0,
        poll_interval=0.2,
        resend_after=2.5,
        clock=clock,
        sleep=sleep,
    )

    assert result is False


@pytest.mark.asyncio
async def test_read_screen_exception_is_tolerated():
    clock = FakeClock()
    calls = {"n": 0}

    async def send_probe():
        pass

    async def read_screen():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("terminal not ready")
        return SHELL_READY_MARKER

    async def sleep(seconds):
        clock.advance(seconds)

    result = await probe_shell_ready(
        send_probe,
        read_screen,
        timeout_seconds=10.0,
        poll_interval=0.1,
        resend_after=2.5,
        clock=clock,
        sleep=sleep,
    )

    assert result is True


def test_screen_tail_drops_blanks_and_keeps_last_n():
    text = "line1\n\nline2\n   \nline3\nline4\nline5\n\n"
    result = screen_tail(text, n=3)
    assert result == "line3\nline4\nline5"


def test_screen_tail_handles_fewer_lines_than_n():
    text = "\nonly one\n\n"
    result = screen_tail(text, n=8)
    assert result == "only one"


def test_screen_tail_empty_text_returns_empty_string():
    assert screen_tail("", n=8) == ""
