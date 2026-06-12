"""The worker settings file injects AskUserQuestion Pre/PostToolUse hooks."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maniple_mcp.iterm_utils import build_stop_hook_settings_file


def test_settings_file_has_stop_and_question_hooks():
    marker = "abc123"
    path = build_stop_hook_settings_file(marker)
    settings = json.loads(Path(path).read_text())
    hooks = settings["hooks"]

    # Stop hook unchanged
    assert hooks["Stop"][0]["hooks"][0]["command"] == f"echo [worker-done:{marker}]"

    # PreToolUse writes the marker for AskUserQuestion
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion"
    pre_cmd = pre["hooks"][0]["command"]
    assert f"{marker}.json" in pre_cmd
    assert "pending" in pre_cmd

    # PostToolUse deletes the marker for AskUserQuestion
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "AskUserQuestion"
    post_cmd = post["hooks"][0]["command"]
    assert f"{marker}.json" in post_cmd


def test_settings_file_auto_trusts_project_mcp_servers_by_default():
    """Workers auto-approve project .mcp.json servers so the trust prompt
    never blocks startup (which the agent-ready detector counts as a failure).
    Auto-trust is on by default."""
    path = build_stop_hook_settings_file("xyz789")
    settings = json.loads(Path(path).read_text())
    assert settings["enableAllProjectMcpServers"] is True


def test_settings_file_omits_auto_trust_when_disabled():
    """With trust_project_mcp=False the setting is absent, so Claude's normal
    'New MCP server found' trust prompt is preserved for untrusted checkouts."""
    path = build_stop_hook_settings_file("notrust1", trust_project_mcp=False)
    settings = json.loads(Path(path).read_text())
    assert "enableAllProjectMcpServers" not in settings
    # Hooks are still injected regardless of the trust setting.
    assert "Stop" in settings["hooks"]


def _mock_cli():
    cli = MagicMock()
    cli.supports_settings_file.return_value = True
    cli.build_full_command.return_value = "claude --foo"
    cli.engine_id = "claude"
    cli.command.return_value = "claude"
    return cli


# Backend parity: both terminal backends must forward trust_project_mcp into
# the shared settings builder (CLAUDE.md backend parity policy).


@pytest.mark.parametrize("trust", [True, False])
def test_tmux_backend_forwards_trust_project_mcp(trust):
    from maniple_mcp.terminal_backends.tmux import TmuxBackend

    backend = TmuxBackend()
    with patch(
        "maniple_mcp.iterm_utils.build_stop_hook_settings_file",
        return_value="/tmp/worker-m1.json",
    ) as build_mock, patch.object(
        backend, "_wait_for_shell_ready", AsyncMock(return_value=True)
    ), patch.object(backend, "send_prompt", AsyncMock()), patch.object(
        backend, "_wait_for_agent_ready", AsyncMock(return_value=True)
    ):
        asyncio.run(
            backend.start_agent_in_session(
                handle=MagicMock(),
                cli=_mock_cli(),
                project_path="/tmp/proj",
                stop_hook_marker_id="m1",
                trust_project_mcp=trust,
            )
        )
    build_mock.assert_called_once_with("m1", trust_project_mcp=trust)


@pytest.mark.parametrize("trust", [True, False])
def test_iterm_path_forwards_trust_project_mcp(trust):
    # The iTerm backend delegates to iterm_utils.start_agent_in_session, so
    # exercising that function covers the iTerm code path.
    from maniple_mcp import iterm_utils

    with patch(
        "maniple_mcp.iterm_utils.build_stop_hook_settings_file",
        return_value="/tmp/worker-m2.json",
    ) as build_mock, patch(
        "maniple_mcp.iterm_utils.wait_for_shell_ready", AsyncMock(return_value=True)
    ), patch(
        "maniple_mcp.iterm_utils.wait_for_agent_ready", AsyncMock(return_value=True)
    ), patch("maniple_mcp.iterm_utils.send_prompt", AsyncMock()):
        asyncio.run(
            iterm_utils.start_agent_in_session(
                session=MagicMock(),
                cli=_mock_cli(),
                project_path="/tmp/proj",
                stop_hook_marker_id="m2",
                trust_project_mcp=trust,
            )
        )
    build_mock.assert_called_once_with("m2", trust_project_mcp=trust)
