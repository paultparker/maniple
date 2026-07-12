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


def _write_subagent_transcript(
    tmp_path: Path, agent_id: str, entries: list[dict]
) -> tuple[Path, Path]:
    """Write a synthetic subagent transcript at the real derived layout:
    `<parent-stem>/subagents/agent-<agent_id>.jsonl` next to a stand-in
    parent transcript file. Returns (parent_transcript_path,
    subagent_transcript_path).
    """
    parent_path = tmp_path / "transcript.jsonl"
    parent_path.write_text("")
    subagent_dir = tmp_path / "transcript" / "subagents"
    subagent_dir.mkdir(parents=True, exist_ok=True)
    subagent_path = subagent_dir / f"agent-{agent_id}.jsonl"
    lines = [json.dumps(entry) for entry in entries]
    subagent_path.write_text("\n".join(lines) + "\n")
    return parent_path, subagent_path


def _subagent_entry(usage: dict, agent_id: str, *, model: str | None = None) -> dict:
    """A synthetic entry as it really appears in a subagent transcript file:
    isSidechain is always True there, with an agentId tying it to its
    subagent (verified empirically 2026-07-08)."""
    message = {"role": "assistant", "usage": usage}
    if model is not None:
        message["model"] = model
    return {
        "type": "assistant",
        "isSidechain": True,
        "agentId": agent_id,
        "message": message,
    }


