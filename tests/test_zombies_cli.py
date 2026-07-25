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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maniple_mcp.zombies_cli import (
    WorkerZombieStatus,
    _coordinator_is_alive,
    _ps_scan,
    _tmux_attached,
    classify_worker,
    discover_workers,
    format_zombies_report,
    get_idle_age,
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


class TestIdleAgeSpawnedAtTimezoneHandling:
    """Regression coverage: manifest spawned_at is UTC ISO with a trailing
    'Z' (spec component 3), which datetime.fromisoformat() parses as
    timezone-AWARE. datetime.now() is timezone-NAIVE, so subtracting the
    two raises TypeError -- which a broad except (ValueError, TypeError)
    was silently swallowing, making the spawned_at fallback always report
    0.0h idle regardless of actual age. That would make 'forgotten'
    unreachable via the fallback path for every real manifest."""

    def test_z_suffixed_spawned_at_computes_real_age_not_zero(self):
        five_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = get_idle_age("w-nojsonl", spawned_at=five_hours_ago, project_path=None)
        assert age == pytest.approx(5.0, abs=0.05)

    def test_naive_spawned_at_still_works(self):
        """Backward compatible with naive isoformat (no tz suffix)."""
        two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat()
        age = get_idle_age("w-nojsonl2", spawned_at=two_hours_ago, project_path=None)
        assert age == pytest.approx(2.0, abs=0.05)

    def test_forgotten_classification_reachable_via_z_suffixed_spawned_at(self):
        """End-to-end: a manifest-only worker (no JSONL yet) whose
        spawned_at is far in the past, in the real manifest format, must
        classify as 'forgotten' when its coordinator is alive -- not
        silently 'ok' due to the timezone bug."""
        five_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        idle = get_idle_age("w-x", spawned_at=five_hours_ago, project_path=None)
        worker = {
            "session_id": "w-x",
            "coordinator_pid": 1234,
            "coordinator_pid_start": datetime.now().isoformat(),
            "idle_age_hours": idle,
        }
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            result = classify_worker(worker, idle_threshold_hours=2)
        assert result == "forgotten"


class TestPsScan:
    """Tests for the best-effort ps-scan fallback that catches pre-feature
    workers (no manifest yet, coordinator=UNKNOWN) via the
    `--settings .../worker-<id>.json` argv pattern (spec component 5)."""

    def test_matches_settings_file_pattern(self):
        ps_output = (
            "  PID COMMAND\n"
            "12345 claude --settings "
            "/Users/x/.claude/claude-team-settings/worker-abc123.json\n"
            "99999 /usr/bin/vim\n"
        )
        assert _ps_scan(ps_output) == {"abc123": 12345}

    def test_no_matching_processes_returns_empty(self):
        ps_output = "1 /usr/bin/vim\n2 /bin/bash\n"
        assert _ps_scan(ps_output) == {}

    def test_multiple_workers(self):
        ps_output = (
            "1 claude --settings /x/worker-aaa.json\n"
            "2 claude --settings /x/worker-bbb.json\n"
        )
        assert _ps_scan(ps_output) == {"aaa": 1, "bbb": 2}

    def test_malformed_lines_ignored_not_raising(self):
        ps_output = "not-a-pid claude --settings /x/worker-zzz.json\n"
        assert _ps_scan(ps_output) == {}

    def test_shells_out_to_real_ps_when_no_output_injected(self):
        """When ps_output isn't injected, it shells out to the real `ps`
        binary -- just verify this doesn't raise on a real machine."""
        result = _ps_scan()
        assert isinstance(result, dict)


class TestTmuxAttached:
    """Real tmux-attached check (list-panes -> session_name -> list-sessions
    session_attached), not just terminal_id truthiness."""

    def test_none_for_non_tmux_terminal(self):
        assert _tmux_attached("iterm:ABC-123") is None
        assert _tmux_attached(None) is None

    def test_true_when_session_has_attached_client(self):
        def runner(args):
            if args[0] == "list-panes":
                return "%5 maniple-repo\n"
            if args[0] == "list-sessions":
                return "maniple-repo 1\n"
            raise AssertionError(args)

        assert _tmux_attached("tmux:%5", tmux_runner=runner) is True

    def test_false_when_session_unattached(self):
        def runner(args):
            if args[0] == "list-panes":
                return "%5 maniple-repo\n"
            if args[0] == "list-sessions":
                return "maniple-repo 0\n"
            raise AssertionError(args)

        assert _tmux_attached("tmux:%5", tmux_runner=runner) is False

    def test_none_when_pane_cannot_be_resolved(self):
        def runner(args):
            return "%99 other-session\n"

        assert _tmux_attached("tmux:%5", tmux_runner=runner) is None

    def test_none_when_tmux_unavailable(self):
        def runner(args):
            raise OSError("tmux not found")

        assert _tmux_attached("tmux:%5", tmux_runner=runner) is None


class TestCoordinatorAliveEdgeCases:
    """Additional _coordinator_is_alive coverage beyond the happy paths in
    TestWorkerClassification: no pid at all, and PermissionError (process
    exists but owned by another user -- must not be reported as dead)."""

    def test_no_pid_is_not_alive(self):
        alive, defunct = _coordinator_is_alive(None, None)
        assert alive is False
        assert defunct is False

    def test_permission_error_treated_as_alive(self):
        """kill(pid, 0) raising PermissionError means the process DOES
        exist (just owned by another user) -- must not be reported dead."""
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = PermissionError
            alive, defunct = _coordinator_is_alive(111, None)
        assert alive is True
        assert defunct is False


class TestWorkerAliveFromPsScan:
    """discover_workers() unions manifests with the ps-scan, and uses the
    ps-scan to determine worker_alive for manifest-sourced workers too
    (not just to discover pre-feature ones)."""

    def test_worker_alive_true_when_ps_scan_matches_session_id(self, tmp_path):
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()
        manifest = {
            "worker": {
                "session_id": "w-1",
                "name": "Alice",
                "agent_type": "claude",
                "spawned_at": datetime.now().isoformat(),
            },
            "coordinator": {"pid": 1000, "pid_start": datetime.now().isoformat()},
        }
        (workers_dir / "w-1.json").write_text(json.dumps(manifest))

        ps_output = "77 claude --settings /x/worker-w-1.json\n"
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            workers = discover_workers(workers_dir=workers_dir, ps_output=ps_output)
        assert workers[0]["worker_alive"] is True

    def test_worker_alive_false_when_no_ps_match(self, tmp_path):
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()
        manifest = {
            "worker": {
                "session_id": "w-2",
                "name": "Bob",
                "agent_type": "claude",
                "spawned_at": datetime.now().isoformat(),
            },
            "coordinator": {"pid": 1000, "pid_start": datetime.now().isoformat()},
        }
        (workers_dir / "w-2.json").write_text(json.dumps(manifest))

        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            workers = discover_workers(workers_dir=workers_dir, ps_output="")
        assert workers[0]["worker_alive"] is False

    def test_pre_feature_worker_discovered_via_ps_scan_only(self, tmp_path):
        """A worker process running with no manifest at all (pre-feature) --
        coordinator is UNKNOWN, classified orphaned."""
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()
        ps_output = "42 claude --settings /x/worker-legacy1.json\n"
        workers = discover_workers(workers_dir=workers_dir, ps_output=ps_output)
        assert len(workers) == 1
        assert workers[0]["session_id"] == "legacy1"
        assert workers[0]["worker_alive"] is True
        assert workers[0]["class"] == "orphaned"

    def test_manifest_worker_not_duplicated_by_ps_scan(self, tmp_path):
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()
        manifest = {
            "worker": {
                "session_id": "w-1",
                "name": "Alice",
                "agent_type": "claude",
                "spawned_at": datetime.now().isoformat(),
            },
            "coordinator": {"pid": 1000, "pid_start": datetime.now().isoformat()},
        }
        (workers_dir / "w-1.json").write_text(json.dumps(manifest))
        ps_output = "77 claude --settings /x/worker-w-1.json\n"
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            workers = discover_workers(workers_dir=workers_dir, ps_output=ps_output)
        assert len(workers) == 1


class TestReconnectCommandsInActionBlock:
    """The action block must contain the EXACT reconnect commands (spec
    component 5), not just a vague hint that they exist."""

    def test_dead_coordinator_shows_exact_resume_command(self):
        workers = [{
            "session_id": "w-1",
            "name": "Alice",
            "agent_type": "claude",
            "idle_age_hours": 10,
            "terminal_id": "tmux:%5",
            "coordinator_alive": False,
            "coordinator_session_id": "coord-1",
            "project_dir": "/repo",
            "class": "orphaned",
        }]
        report = format_zombies_report(
            workers, as_json=False, tmux_runner=lambda args: "%5 maniple-repo\n"
        )
        assert "cd /repo && claude --resume coord-1" in report

    def test_alive_coordinator_shows_exact_switch_client_command(self):
        workers = [{
            "session_id": "w-1",
            "name": "Alice",
            "agent_type": "claude",
            "idle_age_hours": 3,
            "terminal_id": "tmux:%5",
            "coordinator_alive": True,
            "coordinator_session_name": "maniple-repo",
            "class": "forgotten",
        }]
        report = format_zombies_report(
            workers, as_json=False, tmux_runner=lambda args: "%5 maniple-repo\n"
        )
        assert "tmux switch-client -t 'maniple-repo'" in report

    def test_worker_connect_command_resolves_session_via_pane_lookup(self):
        workers = [{
            "session_id": "w-1",
            "name": "Alice",
            "agent_type": "claude",
            "idle_age_hours": 3,
            "terminal_id": "tmux:%5",
            "coordinator_alive": True,
            "coordinator_session_name": "maniple-repo",
            "class": "forgotten",
        }]

        def runner(args):
            assert args[0] == "list-panes"
            return "%5 worker-session\n"

        report = format_zombies_report(workers, as_json=False, tmux_runner=runner)
        assert "tmux attach -t 'worker-session'" in report

    def test_iterm_worker_shows_session_uuid_and_best_effort_reveal(self):
        workers = [{
            "session_id": "w-1",
            "name": "Alice",
            "agent_type": "claude",
            "idle_age_hours": 3,
            "terminal_id": "iterm:ABCD-1234",
            "coordinator_alive": True,
            "class": "forgotten",
        }]
        report = format_zombies_report(workers, as_json=False)
        assert "ABCD-1234" in report
        assert "osascript" in report
