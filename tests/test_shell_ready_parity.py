"""Parity tests: both terminal backends must resend the shell-readiness
probe when a startup prompt swallows the first attempt (e.g. oh-my-zsh's
"Would you like to update? [Y/n]" eating one keystroke), and both must
surface the actual last screen lines (not a generic "still initializing"
hint) when the shell genuinely never becomes ready.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maniple_mcp.shell_probe import SHELL_READY_MARKER

OMZ_PROMPT_TAIL = '[oh-my-zsh] Would you like to update? [Y/n] '


def _mock_cli():
    cli = MagicMock()
    cli.supports_settings_file.return_value = False
    cli.build_full_command.return_value = "claude --foo"
    cli.engine_id = "claude"
    cli.command.return_value = "claude"
    return cli


# ---------------------------------------------------------------------------
# tmux backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tmux_wait_for_shell_ready_recovers_from_swallowed_probe():
    from maniple_mcp.terminal_backends.tmux import TmuxBackend

    backend = TmuxBackend()
    handle = MagicMock()
    send_calls = []
    state = {"screen": OMZ_PROMPT_TAIL + '\ncho "MANIPLE_READY_7f3a9c"\nzsh: command not found: cho'}

    async def fake_send_prompt(h, text, submit=True):
        send_calls.append(text)
        if len(send_calls) >= 2:
            state["screen"] = SHELL_READY_MARKER

    async def fake_read_screen(h):
        return state["screen"]

    with patch.object(backend, "send_prompt", fake_send_prompt), patch.object(
        backend, "read_screen_text", fake_read_screen
    ):
        result = await backend._wait_for_shell_ready(
            handle, timeout_seconds=3.0, poll_interval=0.05
        )

    assert result is True
    assert len(send_calls) == 2
    for text in send_calls:
        assert f'echo "{SHELL_READY_MARKER}"' == text


@pytest.mark.asyncio
async def test_tmux_start_agent_in_session_error_shows_screen_tail_not_initializing_hint():
    from maniple_mcp.terminal_backends.tmux import TmuxBackend

    backend = TmuxBackend()
    handle = MagicMock()

    async def fake_read_screen(h):
        return "some other line\n" + OMZ_PROMPT_TAIL

    with patch.object(
        backend, "_wait_for_shell_ready", AsyncMock(return_value=False)
    ), patch.object(backend, "read_screen_text", fake_read_screen):
        with pytest.raises(RuntimeError) as excinfo:
            await backend.start_agent_in_session(
                handle=handle,
                cli=_mock_cli(),
                project_path="/tmp/proj",
                shell_ready_timeout=0.01,
            )

    message = str(excinfo.value)
    assert OMZ_PROMPT_TAIL.strip() in message
    assert "still be initializing" not in message


@pytest.mark.asyncio
async def test_tmux_start_agent_in_session_error_handles_empty_screen():
    from maniple_mcp.terminal_backends.tmux import TmuxBackend

    backend = TmuxBackend()
    handle = MagicMock()

    async def fake_read_screen(h):
        raise RuntimeError("tmux gone")

    with patch.object(
        backend, "_wait_for_shell_ready", AsyncMock(return_value=False)
    ), patch.object(backend, "read_screen_text", fake_read_screen):
        with pytest.raises(RuntimeError) as excinfo:
            await backend.start_agent_in_session(
                handle=handle,
                cli=_mock_cli(),
                project_path="/tmp/proj",
                shell_ready_timeout=0.01,
            )

    assert "(screen empty)" in str(excinfo.value)


# ---------------------------------------------------------------------------
# iTerm backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterm_wait_for_shell_ready_recovers_from_swallowed_probe():
    from maniple_mcp import iterm_utils

    session = MagicMock()
    send_calls = []
    state = {"screen": OMZ_PROMPT_TAIL + '\ncho "MANIPLE_READY_7f3a9c"\nzsh: command not found: cho'}

    async def fake_send_prompt(s, text):
        send_calls.append(text)
        if len(send_calls) >= 2:
            state["screen"] = SHELL_READY_MARKER

    async def fake_read_screen(s):
        return state["screen"]

    with patch("maniple_mcp.iterm_utils.send_prompt", fake_send_prompt), patch(
        "maniple_mcp.iterm_utils.read_screen_text", fake_read_screen
    ):
        result = await iterm_utils.wait_for_shell_ready(
            session, timeout_seconds=3.0, poll_interval=0.05
        )

    assert result is True
    assert len(send_calls) == 2
    for text in send_calls:
        assert f'echo "{SHELL_READY_MARKER}"' == text


@pytest.mark.asyncio
async def test_iterm_start_agent_in_session_error_shows_screen_tail_not_initializing_hint():
    from maniple_mcp import iterm_utils

    session = MagicMock()

    async def fake_read_screen(s):
        return "some other line\n" + OMZ_PROMPT_TAIL

    with patch(
        "maniple_mcp.iterm_utils.wait_for_shell_ready", AsyncMock(return_value=False)
    ), patch("maniple_mcp.iterm_utils.read_screen_text", fake_read_screen):
        with pytest.raises(RuntimeError) as excinfo:
            await iterm_utils.start_agent_in_session(
                session=session,
                cli=_mock_cli(),
                project_path="/tmp/proj",
                shell_ready_timeout=0.01,
            )

    message = str(excinfo.value)
    assert OMZ_PROMPT_TAIL.strip() in message
    assert "still be initializing" not in message


@pytest.mark.asyncio
async def test_iterm_start_agent_in_session_error_handles_empty_screen():
    from maniple_mcp import iterm_utils

    session = MagicMock()

    async def fake_read_screen(s):
        raise RuntimeError("iterm gone")

    with patch(
        "maniple_mcp.iterm_utils.wait_for_shell_ready", AsyncMock(return_value=False)
    ), patch("maniple_mcp.iterm_utils.read_screen_text", fake_read_screen):
        with pytest.raises(RuntimeError) as excinfo:
            await iterm_utils.start_agent_in_session(
                session=session,
                cli=_mock_cli(),
                project_path="/tmp/proj",
                shell_ready_timeout=0.01,
            )

    assert "(screen empty)" in str(excinfo.value)
