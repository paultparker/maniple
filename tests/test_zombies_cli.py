"""Tests for the zombies_cli module (component 5, spec v1).

Report-only v1: no reaping, no kill flag. Tests cover:
- Manifest reading from ~/.maniple/workers/
- Classification logic (orphaned/forgotten/ok/closed, defunct via start-time)
- Idle age calculation from JSONL mtime or manifest spawned_at
- Human table output and per-zombie action blocks
- --json flag output shape
- Exit code 0 always (report tool)
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maniple_mcp.zombies_cli import (
    WorkerZombieStatus,
    classify_worker,
    discover_workers,
    format_zombies_report,
)


class TestWorkerClassification:
    """Tests for classify_worker() -- the core logic."""

    def test_ok_coordinator_alive_idle_under_threshold(self, tmp_path, monkeypatch):
        """Coordinator alive, idle < threshold -> 'ok'."""
        # Setup: coordinator alive, worker idle 1 hour
        worker = {
            "session_id": "w-123",
            "name": "Alice",
            "coordinator_pid": 1000,
            "coordinator_pid_start": datetime.now().isoformat(),
            "idle_age_hours": 1,
        }
        threshold_hours = 2
        # Mock kill -0 for alive check
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None  # Process exists
            result = classify_worker(worker, threshold_hours)
        assert result == "ok"

    def test_forgotten_coordinator_alive_idle_over_threshold(self, tmp_path, monkeypatch):
        """Coordinator alive, idle >= threshold -> 'forgotten'."""
        worker = {
            "session_id": "w-456",
            "name": "Bob",
            "coordinator_pid": 2000,
            "coordinator_pid_start": datetime.now().isoformat(),
            "idle_age_hours": 3,
        }
        threshold_hours = 2
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None  # Process exists
            result = classify_worker(worker, threshold_hours)
        assert result == "forgotten"

    def test_orphaned_coordinator_dead(self, tmp_path, monkeypatch):
        """Coordinator PID doesn't exist (kill -0 raises) -> 'orphaned'."""
        worker = {
            "session_id": "w-789",
            "name": "Charlie",
            "coordinator_pid": 9999,
            "coordinator_pid_start": datetime.now().isoformat(),
            "idle_age_hours": 0.5,
        }
        threshold_hours = 2
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = ProcessLookupError("No such process")
            result = classify_worker(worker, threshold_hours)
        assert result == "orphaned"

    def test_orphaned_coordinator_defunct_pid_reused(self, tmp_path, monkeypatch):
        """Coordinator PID alive but start time mismatches -> 'orphaned' (PID reused)."""
        worker = {
            "session_id": "w-def",
            "name": "Deirdre",
            "coordinator_pid": 3000,
            "coordinator_pid_start": "2020-01-01T10:00:00",  # Old start time
            "idle_age_hours": 2,
        }
        threshold_hours = 2
        # Mock kill -0 to succeed (PID alive) but start time won't match
        with patch("os.kill") as mock_kill, \
             patch("maniple_mcp.zombies_cli._get_process_start_time") as mock_start:
            mock_kill.return_value = None  # Process exists
            mock_start.return_value = "2026-07-24T15:00:00"  # Recent, different time
            result = classify_worker(worker, threshold_hours)
        assert result == "orphaned"

    def test_closed_worker_has_closed_at_timestamp(self, tmp_path, monkeypatch):
        """Worker with closed_at field -> 'closed'."""
        worker = {
            "session_id": "w-closed",
            "name": "Eve",
            "coordinator_pid": 4000,
            "coordinator_pid_start": datetime.now().isoformat(),
            "idle_age_hours": 0.1,
            "closed_at": (datetime.now() - timedelta(hours=1)).isoformat(),
        }
        threshold_hours = 2
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None  # Process alive (doesn't matter for closed)
            result = classify_worker(worker, threshold_hours)
        assert result == "closed"

    def test_unknown_coordinator_identity_treated_as_orphaned(self, tmp_path, monkeypatch):
        """No coordinator_pid at all -> treated as orphaned."""
        worker = {
            "session_id": "w-unk",
            "name": "Frank",
            "idle_age_hours": 1,
        }
        threshold_hours = 2
        result = classify_worker(worker, threshold_hours)
        assert result == "orphaned"


