"""Tests for Claude Code's folder-trust dialog detection during agent startup."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from maniple_mcp import iterm_utils
from maniple_mcp.cli_backends import claude_cli, codex_cli
from maniple_mcp.cli_backends.base import UntrustedFolderError
from maniple_mcp.terminal_backends.base import TerminalSession
from maniple_mcp.terminal_backends.tmux import TmuxBackend

TRUST_DIALOG_SCREEN = """\
 Accessing workspace:
 /Users/paulparker/Dropbox/code/paseo-patches
 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""

READY_SCREEN = "Claude Code v2.1\n> \n  ? for shortcuts\n"


# --- tmux backend -----------------------------------------------------------


def _tmux(monkeypatch, screen):
    backend = TmuxBackend()

    async def fake_read(session):
        return screen

    async def noop(*args, **kwargs):
        return None

    async def shell_ok(*args, **kwargs):
        return True

    monkeypatch.setattr(backend, "read_screen_text", fake_read)
    monkeypatch.setattr(backend, "send_prompt", noop)
    monkeypatch.setattr(backend, "_wait_for_shell_ready", shell_ok)
    return backend, TerminalSession("tmux", "%1", "%1")


@pytest.mark.asyncio
async def test_tmux_wait_raises_fast_on_trust_dialog(monkeypatch):
    backend, session = _tmux(monkeypatch, TRUST_DIALOG_SCREEN)
    start = time.monotonic()
    with pytest.raises(UntrustedFolderError):
        await backend._wait_for_agent_ready(session, claude_cli, timeout_seconds=30.0)
    assert time.monotonic() - start < 2.0


@pytest.mark.asyncio
async def test_tmux_start_agent_untrusted_folder_message(monkeypatch):
    backend, session = _tmux(monkeypatch, TRUST_DIALOG_SCREEN)
    start = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        await backend.start_agent_in_session(
            session, claude_cli, "/tmp/never-trusted-dir", agent_ready_timeout=30.0
        )
    assert time.monotonic() - start < 2.0
    msg = str(excinfo.value)
    assert "/tmp/never-trusted-dir" in msg
    assert "trust" in msg
    assert "failed to start" not in msg


@pytest.mark.asyncio
async def test_tmux_wait_normal_screen_still_ready(monkeypatch):
    backend, session = _tmux(monkeypatch, READY_SCREEN)
    assert await backend._wait_for_agent_ready(
        session, claude_cli, timeout_seconds=5.0, poll_interval=0.01
    )


@pytest.mark.asyncio
async def test_tmux_wait_codex_unaffected_by_trust_text(monkeypatch):
    backend, session = _tmux(monkeypatch, TRUST_DIALOG_SCREEN)
    # Codex has no such dialog: normal timeout path (False), no exception.
    assert not await backend._wait_for_agent_ready(
        session, codex_cli, timeout_seconds=0.3, poll_interval=0.05
    )


# --- iTerm utils ------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterm_wait_raises_fast_on_trust_dialog():
    start = time.monotonic()
    with patch.object(
        iterm_utils, "read_screen_text", AsyncMock(return_value=TRUST_DIALOG_SCREEN)
    ):
        with pytest.raises(UntrustedFolderError):
            await iterm_utils.wait_for_agent_ready(
                object(), claude_cli, timeout_seconds=30.0
            )
    assert time.monotonic() - start < 2.0


@pytest.mark.asyncio
async def test_iterm_start_agent_untrusted_folder_message():
    with patch.object(
        iterm_utils, "read_screen_text", AsyncMock(return_value=TRUST_DIALOG_SCREEN)
    ), patch.object(iterm_utils, "send_prompt", AsyncMock()), patch.object(
        iterm_utils, "wait_for_shell_ready", AsyncMock(return_value=True)
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await iterm_utils.start_agent_in_session(
                session=object(),
                cli=claude_cli,
                project_path="/tmp/never-trusted-dir",
                agent_ready_timeout=30.0,
            )
    msg = str(excinfo.value)
    assert "/tmp/never-trusted-dir" in msg
    assert "trust" in msg
    assert "failed to start" not in msg


@pytest.mark.asyncio
async def test_iterm_wait_normal_screen_still_ready():
    with patch.object(
        iterm_utils, "read_screen_text", AsyncMock(return_value=READY_SCREEN)
    ):
        assert await iterm_utils.wait_for_agent_ready(
            object(), claude_cli, timeout_seconds=5.0, poll_interval=0.01
        )
