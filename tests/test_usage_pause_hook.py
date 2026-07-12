"""Tests for the self-contained usage-pause PreToolUse hook script.

Sibling to test_context_pause_hook.py: the script (rendered by
`maniple_mcp.usage_pause_hook.render_hook_script()`) is stdlib-only Python,
written to disk and invoked by Claude Code as a subprocess -- it must run
standalone, without importing maniple_mcp. These tests write the rendered
script and a fake statusline cache file to `tmp_path` and invoke it via
subprocess, exactly as Claude Code would.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from maniple_mcp.usage_pause_hook import ALLOWLISTED_TOOLS, render_hook_script


@pytest.fixture
def hook_script(tmp_path: Path) -> Path:
    """Write the rendered hook script to a temp file and return its path."""
    path = tmp_path / "usage_pause_hook.py"
    path.write_text(render_hook_script())
    return path


def _write_state_file(tmp_path: Path, data: dict, *, name: str = "state.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _rate_limits_payload(five_hour_percent: float, *, resets_at=None, seven_day_percent=None) -> dict:
    five_hour = {"used_percentage": five_hour_percent}
    if resets_at is not None:
        five_hour["resets_at"] = resets_at
    rate_limits = {"five_hour": five_hour}
    if seven_day_percent is not None:
        rate_limits["seven_day"] = {"used_percentage": seven_day_percent}
    return {"rate_limits": rate_limits}


# Default scope/override_dir for tests that don't care about the override
# ladder -- a nonexistent dir means "no override file", identical to the
# pre-ladder behavior these constants were introduced to preserve.
_DEFAULT_SCOPE = "worker-test"
_DEFAULT_OVERRIDE_DIR = "/nonexistent-maniple-override-dir-xyz"


def _run_hook(
    hook_script: Path,
    tool_name: str,
    *,
    state_file: object,
    threshold: float = 0.75,
    max_stale_seconds: int = 600,
    scope: str = _DEFAULT_SCOPE,
    override_dir: object = _DEFAULT_OVERRIDE_DIR,
    tool_input: dict | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    payload = {"tool_name": tool_name}
    if tool_input is not None:
        payload["tool_input"] = tool_input
    # Always scrub MANIPLE_WORKER from the inherited environment so tests
    # are deterministic regardless of the outer process's env; callers that
    # want it set pass env={"MANIPLE_WORKER": "1"} explicitly.
    run_env = {k: v for k, v in os.environ.items() if k != "MANIPLE_WORKER"}
    if env is not None:
        run_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            str(hook_script),
            str(threshold),
            str(state_file),
            str(max_stale_seconds),
            str(scope),
            str(override_dir),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=run_env,
    )


def _write_override(
    override_dir: Path, scope: str, *, threshold: float | None, expires_at: float
) -> Path:
    override_dir.mkdir(parents=True, exist_ok=True)
    path = override_dir / f"{scope}.json"
    path.write_text(json.dumps({"threshold": threshold, "expires_at": expires_at}))
    return path


class TestAllowlist:
    @pytest.mark.parametrize("tool_name", sorted(ALLOWLISTED_TOOLS))
    def test_allowlisted_tool_allowed_even_over_threshold(self, hook_script, tmp_path, tool_name):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(95.0))
        result = _run_hook(hook_script, tool_name, state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_allowlisted_tool_name_not_confused(self):
        assert "Bash" not in ALLOWLISTED_TOOLS

    @pytest.mark.parametrize(
        "tool_name", ["ScheduleWakeup", "CronCreate", "CronList", "CronDelete"]
    )
    def test_scheduling_tools_allowed_at_any_usage(self, hook_script, tmp_path, tool_name):
        """The 5-hour usage window resets, so a paused session must always be
        able to schedule its own continuation -- even at 100% usage with the
        highest override rung active."""
        assert tool_name in ALLOWLISTED_TOOLS
        state_file = _write_state_file(tmp_path, _rate_limits_payload(100.0))
        override_dir = tmp_path / "override"
        _write_override(
            override_dir, _DEFAULT_SCOPE, threshold=0.95, expires_at=time.time() + 3600
        )
        result = _run_hook(
            hook_script,
            tool_name,
            state_file=state_file,
            threshold=0.95,
            override_dir=override_dir,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestThresholdEnforcement:
    def test_over_threshold_denies_with_percent_and_5hour_in_reason(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        hook_output = output["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "PreToolUse"
        assert hook_output["permissionDecision"] == "deny"
        reason = hook_output["permissionDecisionReason"]
        assert "90%" in reason
        assert "5-hour" in reason

    def test_under_threshold_allows(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(10.0))
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_exactly_at_threshold_denies(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(75.0))
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_seven_day_high_but_five_hour_low_allows(self, hook_script, tmp_path):
        """seven_day is ignored entirely -- only five_hour matters."""
        state_file = _write_state_file(
            tmp_path, _rate_limits_payload(10.0, seven_day_percent=99.0)
        )
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestResetsAt:
    def test_resets_at_present_reason_contains_a_time(self, hook_script, tmp_path):
        # A fixed, known epoch so we can assert on a specific HH:MM.
        resets_at = 1735732800  # 2025-01-01 08:00:00 UTC
        state_file = _write_state_file(
            tmp_path, _rate_limits_payload(90.0, resets_at=resets_at)
        )
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        output = json.loads(result.stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert any(f"{h:02d}:" in reason for h in range(24))

    @pytest.mark.parametrize(
        "resets_at",
        ["not-a-number", 1e20],
        ids=["garbage", "out_of_range"],
    )
    def test_bad_resets_at_still_denies_without_crashing(self, hook_script, tmp_path, resets_at):
        state_file = _write_state_file(
            tmp_path, _rate_limits_payload(90.0, resets_at=resets_at)
        )
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_no_resets_at_still_denies(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestFailOpen:
    """The hook must never break a worker -- any error/stale/missing data allows the tool call."""

    def test_missing_state_file_fails_open(self, hook_script, tmp_path):
        missing = tmp_path / "does-not-exist.json"
        result = _run_hook(hook_script, "Bash", state_file=missing)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_stale_mtime_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(95.0))
        old_time = time.time() - 700  # older than default max_stale_seconds=600
        os.utime(state_file, (old_time, old_time))
        result = _run_hook(hook_script, "Bash", state_file=state_file, max_stale_seconds=600)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_fresh_mtime_within_max_stale_still_works(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(95.0))
        recent_time = time.time() - 5  # well within default max_stale_seconds=600
        os.utime(state_file, (recent_time, recent_time))
        result = _run_hook(hook_script, "Bash", state_file=state_file, max_stale_seconds=600)
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_malformed_json_fails_open(self, hook_script, tmp_path):
        state_file = tmp_path / "bad.json"
        state_file.write_text("not json {")
        result = _run_hook(hook_script, "Bash", state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_rate_limits_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, {"foo": "bar"})
        result = _run_hook(hook_script, "Bash", state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_five_hour_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, {"rate_limits": {}})
        result = _run_hook(hook_script, "Bash", state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_used_percentage_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, {"rate_limits": {"five_hour": {}}})
        result = _run_hook(hook_script, "Bash", state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_numeric_used_percentage_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(
            tmp_path, {"rate_limits": {"five_hour": {"used_percentage": "lots"}}}
        )
        result = _run_hook(hook_script, "Bash", state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_stdin_json_fails_open(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(95.0))
        result = subprocess.run(
            [sys.executable, str(hook_script), "0.75", str(state_file), "600"],
            input="not valid json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_argv_fails_open(self, hook_script, tmp_path):
        payload = {"tool_name": "Bash"}
        result = subprocess.run(
            [sys.executable, str(hook_script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestOverrideLadder:
    """Escalating override ladder: effective_threshold = max(base, override
    rung). Rungs: base -> 0.90 -> 0.95 -> unlimited (threshold=null).
    Overrides expire at expires_at; expired/malformed overrides fall back
    to base."""

    def test_090_override_allows_at_85_percent(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w1", threshold=0.90, expires_at=time.time() + 3600)
        state_file = _write_state_file(tmp_path, _rate_limits_payload(85.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_090_override_denies_at_91_percent(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w1", threshold=0.90, expires_at=time.time() + 3600)
        state_file = _write_state_file(tmp_path, _rate_limits_payload(91.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_095_override_allows_at_92_percent_denies_at_96(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w1", threshold=0.95, expires_at=time.time() + 3600)

        allow_state = _write_state_file(tmp_path, _rate_limits_payload(92.0), name="allow.json")
        allow_result = _run_hook(
            hook_script, "Bash", state_file=allow_state, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        assert allow_result.stdout.strip() == ""

        deny_state = _write_state_file(tmp_path, _rate_limits_payload(96.0), name="deny.json")
        deny_result = _run_hook(
            hook_script, "Bash", state_file=deny_state, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        deny_output = json.loads(deny_result.stdout)
        assert deny_output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_null_rung_is_unlimited_allows_at_99_percent(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w1", threshold=None, expires_at=time.time() + 3600)
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_expired_override_falls_back_to_base(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w1", threshold=None, expires_at=time.time() - 10)
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_malformed_override_falls_back_to_base(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "w1.json").write_text("not valid json {")
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_missing_override_falls_back_to_base(self, hook_script, tmp_path):
        override_dir = tmp_path / "overrides"  # never created
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_override_scoped_to_different_worker_does_not_apply(self, hook_script, tmp_path):
        """An override file for a different scope must not leak."""
        override_dir = tmp_path / "overrides"
        _write_override(override_dir, "w2", threshold=None, expires_at=time.time() + 3600)
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_deny_reason_mentions_worker_continue_hint(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=tmp_path / "overrides",
        )
        output = json.loads(result.stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert "override_usage_pause" in reason

    def test_deny_reason_mentions_global_cli_hint(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(90.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="global", override_dir=tmp_path / "overrides",
        )
        output = json.loads(result.stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert "maniple usage-override" in reason


class TestGlobalScopeManipleWorker:
    """A globally-installed hook must no-op inside maniple workers (which
    carry their own scoped hook already) -- but only for scope=='global'."""

    def test_global_scope_with_maniple_worker_env_allows_even_over_threshold(
        self, hook_script, tmp_path
    ):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="global", override_dir=tmp_path / "overrides",
            env={"MANIPLE_WORKER": "1"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_worker_scope_ignores_maniple_worker_env(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=tmp_path / "overrides",
            env={"MANIPLE_WORKER": "1"},
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_global_scope_without_maniple_worker_env_still_denies(self, hook_script, tmp_path):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="global", override_dir=tmp_path / "overrides",
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestAntiLoophole:
    """A session must not be able to grant itself an override by writing
    directly into the override directory -- checked BEFORE the normal
    allowlist (which would otherwise let Write/Edit through)."""

    @pytest.mark.parametrize(
        "tool_name, field, filename",
        [
            ("Write", "file_path", "w1.json"),
            ("Edit", "file_path", "w1.json"),
            ("NotebookEdit", "notebook_path", "w1.ipynb"),
        ],
    )
    def test_write_capable_tool_into_override_dir_denied(
        self, hook_script, tmp_path, tool_name, field, filename
    ):
        override_dir = tmp_path / "overrides"
        override_dir.mkdir()
        state_file = _write_state_file(tmp_path, _rate_limits_payload(1.0))  # far under threshold
        target = override_dir / filename
        result = _run_hook(
            hook_script, tool_name, state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
            tool_input={field: str(target)},
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_write_elsewhere_allowed_over_threshold(self, hook_script, tmp_path):
        """Write is normally allowlisted -- only writes INTO override_dir
        are specially denied."""
        override_dir = tmp_path / "overrides"
        override_dir.mkdir()
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        elsewhere = tmp_path / "handoff.md"
        result = _run_hook(
            hook_script, "Write", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
            tool_input={"file_path": str(elsewhere)},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_write_with_missing_file_path_fails_open_on_loophole_check(
        self, hook_script, tmp_path
    ):
        """No file_path field -- the anti-loophole check can't evaluate, so
        it's skipped (falls through to the normal allowlist, which allows
        Write)."""
        override_dir = tmp_path / "overrides"
        override_dir.mkdir()
        state_file = _write_state_file(tmp_path, _rate_limits_payload(99.0))
        result = _run_hook(
            hook_script, "Write", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
            tool_input={},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_bash_tool_into_override_dir_not_treated_as_loophole(self, hook_script, tmp_path):
        """The anti-loophole check only applies to the specific
        write-capable tools -- Bash isn't one of them (it's covered by the
        normal threshold check instead)."""
        override_dir = tmp_path / "overrides"
        override_dir.mkdir()
        state_file = _write_state_file(tmp_path, _rate_limits_payload(1.0))
        result = _run_hook(
            hook_script, "Bash", state_file=state_file, threshold=0.75,
            scope="w1", override_dir=override_dir,
            tool_input={"command": f"echo hi > {override_dir}/w1.json"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