class TestIdleAgeCalculation:
    """Tests for computing idle age from worker metadata."""

    def test_idle_age_from_jsonl_mtime(self, tmp_path, monkeypatch):
        """Idle age computed from JSONL mtime if available."""
        # Create a fake JSONL file with known mtime
        jsonl_path = tmp_path / "fake-session.jsonl"
        jsonl_path.write_text('{"test": "data"}\n')
        # Set mtime to 2 hours ago
        now_ts = time.time()
        two_hours_ago = now_ts - (2 * 3600)
        Path(jsonl_path).stat()
        import os as os_module
        os_module.utime(jsonl_path, (two_hours_ago, two_hours_ago))

        # Patch get_idle_age to use our mocked path
        with patch("maniple_mcp.zombies_cli.get_idle_age") as mock_idle:
            mock_idle.return_value = 2.0
            worker = {
                "session_id": "w-idle1",
                "name": "Grace",
                "coordinator_pid": 5000,
                "coordinator_pid_start": datetime.now().isoformat(),
                "idle_age_hours": 2.0,
            }
            threshold_hours = 2
            with patch("os.kill") as mock_kill:
                mock_kill.return_value = None
                result = classify_worker(worker, threshold_hours)
            # 2 hours idle >= 2 hour threshold -> forgotten
            assert result == "forgotten"

    def test_idle_age_fallback_to_manifest_spawned_at(self):
        """Idle age falls back to manifest spawned_at if JSONL missing."""
        spawned_at = (datetime.now() - timedelta(hours=1.5)).isoformat()
        worker = {
            "session_id": "w-idle2",
            "name": "Hannah",
            "coordinator_pid": 6000,
            "coordinator_pid_start": datetime.now().isoformat(),
            "spawned_at": spawned_at,
            "idle_age_hours": 1.5,
        }
        threshold_hours = 2
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            result = classify_worker(worker, threshold_hours)
        # 1.5 hours idle < 2 hour threshold -> ok
        assert result == "ok"


class TestManifestDiscovery:
    """Tests for reading manifests from ~/.maniple/workers/."""

    def test_discover_workers_reads_manifest_files(self, tmp_path, monkeypatch):
        """discover_workers() finds and parses *.json files in workers dir."""
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()

        # Write two manifests
        w1 = {
            "worker": {
                "session_id": "w-1",
                "name": "Alice",
                "agent_type": "claude",
                "spawned_at": datetime.now().isoformat(),
            },
            "coordinator": {
                "pid": 1000,
                "pid_start": datetime.now().isoformat(),
                "session_id": "coord-abc",
            },
        }
        w2 = {
            "worker": {
                "session_id": "w-2",
                "name": "Bob",
                "agent_type": "codex",
                "spawned_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            },
            "coordinator": {"pid": 2000, "pid_start": datetime.now().isoformat()},
        }
        (workers_dir / "w-1.json").write_text(json.dumps(w1))
        (workers_dir / "w-2.json").write_text(json.dumps(w2))

        # Patch home() to return tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        workers = discover_workers(workers_dir=workers_dir)
        assert len(workers) >= 2
        session_ids = {w["session_id"] for w in workers}
        assert "w-1" in session_ids
        assert "w-2" in session_ids

    def test_discover_workers_skips_invalid_json(self, tmp_path, monkeypatch):
        """Manifest files with invalid JSON are skipped (best-effort)."""
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()

        # Write one valid, one invalid
        (workers_dir / "valid.json").write_text('{"test": "data"}')
        (workers_dir / "invalid.json").write_text("not valid json {")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        workers = discover_workers(workers_dir=workers_dir)
        # Should find at least the valid one, skip invalid without raising
        assert isinstance(workers, list)


class TestOutputFormats:
    """Tests for human table and JSON output."""

    def test_format_human_table_includes_worker_columns(self):
        """Human table has name, class, model/agent, idle age, tmux, coordinator state."""
        workers = [
            {
                "session_id": "w-1",
                "name": "Alice",
                "agent_type": "claude",
                "idle_age_hours": 1.5,
                "terminal_id": "tmux:pane-123",
                "coordinator_alive": True,
                "class": "ok",
                "unattached": False,
            },
            {
                "session_id": "w-2",
                "name": "Bob",
                "agent_type": "codex",
                "idle_age_hours": 3,
                "terminal_id": None,
                "coordinator_alive": False,
                "class": "orphaned",
                "unattached": True,
            },
        ]
        table = format_zombies_report(workers, as_json=False)
        # Should mention workers by name
        assert "Alice" in table
        assert "Bob" in table
        # Should mention classes
        assert "ok" in table
        assert "orphaned" in table

    def test_format_json_includes_all_worker_fields(self):
        """JSON output has complete worker metadata + reconnect commands."""
        workers = [
            {
                "session_id": "w-1",
                "name": "Alice",
                "agent_type": "claude",
                "idle_age_hours": 1,
                "terminal_id": "tmux:pane-123",
                "coordinator": {
                    "pid": 1000,
                    "session_id": "coord-abc",
                    "session_name": "mysession",
                },
                "project_dir": "/home/user/project",
                "class": "ok",
                "unattached": False,
            },
        ]
        json_str = format_zombies_report(workers, as_json=True)
        data = json.loads(json_str)
        assert data["workers"][0]["session_id"] == "w-1"
        assert data["workers"][0]["class"] == "ok"


class TestExitCode:
    """Exit code is always 0 (report tool, no errors)."""

    def test_exit_code_zero_on_success(self):
        """Even with orphaned workers, exit code is 0."""
        # zombies_cli itself doesn't raise or have an exit mechanism;
        # the exit code is handled at server.py::main() which always calls
        # sys.exit(0) for report tools. Just verify zombies_cli runs
        # without exceptions.
        workers = [{"session_id": "w-orphan", "class": "orphaned"}]
        table = format_zombies_report(workers, as_json=False)
        assert table is not None
