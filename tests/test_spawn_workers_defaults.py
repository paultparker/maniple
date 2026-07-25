"""Tests for spawn_workers config defaults."""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

import maniple_mcp.session_state as session_state
from maniple_mcp.config import ConfigError, DefaultsConfig, default_config
from maniple_mcp.coordinator_identity import CoordinatorIdentity
from maniple_mcp.registry import SessionRegistry
from maniple_mcp.terminal_backends.base import TerminalSession
from maniple_mcp.tools import spawn_workers as spawn_workers_module


@pytest.fixture(autouse=True)
def _empty_coordinator_identity(monkeypatch):
    """Stub out coordinator-identity capture so these tests are deterministic
    -- this suite runs inside a real tmux+claude worker, so the ambient
    TMUX/CLAUDE_* env would otherwise leak a real (non-empty) identity into
    every spawned worker's env, breaking `env is None` assertions below.
    Tests that specifically exercise coordinator-identity injection
    override this fixture's monkeypatch with their own identity."""
    monkeypatch.setattr(
        spawn_workers_module, "get_coordinator_identity", lambda: CoordinatorIdentity()
    )
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **kwargs: None)


class FakeBackend:
    """Minimal tmux-like backend for spawn_workers tests."""

    backend_id = "tmux"

    def __init__(self) -> None:
        self.started = []
        self.prompts = []
        self.sessions = []
        self.create_calls = []

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
        self.create_calls.append({
            "name": name,
            "project_path": project_path,
            "issue_id": issue_id,
            "coordinator_badge": coordinator_badge,
        })
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
        **kwargs,
    ) -> None:
        self.started.append({
            "handle": handle,
            "cli": cli,
            "project_path": project_path,
            "dangerously_skip_permissions": dangerously_skip_permissions,
            "env": env,
            "stop_hook_marker_id": stop_hook_marker_id,
        })

    async def send_prompt_for_agent(
        self,
        session: TerminalSession,
        text: str,
        agent_type: str = "claude",
        submit: bool = True,
    ) -> None:
        self.prompts.append({
            "session": session,
            "text": text,
            "agent_type": agent_type,
            "submit": submit,
        })


@pytest.mark.asyncio
async def test_spawn_workers_uses_config_defaults(tmp_path, monkeypatch):
    """spawn_workers should apply config defaults when fields are omitted."""
    config = default_config()
    config.defaults = DefaultsConfig(
        agent_type="codex",
        skip_permissions=True,
        use_worktree=False,
        layout="new",
    )
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)

    seen_agent_types = []

    def fake_get_cli_backend(agent_type: str):
        seen_agent_types.append(agent_type)
        return f"cli:{agent_type}"

    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", fake_get_cli_backend)

    def fail_create_local_worktree(*args, **kwargs):
        raise AssertionError("create_local_worktree should not be called")

    monkeypatch.setattr(
        spawn_workers_module,
        "create_local_worktree",
        fail_create_local_worktree,
    )
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)

    prompt_calls = []

    def fake_generate_worker_prompt(*args, **kwargs):
        prompt_calls.append(kwargs.get("use_worktree"))
        return "PROMPT"

    monkeypatch.setattr(
        spawn_workers_module,
        "generate_worker_prompt",
        fake_generate_worker_prompt,
    )
    monkeypatch.setattr(
        spawn_workers_module,
        "get_coordinator_guidance",
        lambda *args, **kwargs: {"summary": "ok"},
    )

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    backend = FakeBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")
    assert tool is not None

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [{"project_path": str(repo_path), "name": "Worker1"}],
    }, context=ctx)

    assert result["layout"] == "new"
    assert seen_agent_types == ["codex"]
    assert backend.started[0]["dangerously_skip_permissions"] is True
    assert backend.started[0]["env"] == {"CI": "1"}
    assert result["sessions"]["Worker1"]["agent_type"] == "codex"
    assert prompt_calls == [False]


