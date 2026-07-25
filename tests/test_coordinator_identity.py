"""
Tests for coordinator_identity.py.

Covers: ppid-chain walk (mocked subprocess), env capture, tmux-pane
derivation, partial/degraded capture, and the never-raises guarantee.
"""

import os

import pytest

from maniple_mcp import coordinator_identity as ci_module
from maniple_mcp.coordinator_identity import (
    CoordinatorIdentity,
    capture_coordinator_identity,
    clear_cache,
    get_coordinator_identity,
)


@pytest.fixture(autouse=True)
def _isolate_cache_and_env(monkeypatch):
    """Every test gets a clean identity cache and a clean, controlled env
    (no leakage from the real TMUX/ITERM/CLAUDE_* env this worker itself
    runs under -- this suite runs inside a real tmux+claude worker, so the
    ambient env WOULD otherwise pollute results)."""
    clear_cache()
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
        "TMUX",
        "TMUX_PANE",
        "ITERM_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    clear_cache()


def _fake_run_factory(responses: dict):
    """Build a fake `_run` replacement. `responses` maps a command-list
    tuple to its canned stdout (or None to simulate failure)."""

    def _fake_run(cmd, timeout=2.0):
        return responses.get(tuple(cmd))

    return _fake_run


class TestCoordinatorIdentityDataclass:
    def test_all_fields_optional_default_none(self):
        identity = CoordinatorIdentity()
        assert identity.pid is None
        assert identity.pid_start is None
        assert identity.session_id is None
        assert identity.project_dir is None
        assert identity.tmux_session_name is None
        assert identity.tmux_window_index is None
        assert identity.tmux_pane_index is None
        assert identity.iterm_session_id is None

    def test_to_dict_contains_all_fields(self):
        identity = CoordinatorIdentity(pid=123, session_id="abc")
        d = identity.to_dict()
        assert d["pid"] == 123
        assert d["session_id"] == "abc"
        assert "tmux_session_name" in d
        assert "iterm_session_id" in d

    def test_tmux_location_combines_session_window_pane(self):
        identity = CoordinatorIdentity(
            tmux_session_name="mysession",
            tmux_window_index="1",
            tmux_pane_index="0",
        )
        assert identity.tmux_location() == "mysession:1.0"

    def test_tmux_location_none_when_incomplete(self):
        identity = CoordinatorIdentity(tmux_session_name="mysession")
        assert identity.tmux_location() is None

    def test_to_env_vars_omits_unknown_fields(self):
        identity = CoordinatorIdentity()
        assert identity.to_env_vars() == {}

    def test_to_env_vars_includes_known_fields(self):
        identity = CoordinatorIdentity(
            pid=999,
            pid_start="Thu Jul 24 10:00:00 2026",
            session_id="sess-1",
            project_dir="/proj",
            tmux_session_name="s",
            tmux_window_index="1",
            tmux_pane_index="2",
            iterm_session_id="w0t0p0:UUID",
        )
        env = identity.to_env_vars()
        assert env["MANIPLE_COORDINATOR_PID"] == "999"
        assert env["MANIPLE_COORDINATOR_PID_START"] == "Thu Jul 24 10:00:00 2026"
        assert env["MANIPLE_COORDINATOR_SESSION_ID"] == "sess-1"
        assert env["MANIPLE_COORDINATOR_PROJECT_DIR"] == "/proj"
        assert env["MANIPLE_COORDINATOR_TMUX"] == "s:1.2"
        assert env["MANIPLE_COORDINATOR_ITERM"] == "w0t0p0:UUID"

    def test_to_env_vars_partial_only_includes_known(self):
        identity = CoordinatorIdentity(pid=42)
        env = identity.to_env_vars()
        assert env == {"MANIPLE_COORDINATOR_PID": "42"}


