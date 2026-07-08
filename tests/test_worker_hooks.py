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


def _matcherless_commands(settings: dict) -> list[str]:
    return [e["hooks"][0]["command"] for e in _matcherless_pretooluse_entries(settings)]


class TestContextPauseHookInjection:
    """The context-pause PreToolUse hook (no matcher) is injected/omitted
    based on config.context_pause, and existing hooks stay untouched."""

    def test_context_pause_hook_present_by_default(self):
        """With no config file, context_pause defaults to enabled -- the
        hook is injected using the default threshold/window_tokens.
        (usage_pause is also enabled by default -- a sibling hook -- so we
        filter to the context_pause-specific command rather than assuming
        there's exactly one matcherless entry.)"""
        path = build_stop_hook_settings_file("cp-default")
        settings = json.loads(Path(path).read_text())

        matches = [c for c in _matcherless_commands(settings) if "context_pause_hook.py" in c]
        assert len(matches) == 1
        command = matches[0]
        assert "0.75" in command
        assert "1000000" in command
        assert "250000" in command

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
        # usage_pause is disabled too, so this test stays isolated to
        # context_pause's own on/off behavior (its independence from
        # usage_pause is covered separately by
        # TestUsagePauseHookInjection.test_context_and_usage_pause_are_independent).
        config_module.CONFIG_PATH.write_text(
            json.dumps({
                "context_pause": {"enabled": False},
                "usage_pause": {"enabled": False},
            })
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

        matches = [c for c in _matcherless_commands(settings) if "context_pause_hook.py" in c]
        assert len(matches) == 1
        command = matches[0]
        assert "0.6" in command
        assert "50000" in command

    def test_context_pause_hook_uses_configured_max_tokens(self, tmp_path):
        config_module.CONFIG_PATH.write_text(
            json.dumps({
                "context_pause": {
                    "enabled": True,
                    "threshold": 0.75,
                    "window_tokens": 1_000_000,
                    "max_tokens": 100000,
                }
            })
        )

        path = build_stop_hook_settings_file("cp-max-tokens")
        settings = json.loads(Path(path).read_text())

        matches = [c for c in _matcherless_commands(settings) if "context_pause_hook.py" in c]
        assert len(matches) == 1
        command = matches[0]
        assert "100000" in command

    def test_context_pause_hook_quotes_script_path(self):
        """The hook script path is shell-quoted (via shlex.quote) so paths
        containing spaces or shell metacharacters are handled safely."""
        import shlex

        path = build_stop_hook_settings_file("cp-quoted")
        settings = json.loads(Path(path).read_text())
        script_path = Path(path).parent / "context_pause_hook.py"

        matches = _matcherless_pretooluse_entries(settings)
        command = matches[0]["hooks"][0]["command"]
        assert shlex.quote(str(script_path)) in command

    def test_context_pause_hook_command_is_best_effort(self):
        """Command ends with `|| true`, matching the neighboring AskUserQuestion
        hooks' best-effort style -- a hook failure (e.g. missing python3) must
        never surface as a user-visible warning."""
        path = build_stop_hook_settings_file("cp-besteffort")
        settings = json.loads(Path(path).read_text())

        matches = _matcherless_pretooluse_entries(settings)
        command = matches[0]["hooks"][0]["command"]
        assert command.rstrip().endswith("|| true")

    def test_context_pause_hook_script_not_rewritten_when_unchanged(self):
        """The shared hook script is only ever regenerated with the same
        content -- skip the write (and its torn-read race window) when the
        file already exists with identical content, to avoid redundant I/O
        on every spawn."""
        import os

        from maniple_mcp.context_pause_hook import HOOK_SCRIPT_FILENAME

        # First call creates the script.
        path = build_stop_hook_settings_file("cp-nowrite-1")
        script_path = Path(path).parent / HOOK_SCRIPT_FILENAME
        assert script_path.exists()

        # Force an old mtime so a rewrite would be detectable.
        old_time = 1_000_000_000.0
        os.utime(script_path, (old_time, old_time))

        # Second call (content unchanged) must not rewrite the file.
        build_stop_hook_settings_file("cp-nowrite-2")
        assert script_path.stat().st_mtime == old_time

    def test_context_pause_hook_script_rewritten_when_stale(self):
        """If the on-disk script content differs (e.g. an older generator
        version, or corruption), it IS rewritten to match the current
        generated source."""
        from maniple_mcp.context_pause_hook import (
            HOOK_SCRIPT_FILENAME,
            render_hook_script,
        )

        path = build_stop_hook_settings_file("cp-stale-1")
        script_path = Path(path).parent / HOOK_SCRIPT_FILENAME
        script_path.write_text("# stale content from an older version\n")

        build_stop_hook_settings_file("cp-stale-2")
        assert script_path.read_text() == render_hook_script()

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


class TestUsagePauseHookInjection:
    """The usage-pause PreToolUse hook (no matcher) is injected/omitted
    based on config.usage_pause, independent of context_pause, and existing
    hooks stay untouched."""

    def test_usage_pause_hook_present_by_default(self):
        """With no config file, usage_pause defaults to enabled -- the hook
        is injected using the default threshold/state_file/max_stale_seconds."""
        path = build_stop_hook_settings_file("up-default")
        settings = json.loads(Path(path).read_text())

        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        assert len(matches) == 1
        command = matches[0]
        assert "0.75" in command
        assert "/tmp/cc-statusline-input.json" in command
        assert "600" in command

    def test_usage_pause_hook_command_includes_scope_and_override_dir(self):
        """The command carries the worker's marker_id as `scope` and the
        fixed override-ladder directory, so the hook can find/ignore this
        worker's override file."""
        marker = "up-scope-test"
        path = build_stop_hook_settings_file(marker)
        settings = json.loads(Path(path).read_text())

        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        command = matches[0]
        assert marker in command
        assert ".maniple/usage_override" in command

    def test_usage_pause_hook_quotes_state_file_and_script_path(self):
        """Both the script path and state_file are shell-quoted (via
        shlex.quote) so values containing spaces or shell metacharacters
        are handled safely. Simple paths like the defaults have no unsafe
        characters, so shlex.quote leaves them unquoted -- this test just
        confirms the same shlex.quote() output is what's embedded."""
        import shlex

        path = build_stop_hook_settings_file("up-quoted")
        settings = json.loads(Path(path).read_text())
        script_path = Path(path).parent / "usage_pause_hook.py"

        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        command = matches[0]
        assert shlex.quote(str(script_path)) in command
        assert shlex.quote("/tmp/cc-statusline-input.json") in command

    def test_usage_pause_hook_safely_quotes_state_file_with_quote_and_space(self):
        """A state_file value containing a double-quote and a space (e.g. a
        misconfigured or malicious value) must not break out of the
        generated shell command -- shlex.quote() handles this safely,
        unlike naive '"' + value + '"' wrapping."""
        import shlex

        dangerous_state_file = '/tmp/weird "quoted" file.json'
        config_module.CONFIG_PATH.write_text(
            json.dumps({"usage_pause": {"state_file": dangerous_state_file}})
        )

        path = build_stop_hook_settings_file("up-dangerous")
        settings = json.loads(Path(path).read_text())
        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        command = matches[0]

        assert shlex.quote(dangerous_state_file) in command
        # The naive '"value"' wrapping would break the quoting (the
        # embedded '"' ends the naive quoted string early) -- confirm the
        # command still parses into exactly the token we expect.
        tokens = shlex.split(command)
        assert dangerous_state_file in tokens

    def test_usage_pause_hook_command_is_best_effort(self):
        path = build_stop_hook_settings_file("up-besteffort")
        settings = json.loads(Path(path).read_text())
        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        assert matches[0].rstrip().endswith("|| true")

    def test_usage_pause_hook_writes_standalone_script(self):
        from maniple_mcp.usage_pause_hook import (
            HOOK_SCRIPT_FILENAME,
            render_hook_script,
        )

        path = build_stop_hook_settings_file("up-script")
        script_path = Path(path).parent / HOOK_SCRIPT_FILENAME
        assert script_path.exists()
        assert script_path.read_text() == render_hook_script()

    def test_usage_pause_hook_absent_when_disabled(self):
        config_module.CONFIG_PATH.write_text(
            json.dumps({"usage_pause": {"enabled": False}})
        )
        path = build_stop_hook_settings_file("up-disabled")
        settings = json.loads(Path(path).read_text())
        commands = _matcherless_commands(settings)
        assert not any("usage_pause_hook.py" in c for c in commands)

    def test_usage_pause_hook_uses_configured_values(self):
        config_module.CONFIG_PATH.write_text(
            json.dumps({
                "usage_pause": {
                    "enabled": True,
                    "threshold": 0.6,
                    "state_file": "/tmp/custom-statusline.json",
                    "max_stale_seconds": 120,
                }
            })
        )
        path = build_stop_hook_settings_file("up-custom")
        settings = json.loads(Path(path).read_text())
        commands = _matcherless_commands(settings)
        matches = [c for c in commands if "usage_pause_hook.py" in c]
        command = matches[0]
        assert "0.6" in command
        assert "/tmp/custom-statusline.json" in command
        assert "120" in command

    def test_existing_hooks_unchanged_with_usage_pause_enabled(self):
        marker = "up-parity"
        path = build_stop_hook_settings_file(marker)
        settings = json.loads(Path(path).read_text())
        hooks = settings["hooks"]

        assert hooks["Stop"][0]["hooks"][0]["command"] == f"echo [worker-done:{marker}]"
        pre_askuserquestion = [e for e in hooks["PreToolUse"] if e.get("matcher") == "AskUserQuestion"]
        assert len(pre_askuserquestion) == 1
        post = hooks["PostToolUse"][0]
        assert post["matcher"] == "AskUserQuestion"

    @pytest.mark.parametrize(
        "context_pause_enabled,usage_pause_enabled",
        [(True, True), (True, False), (False, True), (False, False)],
    )
    def test_context_and_usage_pause_are_independent(
        self, context_pause_enabled, usage_pause_enabled
    ):
        """Each of the 4 enabled/disabled combinations produces exactly the
        expected set of matcherless PreToolUse hooks."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({
                "context_pause": {"enabled": context_pause_enabled},
                "usage_pause": {"enabled": usage_pause_enabled},
            })
        )
        marker = f"combo-{context_pause_enabled}-{usage_pause_enabled}"
        path = build_stop_hook_settings_file(marker)
        settings = json.loads(Path(path).read_text())
        commands = _matcherless_commands(settings)

        has_context = any("context_pause_hook.py" in c for c in commands)
        has_usage = any("usage_pause_hook.py" in c for c in commands)
        assert has_context is context_pause_enabled
        assert has_usage is usage_pause_enabled
        assert len(commands) == sum([context_pause_enabled, usage_pause_enabled])


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