@pytest.mark.asyncio
async def test_spawn_workers_invalid_config_falls_back(tmp_path, monkeypatch):
    """spawn_workers should fall back to defaults if config is invalid."""
    def raise_config_error():
        raise ConfigError("invalid config")

    monkeypatch.setattr(spawn_workers_module, "load_config", raise_config_error)

    seen_agent_types = []

    def fake_get_cli_backend(agent_type: str):
        seen_agent_types.append(agent_type)
        return f"cli:{agent_type}"

    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", fake_get_cli_backend)

    def fake_create_local_worktree(repo_path, **kwargs):
        return repo_path

    monkeypatch.setattr(
        spawn_workers_module,
        "create_local_worktree",
        fake_create_local_worktree,
    )
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)

    prompt_calls = []

    def fake_generate_worker_prompt(*args, **kwargs):
        prompt_calls.append(kwargs.get("use_worktree"))
        return "PROMPT"

    monkeypatch.setattr(
        spawn_workers_module,
        "generate_worker_prompt",
        fake_generate_worker_prompt,
    )
    monkeypatch.setattr(
        spawn_workers_module,
        "get_coordinator_guidance",
        lambda *args, **kwargs: {"summary": "ok"},
    )

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    backend = FakeBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")
    assert tool is not None

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [{"project_path": str(repo_path), "name": "Worker1"}],
    }, context=ctx)

    assert result["layout"] == "auto"
    assert seen_agent_types == ["claude"]
    assert backend.started[0]["dangerously_skip_permissions"] is False
    assert backend.started[0]["env"] is None
    assert result["sessions"]["Worker1"]["agent_type"] == "claude"
    assert prompt_calls == [True]


@pytest.mark.asyncio
async def test_spawn_workers_merges_codex_ci_with_worktree_tracker_env(tmp_path, monkeypatch):
    """Codex workers should include CI=1 alongside worktree tracker env vars."""
    config = default_config()
    config.defaults = DefaultsConfig(
        agent_type="codex",
        skip_permissions=False,
        use_worktree=False,
        layout="new",
    )
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)
    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda *_: "cli:codex")
    monkeypatch.setattr(
        spawn_workers_module,
        "get_worktree_tracker_dir",
        lambda *_: ("MANIPLE_WORKTREE_TRACKER_DIR", "/tmp/tracker"),
    )
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

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    backend = FakeBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")
    assert tool is not None

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    await tool.run({
        "workers": [{"project_path": str(repo_path), "name": "Worker1"}],
    }, context=ctx)

    assert backend.started[0]["env"] == {
        "MANIPLE_WORKTREE_TRACKER_DIR": "/tmp/tracker",
        "CI": "1",
    }


@pytest.mark.asyncio
async def test_spawn_workers_sets_badge_metadata(tmp_path, monkeypatch):
    """spawn_workers should forward badge metadata to sessions."""
    config = default_config()
    config.defaults = DefaultsConfig(
        agent_type="claude",
        skip_permissions=False,
        use_worktree=False,
        layout="new",
    )
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)
    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda *_: "cli:claude")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
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

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    backend = FakeBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")
    assert tool is not None

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [{
            "project_path": str(repo_path),
            "name": "Worker1",
            "badge": "Preferred badge",
        }],
    }, context=ctx)

    session = result["sessions"]["Worker1"]
    assert session["coordinator_badge"] == "Preferred badge"
    assert backend.create_calls[0]["coordinator_badge"] == "Preferred badge"


async def _run_single_claude_spawn(tmp_path, monkeypatch, backend, worker_overrides=None):
    """Shared setup for a single-claude-worker spawn_workers.run() call."""
    config = default_config()
    config.defaults = DefaultsConfig(
        agent_type="claude",
        skip_permissions=False,
        use_worktree=False,
        layout="new",
    )
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)
    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda *_: "cli:claude")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
    monkeypatch.setattr(
        spawn_workers_module, "generate_worker_prompt", lambda *args, **kwargs: "PROMPT"
    )
    monkeypatch.setattr(
        spawn_workers_module,
        "get_coordinator_guidance",
        lambda *args, **kwargs: {"summary": "ok"},
    )

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    worker = {"project_path": str(repo_path), "name": "Worker1"}
    worker.update(worker_overrides or {})

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    return await tool.run({"workers": [worker]}, context=ctx)


