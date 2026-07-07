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


def _run_hook(
    hook_script: Path,
    tool_name: str,
    *,
    state_file: object,
    threshold: float = 0.75,
    max_stale_seconds: int = 600,
) -> subprocess.CompletedProcess:
    payload = {"tool_name": tool_name}
    return subprocess.run(
        [sys.executable, str(hook_script), str(threshold), str(state_file), str(max_stale_seconds)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestAllowlist:
    @pytest.mark.parametrize("tool_name", sorted(ALLOWLISTED_TOOLS))
    def test_allowlisted_tool_allowed_even_over_threshold(self, hook_script, tmp_path, tool_name):
        state_file = _write_state_file(tmp_path, _rate_limits_payload(95.0))
        result = _run_hook(hook_script, tool_name, state_file=state_file)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_allowlisted_tool_name_not_confused(self):
        assert "Bash" not in ALLOWLISTED_TOOLS


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

    def test_resets_at_garbage_still_denies_without_crashing(self, hook_script, tmp_path):
        state_file = _write_state_file(
            tmp_path, _rate_limits_payload(90.0, resets_at="not-a-number")
        )
        result = _run_hook(hook_script, "Bash", state_file=state_file, threshold=0.75)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_resets_at_out_of_range_still_denies_without_crashing(self, hook_script, tmp_path):
        state_file = _write_state_file(
            tmp_path, _rate_limits_payload(90.0, resets_at=1e20)
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
