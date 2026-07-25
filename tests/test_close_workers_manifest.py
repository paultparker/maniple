"""
Tests for close_workers' worker-manifest closed_at stamping.

Only covers the manifest-stamping addition (component 3 of the zombie-
worker spec) -- not a general close_workers regression suite.
"""

import json
from unittest.mock import AsyncMock

import pytest

from maniple_mcp import worker_manifest as wm_module
from maniple_mcp.registry import SessionRegistry, SessionStatus
from maniple_mcp.terminal_backends.base import TerminalSession
from maniple_mcp.tools.close_workers import _close_single_worker
from maniple_mcp.worker_manifest import manifest_path, write_worker_manifest
from maniple_mcp.coordinator_identity import CoordinatorIdentity


@pytest.fixture(autouse=True)
def _isolate_manifest_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wm_module, "MANIFEST_DIR", tmp_path / "workers")


def _mock_backend(*, close_session_raises=False):
    backend = AsyncMock()
    backend.send_key = AsyncMock()
    backend.send_text = AsyncMock()
    if close_session_raises:
        backend.close_session = AsyncMock(side_effect=RuntimeError("terminal gone"))
    else:
        backend.close_session = AsyncMock()
    return backend


def _make_session(registry: SessionRegistry, session_id: str = "w1"):
    terminal_session = TerminalSession(backend_id="tmux", native_id="%1", handle=None)
    return registry.add(
        terminal_session=terminal_session,
        project_path="/repo",
        name="Groucho",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_close_stamps_closed_at_on_existing_manifest():
    write_worker_manifest(
        worker_session_id="w1",
        name="Groucho",
        agent_type="claude",
        terminal_id="tmux:%1",
        project_path="/repo",
        worktree_path=None,
        main_repo_path=None,
        model=None,
        coordinator=CoordinatorIdentity(),
    )

    registry = SessionRegistry()
    session = _make_session(registry)
    backend = _mock_backend()

    result = await _close_single_worker(backend, session, "w1", registry)

    assert result["success"] is True
    manifest = json.loads(manifest_path("w1").read_text())
    assert "closed_at" in manifest["worker"]


@pytest.mark.asyncio
async def test_close_does_not_delete_manifest_file():
    write_worker_manifest(
        worker_session_id="w2",
        name="Harpo",
        agent_type="claude",
        terminal_id="tmux:%2",
        project_path="/repo",
        worktree_path=None,
        main_repo_path=None,
        model=None,
        coordinator=CoordinatorIdentity(),
    )

    registry = SessionRegistry()
    session = _make_session(registry, "w2")
    backend = _mock_backend()

    await _close_single_worker(backend, session, "w2", registry)

    assert manifest_path("w2").exists()


@pytest.mark.asyncio
async def test_close_missing_manifest_does_not_fail_close():
    """No manifest was ever written for this worker (e.g. pre-feature
    worker) -- close still succeeds normally."""
    registry = SessionRegistry()
    session = _make_session(registry, "no-manifest")
    backend = _mock_backend()

    result = await _close_single_worker(backend, session, "no-manifest", registry)

    assert result["success"] is True
    assert not manifest_path("no-manifest").exists()


@pytest.mark.asyncio
async def test_close_stamps_manifest_even_when_close_session_raises():
    """The exception fallback path in _close_single_worker also stamps
    closed_at -- a worker whose terminal is already gone still gets its
    manifest updated instead of leaving it silently stale."""
    write_worker_manifest(
        worker_session_id="w3",
        name="Chico",
        agent_type="claude",
        terminal_id="tmux:%3",
        project_path="/repo",
        worktree_path=None,
        main_repo_path=None,
        model=None,
        coordinator=CoordinatorIdentity(),
    )

    registry = SessionRegistry()
    session = _make_session(registry, "w3")
    backend = _mock_backend(close_session_raises=True)

    result = await _close_single_worker(backend, session, "w3", registry)

    assert result["success"] is True
    assert "warning" in result
    manifest = json.loads(manifest_path("w3").read_text())
    assert "closed_at" in manifest["worker"]


@pytest.mark.asyncio
async def test_close_busy_without_force_does_not_stamp_manifest():
    """A busy-session rejection returns early before any close action --
    the manifest must stay untouched (worker is still running)."""
    write_worker_manifest(
        worker_session_id="w4",
        name="Zeppo",
        agent_type="claude",
        terminal_id="tmux:%4",
        project_path="/repo",
        worktree_path=None,
        main_repo_path=None,
        model=None,
        coordinator=CoordinatorIdentity(),
    )

    registry = SessionRegistry()
    session = _make_session(registry, "w4")
    registry.update_status("w4", SessionStatus.BUSY)
    backend = _mock_backend()

    result = await _close_single_worker(backend, session, "w4", registry, force=False)

    assert result["success"] is False
    manifest = json.loads(manifest_path("w4").read_text())
    assert "closed_at" not in manifest["worker"]