@pytest.mark.asyncio
async def test_spawn_workers_injects_coordinator_env_vars(tmp_path, monkeypatch):
    """When the coordinator identity is known, its MANIPLE_COORDINATOR_* env
    vars are merged into the worker's launch env -- the single choke point
    (build_full_command via the env param) that both backends funnel
    through, so this is backend-parity by construction."""
    identity = CoordinatorIdentity(
        pid=4242,
        session_id="coord-sess-1",
        tmux_session_name="⚙ mac-perf--🔄 working-maniple-0724-1019",
        tmux_window_index="1",
        tmux_pane_index="0",
    )
    monkeypatch.setattr(spawn_workers_module, "get_coordinator_identity", lambda: identity)
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **kwargs: None)

    backend = FakeBackend()
    await _run_single_claude_spawn(tmp_path, monkeypatch, backend)

    env = backend.started[0]["env"]
    assert env["MANIPLE_COORDINATOR_PID"] == "4242"
    assert env["MANIPLE_COORDINATOR_SESSION_ID"] == "coord-sess-1"
    assert env["MANIPLE_COORDINATOR_TMUX"] == (
        "⚙ mac-perf--🔄 working-maniple-0724-1019:1.0"
    )


@pytest.mark.asyncio
async def test_spawn_workers_env_stays_none_when_identity_is_empty(tmp_path, monkeypatch):
    """An empty CoordinatorIdentity (all fields unknown) must not turn a
    worker's env from None into an empty dict -- to_env_vars() returns {}
    and the merge is skipped entirely."""
    monkeypatch.setattr(
        spawn_workers_module, "get_coordinator_identity", lambda: CoordinatorIdentity()
    )
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **kwargs: None)

    backend = FakeBackend()
    await _run_single_claude_spawn(tmp_path, monkeypatch, backend)

    assert backend.started[0]["env"] is None


@pytest.mark.asyncio
async def test_spawn_workers_writes_manifest_per_worker(tmp_path, monkeypatch):
    """spawn_workers writes a best-effort manifest for each spawned worker,
    carrying the worker's own metadata plus the full coordinator identity."""
    identity = CoordinatorIdentity(pid=555, session_id="coord-sess-2")
    monkeypatch.setattr(spawn_workers_module, "get_coordinator_identity", lambda: identity)

    manifest_calls = []
    monkeypatch.setattr(
        spawn_workers_module,
        "write_worker_manifest",
        lambda **kwargs: manifest_calls.append(kwargs),
    )

    backend = FakeBackend()
    result = await _run_single_claude_spawn(
        tmp_path, monkeypatch, backend, worker_overrides={"model": "claude-sonnet-5"}
    )

    assert len(manifest_calls) == 1
    call = manifest_calls[0]
    session = result["sessions"]["Worker1"]
    assert call["worker_session_id"] == session["session_id"]
    assert call["name"] == "Worker1"
    assert call["agent_type"] == "claude"
    assert call["model"] == "claude-sonnet-5"
    assert call["coordinator"] is identity


@pytest.mark.asyncio
async def test_spawn_workers_manifest_failure_does_not_block_spawn(tmp_path, monkeypatch):
    """A manifest write failure must never fail the spawn. write_worker_
    manifest itself already guarantees never-raises (test_worker_manifest.py),
    but spawn_workers also guards its own call site: by this point in the
    tool the sessions are already spawned/registered, and the tool's outer
    try/except would otherwise turn a hypothetical manifest regression into
    a misleading "spawn failed" response even though workers are alive."""
    monkeypatch.setattr(
        spawn_workers_module, "get_coordinator_identity", lambda: CoordinatorIdentity()
    )

    def _raising_write_worker_manifest(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        spawn_workers_module, "write_worker_manifest", _raising_write_worker_manifest
    )

    backend = FakeBackend()
    result = await _run_single_claude_spawn(tmp_path, monkeypatch, backend)

    assert "error" not in result
    assert result["count"] == 1
    assert "Worker1" in result["sessions"]
