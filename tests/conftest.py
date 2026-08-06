"""Shared pytest fixtures for test isolation."""

from pathlib import Path

import pytest

from maniple_mcp import config as config_module
from maniple_mcp import worker_manifest as worker_manifest_module


@pytest.fixture(autouse=True)
def isolate_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Point config path to a temp file so user config doesn't affect tests."""
    if "test_config_cli.py" in request.node.nodeid:
        return
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)


@pytest.fixture(autouse=True)
def isolate_worker_manifest_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the worker manifest dir to a temp dir so no test can ever write
    a real ~/.maniple/workers/<id>.json (see test_worker_manifest.py and
    test_close_workers_manifest.py for tests that override this further)."""
    monkeypatch.setattr(
        worker_manifest_module, "MANIFEST_DIR", tmp_path / "manifest-dir-isolation"
    )
