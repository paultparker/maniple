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


def _assistant_entry(
    usage: dict, *, is_sidechain: bool = False, model: str | None = None
) -> dict:
    message = {"role": "assistant", "usage": usage}
    if model is not None:
        message["model"] = model
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "message": message,
    }


def _filler_entry(size: int) -> dict:
    """A large, non-usage-bearing entry used to pad transcript size."""
    return {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": "x" * size},
    }


# Comfortably larger than the hook's ~1MB tail-read window, so padding with
# these lines forces a real seek-past-the-front-of-the-file scenario.
_TAIL_READ_BYTES = 1_000_000


def _run_hook(
    hook_script: Path,
    transcript_path: object,
    tool_name: str,
    threshold: float = 0.75,
    window_tokens: int = 200000,
    max_tokens: int = 250000,
    large_window_tokens: int = 300000,
) -> subprocess.CompletedProcess:
    payload = {"transcript_path": str(transcript_path), "tool_name": tool_name}
    return subprocess.run(
        [
            sys.executable,
            str(hook_script),
            str(threshold),
            str(window_tokens),
            str(max_tokens),
            str(large_window_tokens),
        ],
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


class TestMaxTokensCap:
    """The effective limit is a step function of the (Haiku-adjusted)
    effective window, not a flat threshold fraction of it:

    - window >= large_window_tokens ("large") -> flat max_tokens cap;
      threshold does not apply at all in this regime.
    - window < large_window_tokens ("small") -> threshold * window controls.

    This means large windows always pause at exactly max_tokens (not some
    fraction of the window), while small windows (e.g. Haiku's real 200K)
    keep the old threshold-fraction behavior."""

    def test_large_window_allows_just_under_cap(self, hook_script, tmp_path):
        # 1M window (>= 300K large_window_tokens) -> flat 250K cap, not the
        # raw 0.75 * 1M = 750K the threshold fraction would otherwise allow.
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 249_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_large_window_denies_at_cap(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 250_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_small_window_still_bound_by_threshold_fraction_under_cap(
        self, hook_script, tmp_path
    ):
        # 200K window (< 300K large_window_tokens) -> threshold fraction
        # (150K) controls, well under the 250K cap.
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 149_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=200_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 150_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=200_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_window_at_large_boundary_uses_flat_cap_not_fraction(
        self, hook_script, tmp_path
    ):
        """A window exactly AT large_window_tokens (300K) uses the flat
        250K cap, NOT 0.75 * 300K = 225K -- the boundary is inclusive and
        the flat-cap regime wins at the boundary."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 249_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=300_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 250_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=300_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_window_just_under_large_boundary_uses_fraction_not_flat_cap(
        self, hook_script, tmp_path
    ):
        """A window just under large_window_tokens (299_999 < 300_000)
        still uses the threshold fraction (0.75 * 299_999 = 224_999.25),
        NOT the flat 250K cap -- confirms the boundary is a hard cutoff on
        window size, not proximity to the cap."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 224_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=299_999,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 225_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=299_999,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_custom_max_tokens_is_configurable(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 99_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=100_000,
            large_window_tokens=300_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 100_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=100_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_custom_large_window_tokens_is_configurable(self, hook_script, tmp_path):
        """Lowering large_window_tokens below a window pulls it into the
        flat-cap regime even though it wouldn't be "large" under defaults."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 149_999})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=200_000,
            max_tokens=150_000,
            large_window_tokens=100_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 150_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=200_000,
            max_tokens=150_000,
            large_window_tokens=100_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_haiku_window_capped_by_threshold_not_max_tokens(self, hook_script, tmp_path):
        """Haiku's real 200K window (from _effective_window) is under the
        300K large_window_tokens boundary, so the 150K threshold-fraction
        limit controls instead of the flat 250K cap."""
        transcript = _write_transcript(
            tmp_path,
            [_assistant_entry({"input_tokens": 160_000}, model="claude-haiku-4-5-20260101")],
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_deny_reason_mentions_effective_token_limit(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 260_000})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=1_000_000,
            max_tokens=250_000,
            large_window_tokens=300_000,
        )
        output = json.loads(result.stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert "260000" in reason
        assert "250000" in reason

    def test_missing_max_tokens_argv_fails_open(self, hook_script, tmp_path):
        """Only threshold + window_tokens supplied (old 2-arg invocation) --
        the script now requires 4 argv, so this fails open rather than
        crashing or misbehaving."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 999_999})]
        )
        payload = {"transcript_path": str(transcript), "tool_name": "Bash"}
        result = subprocess.run(
            [sys.executable, str(hook_script), "0.75", "1000000"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_large_window_tokens_argv_fails_open(self, hook_script, tmp_path):
        """Only 3 argv supplied (threshold, window_tokens, max_tokens) --
        the script now requires a 4th argv (large_window_tokens), so this
        fails open."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 999_999})]
        )
        payload = {"transcript_path": str(transcript), "tool_name": "Bash"}
        result = subprocess.run(
            [sys.executable, str(hook_script), "0.75", "1000000", "250000"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestModelAwareWindow:
    """The effective window is capped at 200K for Haiku models. As of the
    2026-07 model catalog, Haiku 4.5 has a 200K context window while all
    other current models (Opus, Sonnet, Fable) default to 1M -- so a single
    large config.window_tokens can serve as the default for most workers as
    long as Haiku is special-cased down to its real, smaller window."""

    def test_haiku_model_denied_at_200k_effective_window(self, hook_script, tmp_path):
        # 160k tokens is under 75% of 1M (the configured window_tokens) but
        # well over 75% of Haiku's real 200K window.
        transcript = _write_transcript(
            tmp_path,
            [_assistant_entry({"input_tokens": 160000}, model="claude-haiku-4-5-20260101")],
        )
        result = _run_hook(
            hook_script, transcript, "Bash", threshold=0.75, window_tokens=1_000_000
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_non_haiku_model_allowed_at_same_usage(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path,
            [_assistant_entry({"input_tokens": 160000}, model="claude-opus-4-8-20260215")],
        )
        result = _run_hook(
            hook_script, transcript, "Bash", threshold=0.75, window_tokens=1_000_000
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_haiku_match_is_case_insensitive(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path,
            [_assistant_entry({"input_tokens": 160000}, model="Claude-Haiku-4.5")],
        )
        result = _run_hook(
            hook_script, transcript, "Bash", threshold=0.75, window_tokens=1_000_000
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_missing_model_uses_configured_window_tokens_unmodified(
        self, hook_script, tmp_path
    ):
        """No model field present -- no haiku detection is possible, so the
        configured window_tokens is used as-is."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 160000})]
        )
        result = _run_hook(
            hook_script, transcript, "Bash", threshold=0.75, window_tokens=1_000_000
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


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


class TestTailReadOptimization:
    """The hook must scan only the tail of large transcripts for efficiency
    (transcripts can reach tens of MB; a full parse on every tool call would
    make cumulative cost quadratic over a session), falling back to a full
    scan only when no usage entry is found in the tail."""

    def test_usage_in_tail_of_large_file_still_found(self, hook_script, tmp_path):
        """A large transcript (> tail window) with the real usage entry as
        the last line -- within the tail -- still yields correct usage."""
        entries = [_filler_entry(60_000) for _ in range(20)]  # ~1.2MB of padding
        entries.append(_assistant_entry({"input_tokens": 190000}))
        transcript = _write_transcript(tmp_path, entries)
        assert transcript.stat().st_size > _TAIL_READ_BYTES

        result = _run_hook(transcript_path=transcript, hook_script=hook_script, tool_name="Bash")
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_usage_outside_tail_falls_back_to_full_scan(self, hook_script, tmp_path):
        """A large transcript where the only usage entry sits before the tail
        window (buried under trailing padding with no usage) is still found
        via the full-scan fallback."""
        entries = [_assistant_entry({"input_tokens": 190000})]
        entries.extend(_filler_entry(60_000) for _ in range(20))  # ~1.2MB after it
        transcript = _write_transcript(tmp_path, entries)
        assert transcript.stat().st_size > _TAIL_READ_BYTES

        result = _run_hook(transcript_path=transcript, hook_script=hook_script, tool_name="Bash")
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_small_file_unaffected(self, hook_script, tmp_path):
        """Small transcripts (well under the tail window) behave as before."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 1000})]
        )
        result = _run_hook(transcript_path=transcript, hook_script=hook_script, tool_name="Bash")
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
