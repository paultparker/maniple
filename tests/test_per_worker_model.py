"""
Tests for per-worker model selection feature.

Covers:
  (a) Per-worker model appends --model X to claude CLI args
  (b) defaults.model used when per-worker model absent
  (c) Per-worker model overrides defaults.model
  (d) No --model flag when neither per-worker nor defaults.model is set
  (e) Default command string is unchanged / settings injection still happens
  (f) Config parsing accepts and stores defaults.model
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import maniple_mcp.session_state as session_state
from maniple_mcp import config as config_module
from maniple_mcp.cli_backends import ClaudeCLI
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
# (a) Per-worker model appends --model X
# ---------------------------------------------------------------------------

class TestClaudeCLIBuildArgsModel:
    """Unit tests for ClaudeCLI.build_args model parameter."""

    def _make_cli(self) -> ClaudeCLI:
        """Return a ClaudeCLI using the default command (no env override)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
        return ClaudeCLI()

    def test_build_args_model_appends_flag(self):
        """build_args(model='claude-opus-4-5') should add ['--model', 'claude-opus-4-5']."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(model="claude-opus-4-5")
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "claude-opus-4-5"

    # (d) No --model when neither per-worker nor defaults set
    def test_build_args_no_model_omits_flag(self):
        """build_args() with no model should not include --model."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args()
        assert "--model" not in args

    def test_build_args_none_model_omits_flag(self):
        """build_args(model=None) should not include --model."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(model=None)
        assert "--model" not in args

    # (e) Default command / settings injection still works
    def test_settings_injection_still_works_with_model(self):
        """--settings injection should still happen when model is also set."""
        cli = ClaudeCLI()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            args = cli.build_args(
                model="claude-opus-4-5",
                settings_file="/path/to/settings.json",
            )
        assert "--settings" in args
        assert "/path/to/settings.json" in args
        assert "--model" in args
        assert "claude-opus-4-5" in args

    def test_custom_command_settings_injection_skipped_with_model(self):
        """Custom command: --settings still skipped even when model is set."""
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "happy"}):
            cli = ClaudeCLI()
            args = cli.build_args(
                model="claude-opus-4-5",
                settings_file="/path/to/settings.json",
            )
        assert "--settings" not in args
        # Model is still passed regardless of command
        assert "--model" in args
        assert "claude-opus-4-5" in args

    def test_default_command_string_unchanged(self):
        """Default command must remain 'claude' (no model injected into command itself)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            assert cli.command() == "claude"

    def test_build_full_command_includes_model(self):
        """build_full_command should include --model when model is provided."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(model="claude-sonnet-4-5")
        assert "--model claude-sonnet-4-5" in cmd

    def test_build_full_command_no_model_omits_flag(self):
        """build_full_command without model should not include --model."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command()
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------

class TestDefaultsConfigModel:
    """Tests for defaults.model in config parsing."""

    def test_defaults_model_parsed(self):
        """defaults.model should be parsed from config JSON."""
        data = {"defaults": {"model": "claude-opus-4-5"}}
        cfg = parse_config(data)
        assert cfg.defaults.model == "claude-opus-4-5"

    def test_defaults_model_absent_is_none(self):
        """defaults.model absent from config should default to None."""
        data = {"defaults": {}}
        cfg = parse_config(data)
        assert cfg.defaults.model is None

    def test_defaults_model_none_explicit(self):
        """defaults.model = null in JSON should be accepted and become None."""
        data = {"defaults": {"model": None}}
        cfg = parse_config(data)
        assert cfg.defaults.model is None

    def test_defaults_model_non_string_raises(self):
        """defaults.model must be a string; integer should raise ConfigError."""
        data = {"defaults": {"model": 42}}
        with pytest.raises(ConfigError, match="defaults.model"):
            parse_config(data)

    def test_defaults_model_empty_string_raises(self):
        """defaults.model must not be empty."""
        data = {"defaults": {"model": ""}}
        with pytest.raises(ConfigError, match="defaults.model"):
            parse_config(data)

    def test_defaults_model_round_trips(self, tmp_path, monkeypatch):
        """defaults.model should survive a save/load round-trip."""
        from maniple_mcp.config import save_config, load_config

        config_path = tmp_path / "config.json"
        monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
        cfg = default_config()
        cfg.defaults.model = "claude-haiku-4-5"
        save_config(cfg, config_path)
        loaded = load_config(config_path)
        assert loaded.defaults.model == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# spawn_workers integration tests
# ---------------------------------------------------------------------------

class FakeBackend:
    """Minimal tmux-like backend for spawn_workers model tests."""

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
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **_: None)
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
async def test_spawn_workers_per_worker_model_passed_to_backend(tmp_path, monkeypatch):
    """(a) Per-worker model field should be forwarded to start_agent_in_session."""
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
                    "model": "claude-opus-4-5",
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["model"] == "claude-opus-4-5"


@pytest.mark.asyncio
async def test_spawn_workers_defaults_model_used_when_per_worker_absent(tmp_path, monkeypatch):
    """(b) defaults.model should be used when per-worker model is absent."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    config.defaults.model = "claude-haiku-4-5"
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

    assert backend.started[0]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_spawn_workers_per_worker_overrides_defaults_model(tmp_path, monkeypatch):
    """(c) Per-worker model should override defaults.model."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    config.defaults.model = "claude-haiku-4-5"
    backend = FakeBackend()
    tool, ctx, repo_path = _build_mcp_tool(monkeypatch, config, backend, tmp_path)

    await tool.run(
        {
            "workers": [
                {
                    "project_path": str(repo_path),
                    "name": "Worker1",
                    "model": "claude-opus-4-5",  # should win
                }
            ]
        },
        context=ctx,
    )

    assert backend.started[0]["model"] == "claude-opus-4-5"


@pytest.mark.asyncio
async def test_spawn_workers_no_model_when_neither_set(tmp_path, monkeypatch):
    """(d) No model should be passed when neither per-worker nor defaults.model is set."""
    config = default_config()
    config.defaults = DefaultsConfig(use_worktree=False, layout="new")
    # defaults.model is None by default
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

    assert backend.started[0]["model"] is None