def _run_hook_with_payload(
    hook_script: Path,
    payload: dict,
    threshold: float = 0.75,
    window_tokens: int = 200000,
    max_tokens: int = 250000,
    large_window_tokens: int = 300000,
) -> subprocess.CompletedProcess:
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
    return _run_hook_with_payload(
        hook_script, payload, threshold, window_tokens, max_tokens, large_window_tokens
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

    @pytest.mark.parametrize(
        "tool_name", ["ScheduleWakeup", "CronCreate", "CronList", "CronDelete"]
    )
    def test_scheduling_tools_deliberately_not_allowlisted(self, tool_name):
        """Unlike usage-pause, waiting never fixes a full context window --
        a scheduled wake would resume the same over-limit session. The only
        useful continuation is a handoff into a NEW session, so scheduling
        tools stay blocked here (decided 2026-07-12)."""
        assert tool_name not in ALLOWLISTED_TOOLS


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

    @pytest.mark.parametrize(
        "window_tokens, max_tokens, large_window_tokens, allow_tokens, deny_tokens",
        [
            pytest.param(
                1_000_000, 250_000, 300_000, 249_999, 250_000,
                id="large_window_uses_flat_cap_not_threshold_fraction",
            ),
            pytest.param(
                200_000, 250_000, 300_000, 149_999, 150_000,
                id="small_window_still_bound_by_threshold_fraction_under_cap",
            ),
            pytest.param(
                300_000, 250_000, 300_000, 249_999, 250_000,
                id="window_at_large_boundary_uses_flat_cap_not_fraction",
            ),
            pytest.param(
                299_999, 250_000, 300_000, 224_999, 225_000,
                id="window_just_under_large_boundary_uses_fraction_not_flat_cap",
            ),
            pytest.param(
                1_000_000, 100_000, 300_000, 99_999, 100_000,
                id="custom_max_tokens_is_configurable",
            ),
            pytest.param(
                200_000, 150_000, 100_000, 149_999, 150_000,
                id="custom_large_window_tokens_is_configurable",
            ),
        ],
    )
    def test_boundary_allows_just_under_and_denies_at(
        self,
        hook_script,
        tmp_path,
        window_tokens,
        max_tokens,
        large_window_tokens,
        allow_tokens,
        deny_tokens,
    ):
        """The effective limit is a step function of the window, not a flat
        threshold fraction of it -- window >= large_window_tokens uses the
        flat max_tokens cap (boundary inclusive), window < large_window_tokens
        uses threshold * window instead. Each param case below pins one
        boundary from a distinct angle (a genuinely large window, the
        small-window default, exactly at the large-window boundary, just
        under it, and configurable max_tokens/large_window_tokens)."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": allow_tokens})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=window_tokens,
            max_tokens=max_tokens,
            large_window_tokens=large_window_tokens,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": deny_tokens})]
        )
        result = _run_hook(
            hook_script,
            transcript,
            "Bash",
            threshold=0.75,
            window_tokens=window_tokens,
            max_tokens=max_tokens,
            large_window_tokens=large_window_tokens,
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


class TestSubagentContext:
    """A PreToolUse payload for a subagent's own tool call carries an
    `agent_id` field. Empirically verified 2026-07-08 (headless `claude -p`
    probe with a capturing PreToolUse hook, real Agent-tool subagent call):
    `transcript_path` in that payload points at the PARENT transcript file
    (same as a normal main-chain call), NOT a separate subagent file. The
    subagent's own transcript lives on disk at
    `<dir>/<parent-stem>/subagents/agent-<agent_id>.jsonl`, and *every*
    entry in it is `isSidechain: true` carrying a matching `agentId`. So the
    hook must derive that path itself and scan it keyed on `agentId` --
    the ordinary sidechain skip would filter out 100% of a subagent
    transcript's entries -- to bound a subagent's own context growth by the
    same step-function limit as the main session."""

    AGENT_ID = "ad99aa9504d322af6"

    def test_subagent_over_limit_denies(self, hook_script, tmp_path):
        parent_path, _ = _write_subagent_transcript(
            tmp_path,
            self.AGENT_ID,
            [_subagent_entry({"input_tokens": 260_000}, self.AGENT_ID)],
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": self.AGENT_ID,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_subagent_under_limit_allows(self, hook_script, tmp_path):
        parent_path, _ = _write_subagent_transcript(
            tmp_path,
            self.AGENT_ID,
            [_subagent_entry({"input_tokens": 1_000}, self.AGENT_ID)],
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": self.AGENT_ID,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_subagent_allowlisted_tool_allowed_even_over_threshold(
        self, hook_script, tmp_path
    ):
        parent_path, _ = _write_subagent_transcript(
            tmp_path,
            self.AGENT_ID,
            [_subagent_entry({"input_tokens": 260_000}, self.AGENT_ID)],
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Write",
            "agent_id": self.AGENT_ID,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_sidechain_entries_within_subagent_transcript_still_counted(
        self, hook_script, tmp_path
    ):
        """Every entry in a real subagent transcript file is isSidechain,
        so scanning must key off agentId instead of skipping isSidechain
        wholesale -- otherwise usage would always resolve to None and the
        hook would silently fail open for every subagent, unconditionally."""
        parent_path, _ = _write_subagent_transcript(
            tmp_path,
            self.AGENT_ID,
            [_subagent_entry({"input_tokens": 260_000}, self.AGENT_ID)],
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": self.AGENT_ID,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_subagent_transcript_missing_fails_open(self, hook_script, tmp_path):
        """agent_id present but no file at the derived subagent path --
        must fail open, not fall back to scanning the parent transcript
        (that would check the wrong data: the coordinator's own usage, not
        the subagent's)."""
        parent_path = tmp_path / "transcript.jsonl"
        parent_path.write_text(
            json.dumps(_assistant_entry({"input_tokens": 999_999})) + "\n"
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": "no-such-agent",
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_subagent_transcript_corrupt_fails_open(self, hook_script, tmp_path):
        parent_path, subagent_path = _write_subagent_transcript(
            tmp_path, self.AGENT_ID, []
        )
        subagent_path.write_text("not json\n{also not json ]\n")
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": self.AGENT_ID,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_parent_payload_without_agent_id_ignores_subagent_dir(
        self, hook_script, tmp_path
    ):
        """Regression: a normal (non-subagent) payload with no agent_id must
        keep scanning the parent transcript and skipping sidechain entries,
        even when a subagents/ dir happens to exist alongside it."""
        parent_path, _ = _write_subagent_transcript(
            tmp_path,
            self.AGENT_ID,
            [_subagent_entry({"input_tokens": 999_999}, self.AGENT_ID)],
        )
        parent_path.write_text(
            json.dumps(_assistant_entry({"input_tokens": 1_000})) + "\n"
        )
        payload = {"transcript_path": str(parent_path), "tool_name": "Bash"}
        result = _run_hook_with_payload(hook_script, payload, window_tokens=200_000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_regular_sidechain_entry_in_parent_still_skipped_when_no_agent_id(
        self, hook_script, tmp_path
    ):
        """Regression for the parent-transcript path specifically: an
        isSidechain entry inside the MAIN transcript (a speculative branch,
        not a subagent file) must still be skipped when there's no
        agent_id in the payload."""
        transcript = _write_transcript(
            tmp_path,
            [
                _assistant_entry({"input_tokens": 1000}),
                _assistant_entry({"input_tokens": 199000}, is_sidechain=True),
            ],
        )
        payload = {"transcript_path": str(transcript), "tool_name": "Bash"}
        result = _run_hook_with_payload(hook_script, payload, window_tokens=200000)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_empty_string_agent_id_treated_as_absent(self, hook_script, tmp_path):
        """A falsy-but-not-None agent_id (e.g. "") must be normalized to a
        real "no subagent" case, not passed through to the scanner as a
        truthy identity value. Before the fix: main()'s `if agent_id:` gate
        (truthiness) treated "" as absent and left scan_path as the parent
        transcript, but `_last_main_chain_usage(scan_path, agent_id="")`
        was still called with agent_id="" -- the scanner's `if agent_id is
        None` check (identity) then took the agentId-match branch instead
        of the sidechain-skip branch, matched nothing in the parent
        transcript, used stayed None, and the hook silently allowed
        everything regardless of the parent's real usage."""
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 190000})]
        )
        payload = {
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "agent_id": "",
        }
        result = _run_hook_with_payload(hook_script, payload, window_tokens=200000)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestSubagentPathTraversalHardening:
    """Belt-and-suspenders: an agent_id containing path-traversal
    characters must never reach _subagent_transcript_path at all. This
    already fails open today as a side effect (the derived path lands
    somewhere nonexistent or unreadable), but an explicit guard means that
    isn't accidental -- a suspicious agent_id short-circuits straight to
    fail-open instead of being used to build a filesystem path."""

    def test_agent_id_with_path_separator_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 999_999})]
        )
        payload = {
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "agent_id": "evil/agent",
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_agent_id_with_dotdot_fails_open(self, hook_script, tmp_path):
        transcript = _write_transcript(
            tmp_path, [_assistant_entry({"input_tokens": 999_999})]
        )
        payload = {
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "agent_id": "../../etc/passwd",
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_well_formed_agent_id_still_works(self, hook_script, tmp_path):
        """Regression: a normal hex agent_id (no traversal characters) is
        unaffected by the guard and still resolves/scans its subagent
        transcript as before."""
        agent_id = "ad99aa9504d322af6"
        parent_path, _ = _write_subagent_transcript(
            tmp_path, agent_id, [_subagent_entry({"input_tokens": 260_000}, agent_id)]
        )
        payload = {
            "transcript_path": str(parent_path),
            "tool_name": "Bash",
            "agent_id": agent_id,
        }
        result = _run_hook_with_payload(
            hook_script,
            payload,
            window_tokens=1_000_000,
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