class TestPpidWalk:
    def test_finds_claude_one_hop(self, monkeypatch):
        """venv install: coordinator MCP server is a direct child of claude."""
        my_pid = os.getpid()
        claude_pid = my_pid + 1
        responses = {
            ("ps", "-o", "ppid=", "-p", str(my_pid)): str(claude_pid),
            ("ps", "-o", "comm=", "-p", str(claude_pid)): "/usr/local/bin/claude",
            ("ps", "-o", "lstart=", "-p", str(claude_pid)): "Thu Jul 24 09:00:00 2026",
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))
        monkeypatch.setattr(os, "getpid", lambda: my_pid)

        identity = capture_coordinator_identity()
        assert identity.pid == claude_pid
        assert identity.pid_start == "Thu Jul 24 09:00:00 2026"

    def test_finds_claude_two_hops_uvx(self, monkeypatch):
        """uvx install inserts one `uv` hop between the MCP server and claude."""
        my_pid = os.getpid()
        uv_pid = my_pid + 1
        claude_pid = my_pid + 2
        responses = {
            ("ps", "-o", "ppid=", "-p", str(my_pid)): str(uv_pid),
            ("ps", "-o", "comm=", "-p", str(uv_pid)): "/usr/local/bin/uv",
            ("ps", "-o", "ppid=", "-p", str(uv_pid)): str(claude_pid),
            ("ps", "-o", "comm=", "-p", str(claude_pid)): "/usr/local/bin/claude",
            ("ps", "-o", "lstart=", "-p", str(claude_pid)): "Thu Jul 24 09:00:00 2026",
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))
        monkeypatch.setattr(os, "getpid", lambda: my_pid)

        identity = capture_coordinator_identity()
        assert identity.pid == claude_pid

    def test_gives_up_after_max_hops(self, monkeypatch):
        """No claude process found within 5 hops -- pid stays None, never raises."""
        my_pid = os.getpid()

        # Build a chain of 6 non-claude hops so the 5-hop cap is exceeded.
        responses = {}
        pid = my_pid
        for hop in range(6):
            next_pid = pid + 1
            responses[("ps", "-o", "ppid=", "-p", str(pid))] = str(next_pid)
            responses[("ps", "-o", "comm=", "-p", str(next_pid))] = "/usr/bin/some-shell"
            pid = next_pid

        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))
        monkeypatch.setattr(os, "getpid", lambda: my_pid)

        identity = capture_coordinator_identity()
        assert identity.pid is None
        assert identity.pid_start is None

    def test_stops_at_pid_1(self, monkeypatch):
        my_pid = os.getpid()
        responses = {
            ("ps", "-o", "ppid=", "-p", str(my_pid)): "1",
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))
        monkeypatch.setattr(os, "getpid", lambda: my_pid)

        identity = capture_coordinator_identity()
        assert identity.pid is None

    def test_ps_failure_degrades_gracefully(self, monkeypatch):
        """ps returning nothing (e.g. command failed) never raises."""
        monkeypatch.setattr(ci_module, "_run", lambda cmd, timeout=2.0: None)

        identity = capture_coordinator_identity()
        assert identity.pid is None

    def test_ps_raises_never_propagates(self, monkeypatch):
        """Even if the subprocess helper itself raises, capture must not raise."""

        def _boom(cmd, timeout=2.0):
            raise OSError("ps not found")

        monkeypatch.setattr(ci_module, "_run", _boom)

        identity = capture_coordinator_identity()
        assert identity.pid is None


