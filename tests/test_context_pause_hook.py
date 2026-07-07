"""Tests for the self-contained context-pause PreToolUse hook script.

The script (rendered by `maniple_mcp.context_pause_hook.render_hook_script()`)
is stdlib-only Python, written to disk and invoked by Claude Code as a
subprocess -- it must run standalone, without importing maniple_mcp. These
tests write the rendered script to `tmp_path` and invoke it via subprocess,
exactly as Claude Code would, so they never touch the real
~/.claude/claude-team-settings/ directory.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from maniple_mcp.context_pause_hook import ALLOWLISTED_TOOLS, render_hook_script


@pytest.fixture
def hook_script(tmp_path: Path) -> Path:
    """Write the rendered hook script to a temp file and return its path."""
    path = tmp_path / "context_pause_hook.py"
    path.write_text(render_hook_script())
    return path


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n")
    return path


def _assistant_entry(usage: dict, *, is_sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "message": {"role": "assistant", "usage": usage},
    }


def _run_hook(
    hook_script: Path,
    transcript_path: object,
    tool_name: str,
    threshold: float = 0.75,
    window_tokens: int = 200000,
) -> subprocess.CompletedProcess:
    payload = {"transcript_path": str(transcript_path), "tool_name": tool_name}
    return subprocess.run(
        [sys.executable, str(hook_script), str(threshold), str(window_tokens)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestAllowlist:
    """Allowlisted tools always pass, even over threshold."""

    @pytest.mark.parametrize("tool_name", sorted(ALLOWLISTED_TOOLS))
    def test_allowlisted_tool_allowed_even_over_threshold(
        self, hook_script, tmp_path, tool_name
    ):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 190000})]
        )
        result = _run_hook(hook_script, transcript, tool_name, threshold=0.75, window_tokens=200000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_allowlisted_tool_name_not_confused(self, hook_script, tmp_path):
        """Sanity check: an unrelated tool name is not accidentally allowlisted."""
        assert "Bash" not in ALLOWLISTED_TOOLS


class TestThresholdEnforcement:
    def test_over_threshold_denies_non_allowlisted_tool(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 190000})]
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        hook_output = output["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "PreToolUse"
        assert hook_output["permissionDecision"] == "deny"
        assert "95%" in hook_output["permissionDecisionReason"]

    def test_under_threshold_allows(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 1000})]
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_usage_computed_from_input_cache_read_and_cache_creation(
        self, hook_script, tmp_path
    ):
        # 50000 + 50000 + 50001 = 150001 / 200000 = 75.0005% >= 75% threshold.
        transcript = _write_transcript(
            tmp_path,
            [
                _assistant_entry(
                    {
                        "input_tokens": 50000,
                        "cache_read_input_tokens": 50000,
                        "cache_creation_input_tokens": 50001,
                    }
                )
            ],
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_exactly_at_threshold_denies(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 150000})]
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestSidechainHandling:
    def test_sidechain_entries_skipped(self, hook_script, tmp_path):
        """A later sidechain entry with high usage must not trigger a deny;
        the last MAIN-chain assistant usage (low) should be used instead."""
        transcript = _write_transcript(
            tmp_path,
            [
                _assistant_entry({"input_tokens": 1000}),
                _assistant_entry({"input_tokens": 199000}, is_sidechain=True),
            ],
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_last_main_chain_entry_used(self, hook_script, tmp_path):
        """Usage should reflect the LAST main-chain assistant message, not the first."""
        transcript = _write_transcript(
            tmp_path,
            [
                _assistant_entry({"input_tokens": 190000}),
                _assistant_entry({"input_tokens": 1000}),
            ],
        )
        result = _run_hook(hook_script, transcript, "Bash", threshold=0.75, window_tokens=200000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestFailOpen:
    """The hook must never break a worker -- any error allows the tool call."""

    def test_missing_transcript_fails_open(self, hook_script, tmp_path):
        missing = tmp_path / "does-not-exist.jsonl"
        result = _run_hook(hook_script, missing, "Bash")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_transcript_fails_open(self, hook_script, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text("not json\n{also not json ]\n")
        result = _run_hook(hook_script, path, "Bash")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_usage_field_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path,
            [{"type": "assistant", "isSidechain": False, "message": {"role": "assistant"}}],
        )
        result = _run_hook(hook_script, transcript, "Bash")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_no_assistant_messages_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [{"type": "user", "message": {"role": "user", "content": "hi"}}]
        )
        result = _run_hook(hook_script, transcript, "Bash")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_stdin_json_fails_open(self, hook_script, tmp_path):
        result = subprocess.run(
            [sys.executable, str(hook_script), "0.75", "200000"],
            input="not valid json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_argv_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 190000})]
        )
        payload = {"transcript_path": str(transcript), "tool_name": "Bash"}
        result = subprocess.run(
            [sys.executable, str(hook_script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_tool_name_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 190000})]
        )
        payload = {"transcript_path": str(transcript)}
        result = subprocess.run(
            [sys.executable, str(hook_script), "0.75", "200000"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
