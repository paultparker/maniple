"""
Tests for worker_manifest.py.

Covers: manifest written at spawn, closed_at stamped (not deleted) on
close, and best-effort behavior surviving I/O errors so a manifest failure
never blocks or fails a spawn/close.
"""

import json

import pytest

from maniple_mcp import worker_manifest as wm_module
from maniple_mcp.coordinator_identity import CoordinatorIdentity
from maniple_mcp.worker_manifest import (
    manifest_path,
    stamp_worker_closed,
    write_worker_manifest,
)


@pytest.fixture(autouse=True)
def _isolate_manifest_dir(tmp_path, monkeypatch):
    manifest_dir = tmp_path / "workers"
    monkeypatch.setattr(wm_module, "MANIFEST_DIR", manifest_dir)
    return manifest_dir


def _write_default_manifest(worker_session_id="abc123", coordinator=None):
    write_worker_manifest(
        worker_session_id=worker_session_id,
        name="Groucho",
        agent_type="claude",
        terminal_id="tmux:%1",
        project_path="/repo",
        worktree_path="/repo-worktree",
        main_repo_path="/repo",
        model="claude-sonnet-5",
        coordinator=coordinator or CoordinatorIdentity(),
    )


class TestWriteWorkerManifest:
    def test_creates_manifest_dir_if_missing(self, tmp_path):
        manifest_dir = tmp_path / "workers"
        assert not manifest_dir.exists()
        _write_default_manifest()
        assert manifest_dir.exists()

    def test_writes_manifest_file_named_by_session_id(self):
        _write_default_manifest(worker_session_id="xyz789")
        assert manifest_path("xyz789").exists()

    def test_manifest_has_schema_version_1(self):
        _write_default_manifest()
        data = json.loads(manifest_path("abc123").read_text())
        assert data["schema_version"] == 1

    def test_manifest_worker_block_fields(self):
        _write_default_manifest()
        data = json.loads(manifest_path("abc123").read_text())
        worker = data["worker"]
        assert worker["session_id"] == "abc123"
        assert worker["name"] == "Groucho"
        assert worker["agent_type"] == "claude"
        assert worker["terminal_id"] == "tmux:%1"
        assert worker["project_path"] == "/repo"
        assert worker["worktree_path"] == "/repo-worktree"
        assert worker["main_repo_path"] == "/repo"
        assert worker["model"] == "claude-sonnet-5"
        assert "spawned_at" in worker
        assert worker["spawned_at"]  # non-empty

    def test_manifest_worker_block_allows_none_fields(self):
        write_worker_manifest(
            worker_session_id="noopt1",
            name="Harpo",
            agent_type="codex",
            terminal_id="iterm:UUID",
            project_path="/repo2",
            worktree_path=None,
            main_repo_path=None,
            model=None,
            coordinator=CoordinatorIdentity(),
        )
        data = json.loads(manifest_path("noopt1").read_text())
        worker = data["worker"]
        assert worker["worktree_path"] is None
        assert worker["main_repo_path"] is None
        assert worker["model"] is None

    def test_manifest_coordinator_block_is_full_identity_dict(self):
        identity = CoordinatorIdentity(
            pid=111,
            session_id="coord-sess",
            tmux_session_name="mysession",
            tmux_window_index="1",
            tmux_pane_index="0",
        )
        _write_default_manifest(coordinator=identity)
        data = json.loads(manifest_path("abc123").read_text())
        assert data["coordinator"] == identity.to_dict()

    def test_write_never_raises_on_io_error(self, monkeypatch):
        """Manifest writing must never block/fail a spawn -- even if the
        underlying filesystem write raises."""

        def _boom(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(wm_module.Path, "write_text", _boom)

        # Must not raise.
        _write_default_manifest(worker_session_id="ioerr1")

    def test_write_never_raises_when_mkdir_fails(self, monkeypatch):
        def _boom(self, *args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(wm_module.Path, "mkdir", _boom)

        # Must not raise.
        _write_default_manifest(worker_session_id="mkdirerr1")


class TestStampWorkerClosed:
    def test_stamps_closed_at_on_existing_manifest(self):
        _write_default_manifest(worker_session_id="close1")
        stamp_worker_closed("close1")
        data = json.loads(manifest_path("close1").read_text())
        assert "closed_at" in data["worker"]
        assert data["worker"]["closed_at"]

    def test_does_not_delete_the_manifest_file(self):
        _write_default_manifest(worker_session_id="close2")
        stamp_worker_closed("close2")
        assert manifest_path("close2").exists()

    def test_preserves_other_worker_fields_when_stamping(self):
        _write_default_manifest(worker_session_id="close3")
        stamp_worker_closed("close3")
        data = json.loads(manifest_path("close3").read_text())
        assert data["worker"]["name"] == "Groucho"
        assert data["worker"]["session_id"] == "close3"

    def test_missing_manifest_is_a_noop_not_an_error(self):
        # No manifest was ever written for this id -- must not raise.
        stamp_worker_closed("never-existed")
        assert not manifest_path("never-existed").exists()

    def test_stamp_never_raises_on_corrupt_manifest(self):
        manifest_path("corrupt1").parent.mkdir(parents=True, exist_ok=True)
        manifest_path("corrupt1").write_text("not valid json{{{")

        # Must not raise.
        stamp_worker_closed("corrupt1")

    def test_stamp_never_raises_on_io_error(self, monkeypatch):
        _write_default_manifest(worker_session_id="close4")

        def _boom(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(wm_module.Path, "write_text", _boom)

        # Must not raise.
        stamp_worker_closed("close4")