class TestEnvCapture:
    def test_captures_all_known_env_vars(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-123")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/my/project")
        monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:UUID-HERE")
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setattr(ci_module, "_run", lambda cmd, timeout=2.0: None)

        identity = capture_coordinator_identity()
        assert identity.session_id == "sess-123"
        assert identity.project_dir == "/my/project"
        assert identity.iterm_session_id == "w0t0p0:UUID-HERE"

    def test_missing_claude_code_session_id_is_none_not_error(self, monkeypatch):
        """CLAUDE_CODE_SESSION_ID is version-dependent -- absence is fine."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setattr(ci_module, "_run", lambda cmd, timeout=2.0: None)

        identity = capture_coordinator_identity()
        assert identity.session_id is None

    def test_empty_string_env_vars_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "")
        monkeypatch.setattr(ci_module, "_run", lambda cmd, timeout=2.0: None)

        identity = capture_coordinator_identity()
        assert identity.project_dir is None


class TestTmuxDerivation:
    def test_derives_session_window_pane_from_tmux_pane(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%12")
        responses = {
            ("tmux", "display", "-p", "-t", "%12", "-F", "#{session_name}\t#{window_index}\t#{pane_index}"): (
                "mysession\t1\t0"
            ),
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))

        identity = capture_coordinator_identity()
        assert identity.tmux_session_name == "mysession"
        assert identity.tmux_window_index == "1"
        assert identity.tmux_pane_index == "0"
        assert identity.tmux_pane_env == "%12"

    def test_tmux_display_failure_degrades_gracefully(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%99")
        monkeypatch.setattr(ci_module, "_run", lambda cmd, timeout=2.0: None)

        identity = capture_coordinator_identity()
        assert identity.tmux_session_name is None
        assert identity.tmux_window_index is None
        assert identity.tmux_pane_index is None
        # Raw env var is still captured even though derivation failed.
        assert identity.tmux_pane_env == "%99"

    def test_tmux_display_malformed_output_degrades_gracefully(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%1")
        responses = {
            ("tmux", "display", "-p", "-t", "%1", "-F", "#{session_name}\t#{window_index}\t#{pane_index}"): (
                "not-enough-fields"
            ),
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))

        identity = capture_coordinator_identity()
        assert identity.tmux_session_name is None

    def test_no_tmux_pane_skips_derivation(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        calls = []

        def _tracking_run(cmd, timeout=2.0):
            calls.append(cmd)
            return None

        monkeypatch.setattr(ci_module, "_run", _tracking_run)

        identity = capture_coordinator_identity()
        assert identity.tmux_session_name is None
        assert not any(cmd[0] == "tmux" for cmd in calls)

    def test_emoji_and_spaces_in_tmux_session_name_survive(self, monkeypatch):
        """Real tmux session names in this environment look like
        '⚙ mac-perf--🔄 working-maniple-0724-1019' -- must round-trip intact."""
        monkeypatch.setenv("TMUX_PANE", "%5")
        session_name = "⚙ mac-perf--🔄 working-maniple-0724-1019"
        responses = {
            ("tmux", "display", "-p", "-t", "%5", "-F", "#{session_name}\t#{window_index}\t#{pane_index}"): (
                f"{session_name}\t2\t1"
            ),
        }
        monkeypatch.setattr(ci_module, "_run", _fake_run_factory(responses))

        identity = capture_coordinator_identity()
        assert identity.tmux_session_name == session_name
        assert identity.tmux_location() == f"{session_name}:2.1"


class TestNeverRaises:
    def test_capture_never_raises_on_total_subprocess_failure(self, monkeypatch):
        def _boom(cmd, timeout=2.0):
            raise RuntimeError("boom")

        monkeypatch.setattr(ci_module, "_run", _boom)
        monkeypatch.setenv("TMUX_PANE", "%1")

        # Must not raise -- degrades to a fully-partial identity.
        identity = capture_coordinator_identity()
        assert isinstance(identity, CoordinatorIdentity)
        assert identity.pid is None
        assert identity.tmux_session_name is None

    def test_capture_never_raises_when_os_getpid_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("no pid for you")

        monkeypatch.setattr(os, "getpid", _boom)
        identity = capture_coordinator_identity()
        assert identity.pid is None


class TestCaching:
    def test_get_coordinator_identity_caches(self, monkeypatch):
        call_count = 0

        def _fake_capture():
            nonlocal call_count
            call_count += 1
            return CoordinatorIdentity(pid=call_count)

        monkeypatch.setattr(ci_module, "capture_coordinator_identity", _fake_capture)

        first = get_coordinator_identity()
        second = get_coordinator_identity()
        assert first is second
        assert call_count == 1

    def test_force_refresh_recaptures(self, monkeypatch):
        call_count = 0

        def _fake_capture():
            nonlocal call_count
            call_count += 1
            return CoordinatorIdentity(pid=call_count)

        monkeypatch.setattr(ci_module, "capture_coordinator_identity", _fake_capture)

        first = get_coordinator_identity()
        second = get_coordinator_identity(force_refresh=True)
        assert first is not second
        assert call_count == 2

    def test_clear_cache_forces_recapture(self, monkeypatch):
        call_count = 0

        def _fake_capture():
            nonlocal call_count
            call_count += 1
            return CoordinatorIdentity(pid=call_count)

        monkeypatch.setattr(ci_module, "capture_coordinator_identity", _fake_capture)

        get_coordinator_identity()
        clear_cache()
        get_coordinator_identity()
        assert call_count == 2
