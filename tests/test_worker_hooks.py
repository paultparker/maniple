"""The worker settings file injects AskUserQuestion Pre/PostToolUse hooks."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maniple_mcp import config as config_module
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


def _matcherless_pretooluse_entries(settings: dict) -> list[dict]:
    """Return PreToolUse hook entries with no `matcher` key (fire for all tools)."""
    return [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if "matcher" not in entry
    ]


class TestContextPauseHookInjection:
    """The context-pause PreToolUse hook (no matcher) is injected/omitted
    based on config.context_pause, and existing hooks stay untouched."""

    def test_context_pause_hook_present_by_default(self):
        """With no config file, context_pause defaults to enabled -- the
        hook is injected using the default threshold/window_tokens."""
        path = build_stop_hook_settings_file("cp-default")
        settings = json.loads(Path(path).read_text())

        matches = _matcherless_pretooluse_entries(settings)
        assert len(matches) == 1
        command = matches[0]["hooks"][0]["command"]
        assert "context_pause_hook.py" in command
        assert "0.75" in command
        assert "200000" in command

    def test_context_pause_hook_writes_standalone_script(self):
        """The hook script file itself is written to the settings dir and
        matches the generated stdlib-only source."""
        from maniple_mcp.context_pause_hook import (
            HOOK_SCRIPT_FILENAME,
            render_hook_script,
        )

        path = build_stop_hook_settings_file("cp-script")
        script_path = Path(path).parent / HOOK_SCRIPT_FILENAME
        assert script_path.exists()
        assert script_path.read_text() == render_hook_script()

    def test_context_pause_hook_absent_when_disabled(self, tmp_path):
        config_module.CONFIG_PATH.write_text(
            json.dumps({"context_pause": {"enabled": False}})
        )

        path = build_stop_hook_settings_file("cp-disabled")
        settings = json.loads(Path(path).read_text())

        assert _matcherless_pretooluse_entries(settings) == []
        # Existing hooks are unaffected by disabling context_pause.
        assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == (
            "echo [worker-done:cp-disabled]"
        )
        pre = settings["hooks"]["PreToolUse"]
        assert len(pre) == 1
        assert pre[0]["matcher"] == "AskUserQuestion"

    def test_context_pause_hook_uses_configured_threshold_and_window(self, tmp_path):
        config_module.CONFIG_PATH.write_text(
            json.dumps({
                "context_pause": {
                    "enabled": True,
                    "threshold": 0.6,
                    "window_tokens": 50000,
                }
            })
        )

        path = build_stop_hook_settings_file("cp-custom")
        settings = json.loads(Path(path).read_text())

        matches = _matcherless_pretooluse_entries(settings)
        assert len(matches) == 1
        command = matches[0]["hooks"][0]["command"]
        assert "0.6" in command
        assert "50000" in command

    def test_existing_hooks_unchanged_with_context_pause_enabled(self):
        """Stop and AskUserQuestion Pre/PostToolUse hooks are untouched when
        the context-pause hook is also injected."""
        marker = "cp-parity"
        path = build_stop_hook_settings_file(marker)
        settings = json.loads(Path(path).read_text())
        hooks = settings["hooks"]

        assert hooks["Stop"][0]["hooks"][0]["command"] == f"echo [worker-done:{marker}]"

        pre_askuserquestion = [e for e in hooks["PreToolUse"] if e.get("matcher") == "AskUserQuestion"]
        assert len(pre_askuserquestion) == 1
        pre_cmd = pre_askuserquestion[0]["hooks"][0]["command"]
        assert f"{marker}.json" in pre_cmd
        assert "pending" in pre_cmd

        post = hooks["PostToolUse"][0]
        assert post["matcher"] == "AskUserQuestion"
        post_cmd = post["hooks"][0]["command"]
        assert f"{marker}.json" in post_cmd


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
