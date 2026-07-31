"""
Tests for per-worker effort selection feature.

Mirrors test_per_worker_model.py exactly, one-for-one, for the `effort`
field (passed as `--effort <value>` to the claude CLI).

Covers:
  (a) Per-worker effort appends --effort X to claude CLI args
  (b) defaults.effort used when per-worker effort absent
  (c) Per-worker effort overrides defaults.effort
  (d) No --effort flag when neither per-worker nor defaults.effort is set
  (e) Default command string is unchanged / settings injection still happens
  (f) Config parsing accepts and stores defaults.effort
  (g) Invalid effort levels raise ConfigError
  (h) Codex backend accepts and ignores effort
  (i) Backend parity: tmux + iTerm both flow effort through build_full_command
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import maniple_mcp.session_state as session_state
from maniple_mcp import config as config_module
from maniple_mcp.cli_backends import ClaudeCLI, CodexCLI
from maniple_mcp.config import (
    ConfigError,
    DefaultsConfig,
    default_config,
    parse_config,
)
from maniple_mcp.registry import SessionRegistry
from maniple_mcp.terminal_backends.base import TerminalSession
from maniple_mcp.tools import spawn_workers as spawn_workers_module
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# (a) Per-worker effort appends --effort X
# ---------------------------------------------------------------------------

class TestClaudeCLIBuildArgsEffort:
    """Unit tests for ClaudeCLI.build_args effort parameter."""

    def test_build_args_effort_appends_flag(self):
        """build_args(effort='high') should add ['--effort', 'high']."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(effort="high")
        assert "--effort" in args
        idx = args.index("--effort")
        assert args[idx + 1] == "high"

    # (d) No --effort when neither per-worker nor defaults set
    def test_build_args_no_effort_omits_flag(self):
        """build_args() with no effort should not include --effort."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args()
        assert "--effort" not in args

    def test_build_args_none_effort_omits_flag(self):
        """build_args(effort=None) should not include --effort."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(effort=None)
        assert "--effort" not in args

    # (e) Default command / settings injection still works
    def test_settings_injection_still_works_with_effort(self):
        """--settings injection should still happen when effort is also set."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(
                effort="max",
                settings_file="/path/to/settings.json",
            )
        assert "--settings" in args
        assert "/path/to/settings.json" in args
        assert "--effort" in args
        assert "max" in args

    def test_custom_command_settings_injection_skipped_with_effort(self):
        """Custom command: --settings still skipped even when effort is set."""
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "happy"}):
            cli = ClaudeCLI()
            args = cli.build_args(
                effort="xhigh",
                settings_file="/path/to/settings.json",
            )
        assert "--settings" not in args
        # Effort is still passed regardless of command
        assert "--effort" in args
        assert "xhigh" in args

    def test_default_command_string_unchanged(self):
        """Default command must remain 'claude' (no effort injected into command itself)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            assert cli.command() == "claude"

    def test_build_full_command_includes_effort(self):
        """build_full_command should include --effort when effort is provided."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(effort="medium")
        assert "--effort medium" in cmd

    def test_build_full_command_no_effort_omits_flag(self):
        """build_full_command without effort should not include --effort."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command()
        assert "--effort" not in cmd

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_build_args_accepts_all_valid_levels(self, level):
        """All CLI-accepted effort levels should be passed through unchanged."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(effort=level)
        assert args == ["--effort", level]


# ---------------------------------------------------------------------------
# (h) Codex backend accepts and ignores effort
# ---------------------------------------------------------------------------

class TestCodexCLIBuildArgsEffort:
    """Codex backend must accept the effort kwarg and ignore it silently."""

    def test_build_args_effort_ignored_no_crash(self):
        cli = CodexCLI()
        args = cli.build_args(effort="high")
        assert "--effort" not in args
        assert "high" not in args

    def test_build_args_no_effort_still_works(self):
        cli = CodexCLI()
        args = cli.build_args()
        assert "--effort" not in args


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------

class TestDefaultsConfigEffort:
    """Tests for defaults.effort in config parsing."""

    def test_defaults_effort_parsed(self):
        """defaults.effort should be parsed from config JSON."""
        data = {"defaults": {"effort": "high"}}
        cfg = parse_config(data)
        assert cfg.defaults.effort == "high"

    def test_defaults_effort_absent_is_none(self):
        """defaults.effort absent from config should default to None."""
        data = {"defaults": {}}
        cfg = parse_config(data)
        assert cfg.defaults.effort is None

    def test_defaults_effort_none_explicit(self):
        """defaults.effort = null in JSON should be accepted and become None."""
        data = {"defaults": {"effort": None}}
        cfg = parse_config(data)
        assert cfg.defaults.effort is None

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_defaults_effort_accepts_all_valid_levels(self, level):
        """All CLI-accepted effort levels should be valid config values --
        including 'max', which settings.json rejects but the CLI flag
        accepts. Must validate against the CLI's set, not settings.json's."""
        data = {"defaults": {"effort": level}}
        cfg = parse_config(data)
        assert cfg.defaults.effort == level

    def test_defaults_effort_invalid_level_raises(self):
        """defaults.effort must be one of the CLI-accepted levels."""
        data = {"defaults": {"effort": "ultracode"}}
        with pytest.raises(ConfigError, match="defaults.effort"):
            parse_config(data)

    def test_defaults_effort_non_string_raises(self):
        """defaults.effort must be a string; integer should raise ConfigError."""
        data = {"defaults": {"effort": 42}}
        with pytest.raises(ConfigError, match="defaults.effort"):
            parse_config(data)

    def test_defaults_effort_unknown_key_no_longer_rejected(self):
        """defaults.effort must be an accepted key in the defaults section."""
        data = {"defaults": {"effort": "high"}}
        # Should not raise "Unknown keys in defaults: effort"
        cfg = parse_config(data)
        assert cfg.defaults.effort == "high"

    def test_defaults_effort_round_trips(self, tmp_path, monkeypatch):
        """defaults.effort should survive a save/load round-trip."""
        from maniple_mcp.config import save_config, load_config

        config_path = tmp_path / "config.json"
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
        cfg = default_config()
        cfg.defaults.effort = "xhigh"
        save_config(cfg, config_path)
        loaded = load_config(config_path)
        assert loaded.defaults.effort == "xhigh"


# ---------------------------------------------------------------------------
# spawn_workers integration tests
# ---------------------------------------------------------------------------

class FakeBackend:
    """Minimal tmux-like backend for spawn_workers effort tests."""

    backend_id = "tmux"

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.prompts: list[dict] = []
        self.sessions: list[TerminalSession] = []
        self.create_calls: list[dict] = []

    async def create_session(
        self,
        name: str | None = None,
        *,
        project_path: str | None = None,
        issue_id: str | None = None,
        coordinator_badge: str | None = None,
        profile: str | None = None,
        profile_customizations: object | None = None,
    ) -> TerminalSession:
        self.create_calls.append({"name": name})
        session = TerminalSession(
            backend_id=self.backend_id,
            native_id=f"session-{len(self.sessions)}",
            handle=None,
        )
        self.sessions.append(session)
        return session

    async def start_agent_in_session(
        self,
        handle: TerminalSession,
        cli: object,
        project_path: str,
        dangerously_skip_permissions: bool = False,
        env: dict[str, str] | None = None,
        stop_hook_marker_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        **kwargs,
    ) -> None:
        self.started.append({
            "handle": handle,
            "cli": cli,
            "project_path": project_path,
            "dangerously_skip_permissions": dangerously_skip_permissions,
            "env": env,
            "stop_hook_marker_id": stop_hook_marker_id,
            "model": model,
            "effort": effort,
        })

    async def send_prompt_for_agent(
        self,
        session: TerminalSession,
        text: str,
        agent_type: str = "claude",
        submit: bool = True,
    ) -> None:
        self.prompts.append({"session": session, "text": text})


def _build_mcp_tool(monkeypatch, config, backend, tmp_path):
    """Helper: wire up spawn_workers with fake dependencies and return (tool, ctx)."""
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)
    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda *_: "cli:claude")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **kwargs: None)
    monkeypatch.setattr(
        spawn_workers_module,
        "generate_worker_prompt",
        lambda *args, **kwargs: "PROMPT",
    )
    monkeypatch.setattr(
        spawn_workers_module,
        "get_coordinator_guidance",
        lambda *args, **kwargs: {"summary": "ok"},
    )

    async def fake_await_marker(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker)
    monkeypatch.setattr(
        session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER"
    )

    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    return tool, ctx, repo_path


@pytest.mark.asyncio
async def test_spawn_workers_per_worker_effort_passed_to_backend(tmp_path, monkeypatch):
    """(a) Per-worker effort field should be forwarded to start_agent_in_session."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    backend = FakeBackend()
    tool, ctx, repo_path = _build_mcp_tool(monkeypatch, config, backend, tmp_path)

    await tool.run(
        {
            "workers": [
                {
                    "project_path": str(repo_path),
                    "name": "Worker1",
                    "effort": "high",
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["effort"] == "high"


@pytest.mark.asyncio
async def test_spawn_workers_defaults_effort_used_when_per_worker_absent(tmp_path, monkeypatch):
    """(b) defaults.effort should be used when per-worker effort is absent."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    config.defaults.effort = "medium"
    backend = FakeBackend()
    tool, ctx, repo_path = _build_mcp_tool(monkeypatch, config, backend, tmp_path)

    await tool.run(
        {
            "workers": [
                {
                    "project_path": str(repo_path),
                    "name": "Worker1",
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["effort"] == "medium"


@pytest.mark.asyncio
async def test_spawn_workers_per_worker_overrides_defaults_effort(tmp_path, monkeypatch):
    """(c) Per-worker effort should override defaults.effort."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    config.defaults.effort = "medium"
    backend = FakeBackend()
    tool, ctx, repo_path = _build_mcp_tool(monkeypatch, config, backend, tmp_path)

    await tool.run(
        {
            "workers": [
                {
                    "project_path": str(repo_path),
                    "name": "Worker1",
                    "effort": "max",  # should win
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["effort"] == "max"


@pytest.mark.asyncio
async def test_spawn_workers_no_effort_when_neither_set(tmp_path, monkeypatch):
    """(d) No effort should be passed when neither per-worker nor defaults.effort is set."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    # defaults.effort is None by default
    backend = FakeBackend()
    tool, ctx, repo_path = _build_mcp_tool(monkeypatch, config, backend, tmp_path)

    await tool.run(
        {
            "workers": [
                {
                    "project_path": str(repo_path),
                    "name": "Worker1",
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["effort"] is None


# ---------------------------------------------------------------------------
# (i) Backend parity: tmux + iTerm both route effort through build_full_command
# ---------------------------------------------------------------------------

class TestBackendParityEffort:
    """Both terminal backends funnel start_agent_in_session's `effort` kwarg
    through AgentCLI.build_full_command -- the single choke point (per
    base.py's build_full_command docstring/comment). This proves both
    backends get effort "for free" through that shared plumbing, rather
    than each needing its own bespoke --effort handling."""

    @pytest.mark.asyncio
    async def test_tmux_backend_forwards_effort_to_build_full_command(self, monkeypatch):
        from maniple_mcp.terminal_backends.tmux import TmuxBackend

        captured = {}

        class FakeCLI:
            def supports_settings_file(self):
                return False

            def build_full_command(self, **kwargs):
                captured.update(kwargs)
                return "claude"

            engine_id = "claude"

        backend = TmuxBackend.__new__(TmuxBackend)

        async def fake_wait_shell(*args, **kwargs):
            return True

        async def fake_wait_agent(*args, **kwargs):
            return True

        async def fake_send_prompt(*args, **kwargs):
            return None

        monkeypatch.setattr(backend, "_wait_for_shell_ready", fake_wait_shell)
        monkeypatch.setattr(backend, "_wait_for_agent_ready", fake_wait_agent)
        monkeypatch.setattr(backend, "send_prompt", fake_send_prompt)

        handle = TerminalSession(backend_id="tmux", native_id="%1", handle="%1")
        await backend.start_agent_in_session(
            handle=handle,
            cli=FakeCLI(),
            project_path="/tmp/repo",
            effort="high",
        )

        assert captured.get("effort") == "high"

    @pytest.mark.asyncio
    async def test_iterm_backend_forwards_effort_to_iterm_utils(self, monkeypatch):
        from maniple_mcp.terminal_backends import iterm as iterm_backend_module
        from maniple_mcp.terminal_backends.iterm import ItermBackend

        captured = {}

        async def fake_start_agent_in_session(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            iterm_backend_module.iterm_utils,
            "start_agent_in_session",
            fake_start_agent_in_session,
        )

        backend = ItermBackend.__new__(ItermBackend)
        handle = TerminalSession(backend_id="iterm", native_id="sess-1", handle="sess-1")

        monkeypatch.setattr(backend, "unwrap_session", lambda h: h)

        await backend.start_agent_in_session(
            handle=handle,
            cli=object(),
            project_path="/tmp/repo",
            effort="xhigh",
        )

        assert captured.get("effort") == "xhigh"
