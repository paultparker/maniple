"""Tests for the worker_prompt module."""

import json

import pytest

from maniple_mcp import config as config_module
from maniple_mcp.worker_prompt import (
    AgentType,
    generate_worker_prompt,
    get_coordinator_guidance,
)


class TestGenerateWorkerPrompt:
    """Tests for generate_worker_prompt function."""

    def test_includes_worker_name(self):
        """Prompt should address the worker by name."""
        prompt = generate_worker_prompt("worker-1", "Ringo")
        assert "Ringo" in prompt

    def test_includes_do_work_fully_rule(self):
        """Prompt should contain the 'do work fully' instruction."""
        prompt = generate_worker_prompt("test-session", "George")
        assert "Do the work fully" in prompt

    def test_prompt_is_non_empty_string(self):
        """Prompt should be a non-empty string."""
        prompt = generate_worker_prompt("test", "Worker")
        assert isinstance(prompt, str)
        assert len(prompt) > 100  # Should be substantial


class TestAssignmentCases:
    """Tests for the 4 assignment cases in worker prompts."""

    def test_case1_issue_only(self, tmp_path):
        """With issue only, should show assignment and tracker workflow."""
        # Create a project with pebbles marker for testing
        project_path = tmp_path / "test-repo"
        project_path.mkdir()
        (project_path / ".pebbles").mkdir()

        issue_id = "cic-123"
        prompt = generate_worker_prompt(
            "test", "Worker",
            issue_id=issue_id,
            project_path=str(project_path)
        )
        assert f"Your assignment is `{issue_id}`" in prompt
        assert "workflow" in prompt
        assert "Mark in progress" in prompt
        assert "Close issue" in prompt
        assert "Get to work!" in prompt

    def test_case2_issue_and_custom_prompt(self):
        """With issue and custom prompt, should show both."""
        issue_id = "cic-456"
        prompt = generate_worker_prompt(
            "test", "Worker",
            issue_id=issue_id,
            custom_prompt="Focus on the edge cases"
        )
        assert f"`{issue_id}`" in prompt
        assert "Focus on the edge cases" in prompt
        assert "workflow" in prompt
        assert "Get to work!" in prompt

    def test_case3_custom_prompt_only(self):
        """With custom prompt only, should show the task."""
        prompt = generate_worker_prompt(
            "test", "Worker",
            custom_prompt="Review the auth module for security issues"
        )
        assert "Review the auth module for security issues" in prompt
        assert "The coordinator assigned you the following task" in prompt
        assert "Get to work!" in prompt
        # Should not have tracker workflow
        assert "workflow" not in prompt

    def test_case4_no_issue_no_prompt(self):
        """With neither issue nor prompt, should say coordinator will message."""
        prompt = generate_worker_prompt("test", "Worker")
        assert "The coordinator will send your first task shortly" in prompt
        # Should not have assignment section
        assert "YOUR ASSIGNMENT" not in prompt
        assert "workflow" not in prompt


class TestIssueTrackerWorkflow:
    """Tests for issue tracker workflow instructions."""

    def test_workflow_includes_update_and_close(self, tmp_path):
        """Workflow should include update and close commands."""
        # Create a project with pebbles marker for testing
        project_path = tmp_path / "test-repo"
        project_path.mkdir()
        (project_path / ".pebbles").mkdir()

        issue_id = "cic-abc"
        prompt = generate_worker_prompt(
            "test", "Worker",
            issue_id=issue_id,
            project_path=str(project_path)
        )
        assert f"update {issue_id}" in prompt
        assert f"close {issue_id}" in prompt
        assert "status in_progress" in prompt

    def test_workflow_includes_commit_instruction(self, tmp_path):
        """Workflow should include commit with issue reference."""
        # Create a project with pebbles marker for testing
        project_path = tmp_path / "test-repo"
        project_path.mkdir()
        (project_path / ".pebbles").mkdir()

        issue_id = "cic-abc"
        prompt = generate_worker_prompt(
            "test", "Worker",
            issue_id=issue_id,
            project_path=str(project_path)
        )
        assert f'git commit -m "{issue_id}:' in prompt


class TestContextPauseHeadsUp:
    """Tests for the context-pause threshold paragraph in worker prompts."""

    def test_claude_prompt_mentions_default_threshold(self):
        """With no config file, the default 75% threshold is mentioned."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "~75%" in prompt
        assert "Write/Read/TodoWrite" in prompt

    def test_claude_prompt_uses_configured_threshold(self):
        """A custom configured threshold is reflected in the prompt."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"context_pause": {"threshold": 0.6}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "~60%" in prompt

    def test_claude_prompt_mentions_default_max_tokens_cap(self):
        """With no config file, the default 250,000-token cap is mentioned."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "250,000" in prompt

    def test_claude_prompt_uses_configured_max_tokens(self):
        """A custom configured max_tokens is reflected in the prompt."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"context_pause": {"max_tokens": 100000}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "100,000" in prompt
        assert "250,000" not in prompt

    def test_claude_prompt_mentions_default_large_window_boundary(self):
        """With no config file, the default 300,000-token boundary is mentioned."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "300,000" in prompt

    def test_claude_prompt_uses_configured_large_window_tokens(self):
        """A custom configured large_window_tokens is reflected in the prompt."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"context_pause": {"large_window_tokens": 400000}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "400,000" in prompt
        assert "300,000" not in prompt

    def test_claude_prompt_omits_heads_up_when_disabled(self):
        """No heads-up paragraph is included when context_pause is disabled."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"context_pause": {"enabled": False}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "Context-window heads-up" not in prompt

    def test_codex_prompt_has_no_heads_up(self):
        """Codex workers have no hook mechanism, so no heads-up paragraph."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex")
        assert "Context-window heads-up" not in prompt


class TestUsagePauseHeadsUp:
    """Tests for the usage-pause (5-hour account window) paragraph in
    worker prompts -- sibling to TestContextPauseHeadsUp, independent."""

    def test_claude_prompt_mentions_default_threshold(self):
        """With no config file, the default 75% threshold is mentioned."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "~75%" in prompt
        assert "5-hour" in prompt

    def test_claude_prompt_uses_configured_threshold(self):
        """A custom configured threshold is reflected in the prompt."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"usage_pause": {"threshold": 0.6}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "~60%" in prompt

    def test_claude_prompt_omits_heads_up_when_disabled(self):
        """No heads-up paragraph is included when usage_pause is disabled."""
        config_module.CONFIG_PATH.write_text(
            json.dumps({"usage_pause": {"enabled": False}})
        )
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "Plan usage heads-up" not in prompt

    def test_claude_prompt_mentions_scheduling_tools_stay_available(self):
        """The heads-up tells the worker it can still schedule its own
        continuation (ScheduleWakeup/Cron*) while usage-paused."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "ScheduleWakeup" in prompt

    def test_codex_prompt_has_no_heads_up(self):
        """Codex workers have no hook mechanism, so no heads-up paragraph."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex")
        assert "Plan usage heads-up" not in prompt

    def test_context_and_usage_heads_up_both_present_independently(self):
        """Both heads-up paragraphs can coexist (each independently toggled)."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "Context-window heads-up" in prompt
        assert "Plan usage heads-up" in prompt

    def test_claude_prompt_mentions_override_tool_with_explicit_permission_contract(
        self,
    ):
        """The heads-up must name override_usage_pause and the user-approval
        contract -- the coordinator must never grant a continue on its own
        judgment."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "override_usage_pause" in prompt
        assert "explicit permission" in prompt or "explicit approval" in prompt


class TestGetCoordinatorGuidance:
    """Tests for get_coordinator_guidance function."""

    def test_returns_non_empty_string(self):
        """Should return a non-empty string."""
        guidance = get_coordinator_guidance([{"name": "Groucho", "issue_id": "cic-123"}])
        assert isinstance(guidance, str)
        assert len(guidance) > 0

    def test_contains_team_dispatched_header(self):
        """Guidance should have team dispatched header."""
        guidance = get_coordinator_guidance([{"name": "Groucho", "issue_id": "cic-123"}])
        assert "TEAM DISPATCHED" in guidance

    def test_shows_worker_with_issue(self):
        """Should show worker name and issue assignment."""
        guidance = get_coordinator_guidance([{"name": "Groucho", "issue_id": "cic-123"}])
        assert "Groucho" in guidance
        assert "cic-123" in guidance
        assert "mark in_progress" in guidance

    def test_shows_worker_with_custom_prompt(self):
        """Should show worker with custom task."""
        guidance = get_coordinator_guidance([
            {"name": "Harpo", "custom_prompt": "Review the auth module"}
        ])
        assert "Harpo" in guidance
        assert "Review the auth module" in guidance

    def test_shows_worker_awaiting_task(self):
        """Should show warning for worker awaiting task."""
        guidance = get_coordinator_guidance([
            {"name": "Chico", "awaiting_task": True}
        ])
        assert "Chico" in guidance
        assert "AWAITING TASK" in guidance

    def test_shows_multiple_workers(self):
        """Should show all workers."""
        guidance = get_coordinator_guidance([
            {"name": "Groucho", "issue_id": "cic-123"},
            {"name": "Harpo", "custom_prompt": "Do something"},
            {"name": "Chico", "awaiting_task": True},
        ])
        assert "Groucho" in guidance
        assert "Harpo" in guidance
        assert "Chico" in guidance

    def test_includes_coordination_reminder(self):
        """Should include coordination style reminder."""
        guidance = get_coordinator_guidance([{"name": "Groucho", "issue_id": "cic-123"}])
        assert "Coordination style" in guidance or "Hands-off" in guidance

    def test_truncates_long_custom_prompt(self):
        """Should truncate long custom prompts."""
        long_prompt = "A" * 100
        guidance = get_coordinator_guidance([
            {"name": "Harpo", "custom_prompt": long_prompt}
        ])
        assert "..." in guidance
        # Should not contain the full 100-char string
        assert long_prompt not in guidance


class TestWorktreeMode:
    """Tests for worktree-aware prompt generation."""

    def test_worker_prompt_without_worktree_no_commit(self):
        """Worker prompt without worktree should not mention committing (unless issue)."""
        prompt = generate_worker_prompt("test", "Worker", use_worktree=False)
        # Without issue or worktree, no commit instruction
        assert "Commit when done" not in prompt

    def test_worker_prompt_with_worktree_includes_commit(self):
        """Worker prompt with worktree (no issue) should instruct committing."""
        prompt = generate_worker_prompt("test", "Worker", use_worktree=True)
        assert "Commit when done" in prompt
        assert "cherry-pick" in prompt

    def test_worker_prompt_with_issue_has_commit_in_workflow(self):
        """Worker prompt with issue has commit as part of tracker workflow."""
        prompt = generate_worker_prompt("test", "Worker", issue_id="cic-123")
        # Commit is in the tracker workflow, not separate
        assert "git commit" in prompt
        assert "cic-123" in prompt

    def test_worktree_with_issue_no_separate_commit_section(self):
        """With issue, commit is in tracker workflow - no separate commit section."""
        prompt = generate_worker_prompt("test", "Worker", use_worktree=True, issue_id="cic-123")
        # Should have tracker workflow with commit
        assert 'git commit -m "cic-123:' in prompt
        # Should NOT have separate "Commit when done" section
        assert "Commit when done" not in prompt


class TestAgentTypeParameter:
    """Tests for agent_type parameter in generate_worker_prompt."""

    def test_default_agent_type_is_claude(self):
        """Default agent_type should be claude."""
        prompt = generate_worker_prompt("test", "Worker")
        # Claude prompt has "claude-team" reference
        assert "claude-team" in prompt

    def test_explicit_claude_agent_type(self):
        """Explicit claude agent_type should produce Claude prompt."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="claude")
        assert "claude-team" in prompt
        assert "automatically report" in prompt

    def test_codex_agent_type_produces_different_prompt(self):
        """Codex agent_type should produce Codex-specific prompt."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex")
        # Codex prompt should NOT have claude-team specific references
        assert "claude-team" not in prompt
        # Codex prompt should have different completion detection instructions
        assert "COMPLETED" in prompt or "BLOCKED" in prompt


class TestCodexWorkerPrompt:
    """Tests for Codex-specific worker prompt generation."""

    def test_codex_includes_worker_name(self):
        """Codex prompt should address the worker by name."""
        prompt = generate_worker_prompt("test", "Zeppo", agent_type="codex")
        assert "Zeppo" in prompt

    def test_codex_has_no_mcp_markers(self):
        """Codex prompt should not reference MCP markers."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex")
        assert "claude-team" not in prompt
        assert "automatically report your session" not in prompt

    def test_codex_has_status_completion_instructions(self):
        """Codex prompt should instruct to end with COMPLETED or BLOCKED."""
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex")
        assert "COMPLETED" in prompt
        assert "BLOCKED" in prompt

    def test_codex_tracker_workflow_same_as_claude(self, tmp_path):
        """Codex tracker workflow should match Claude's."""
        # Create a project with pebbles marker for testing
        project_path = tmp_path / "test-repo"
        project_path.mkdir()
        (project_path / ".pebbles").mkdir()

        issue_id = "cic-123"
        codex_prompt = generate_worker_prompt(
            "test", "Worker",
            agent_type="codex",
            issue_id=issue_id,
            project_path=str(project_path)
        )
        claude_prompt = generate_worker_prompt(
            "test", "Worker",
            agent_type="claude",
            issue_id=issue_id,
            project_path=str(project_path)
        )
        for prompt in (codex_prompt, claude_prompt):
            assert f"update {issue_id}" in prompt
            assert f"close {issue_id}" in prompt
            assert "status in_progress" in prompt

    def test_codex_with_issue_only(self):
        """Codex with issue only should show assignment."""
        issue_id = "cic-456"
        prompt = generate_worker_prompt("test", "Worker", agent_type="codex", issue_id=issue_id)
        assert f"Your assignment is `{issue_id}`" in prompt
        assert "workflow" in prompt
        assert "Mark in progress" in prompt

    def test_codex_with_custom_prompt(self):
        """Codex with custom prompt should show the task."""
        prompt = generate_worker_prompt(
            "test", "Worker",
            agent_type="codex",
            custom_prompt="Fix the auth bug"
        )
        assert "Fix the auth bug" in prompt
        assert "The coordinator assigned you the following task" in prompt

    def test_codex_with_worktree_includes_commit(self):
        """Codex with worktree should include commit instructions."""
        prompt = generate_worker_prompt(
            "test", "Worker",
            agent_type="codex",
            use_worktree=True
        )
        assert "Commit when done" in prompt
        assert "cherry-pick" in prompt


class TestMixedTeamCoordinatorGuidance:
    """Tests for coordinator guidance with mixed Claude/Codex teams."""

    def test_single_agent_type_no_indicator(self):
        """With only one agent type, no [type] indicator should appear."""
        guidance = get_coordinator_guidance([
            {"name": "Groucho", "issue_id": "cic-123", "agent_type": "claude"},
            {"name": "Harpo", "issue_id": "cic-456", "agent_type": "claude"},
        ])
        assert "[claude]" not in guidance
        assert "[codex]" not in guidance

    def test_mixed_team_shows_type_indicators(self):
        """With mixed team, should show [type] indicators."""
        guidance = get_coordinator_guidance([
            {"name": "Groucho", "issue_id": "cic-123", "agent_type": "claude"},
            {"name": "GPT-4", "issue_id": "cic-456", "agent_type": "codex"},
        ])
        assert "[claude]" in guidance
        assert "[codex]" in guidance

    def test_mixed_team_shows_guidance_note(self):
        """Mixed team should include guidance about different idle detection."""
        guidance = get_coordinator_guidance([
            {"name": "Groucho", "agent_type": "claude", "issue_id": "cic-123"},
            {"name": "Codex-1", "agent_type": "codex", "issue_id": "cic-456"},
        ])
        assert "Mixed team note" in guidance
        assert "Claude workers" in guidance
        assert "Codex workers" in guidance

    def test_default_agent_type_is_claude(self):
        """Workers without explicit agent_type should default to claude."""
        guidance = get_coordinator_guidance([
            {"name": "Groucho", "issue_id": "cic-123"},  # No agent_type
            {"name": "Codex-1", "agent_type": "codex", "issue_id": "cic-456"},
        ])
        # Should still be mixed team because one is explicitly codex
        assert "[claude]" in guidance
        assert "[codex]" in guidance

    def test_codex_only_team_no_mixed_note(self):
        """Codex-only team should not show mixed team note."""
        guidance = get_coordinator_guidance([
            {"name": "Codex-1", "agent_type": "codex", "issue_id": "cic-123"},
            {"name": "Codex-2", "agent_type": "codex", "issue_id": "cic-456"},
        ])
        assert "Mixed team note" not in guidance
        # Should still not show type indicators (not mixed)
        assert "[codex]" not in guidance


class TestCoordinatorSection:
    """Tests for the coordinator-identity section (spec component 4).

    `coordinator` arrives as a plain dict matching the shape of the
    (sibling-owned) CoordinatorIdentity.to_dict(): pid, pid_start,
    session_id, project_dir, session_name, window_index, pane_index,
    iterm_session_id -- all Optional. This module never imports the
    sibling's coordinator_identity module; it only reads dict keys
    defensively so it can't raise on a shape mismatch.
    """

    FULL_COORDINATOR = {
        "pid": 4242,
        "pid_start": "Thu Jul 24 09:00:00 2026",
        "session_id": "coord-abc-123",
        "project_dir": "/Users/paulparker/Dropbox/code/maniple",
        "session_name": "maniple-verify-design",
        "window_index": 3,
        "pane_index": 0,
        "iterm_session_id": None,
    }

    def test_omitted_when_coordinator_is_none(self):
        """No coordinator info at all -> section omitted entirely."""
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", coordinator=None
        )
        assert "Your coordinator:" not in prompt
        assert "manifest" not in prompt.lower()

    def test_omitted_when_coordinator_is_empty_dict(self):
        """An empty/all-None dict is treated the same as no identity."""
        empty = {
            "pid": None, "pid_start": None, "session_id": None,
            "project_dir": None, "session_name": None,
            "window_index": None, "pane_index": None, "iterm_session_id": None,
        }
        prompt = generate_worker_prompt("worker-1", "Ringo", coordinator=empty)
        assert "Your coordinator:" not in prompt

    def test_full_identity_renders_session_pid_and_tmux(self):
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", coordinator=self.FULL_COORDINATOR
        )
        assert "Your coordinator:" in prompt
        assert "coord-abc-123" in prompt
        assert "4242" in prompt
        assert "maniple-verify-design" in prompt
        assert "window 3" in prompt

    def test_full_identity_includes_alive_reconnect_command(self):
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", coordinator=self.FULL_COORDINATOR
        )
        assert "tmux switch-client -t 'maniple-verify-design'" in prompt
        assert "tmux attach -t 'maniple-verify-design'" in prompt

    def test_full_identity_includes_dead_resume_command(self):
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", coordinator=self.FULL_COORDINATOR
        )
        assert (
            "cd /Users/paulparker/Dropbox/code/maniple && claude --resume coord-abc-123"
            in prompt
        )

    def test_manifest_path_uses_workers_own_session_id(self):
        prompt = generate_worker_prompt(
            "worker-xyz", "Ringo", coordinator=self.FULL_COORDINATOR
        )
        assert "~/.maniple/workers/worker-xyz.json" in prompt

    def test_partial_identity_session_id_only_omits_reconnect_commands(self):
        """Only session_id known -- no pid/tmux/project_dir -- so identity
        line renders but neither reconnect command can be built."""
        partial = {"session_id": "coord-solo"}
        prompt = generate_worker_prompt("worker-1", "Ringo", coordinator=partial)
        assert "coord-solo" in prompt
        assert "switch-client" not in prompt
        assert "claude --resume" not in prompt
        # Manifest line still present since coordinator info is known.
        assert "~/.maniple/workers/worker-1.json" in prompt

    def test_partial_identity_missing_project_dir_omits_dead_command_only(self):
        """pid/tmux/session_id known but no project_dir -- alive command
        renders (needs only tmux), dead command is omitted (needs project_dir
        + session_id together)."""
        partial = {
            "pid": 99,
            "session_id": "coord-2",
            "session_name": "mysession",
            "window_index": 1,
        }
        prompt = generate_worker_prompt("worker-1", "Ringo", coordinator=partial)
        assert "tmux switch-client -t 'mysession'" in prompt
        assert "claude --resume" not in prompt

    def test_iterm_only_identity_mentions_iterm_session(self):
        """No tmux, but an iTerm session id is known."""
        partial = {"session_id": "coord-3", "iterm_session_id": "ABCD-1234"}
        prompt = generate_worker_prompt("worker-1", "Ringo", coordinator=partial)
        assert "ABCD-1234" in prompt
        assert "switch-client" not in prompt

    def test_unknown_dict_keys_are_ignored_not_raising(self):
        """Extra/unrecognized keys (e.g. from a future schema bump) must not
        raise -- this module reads keys defensively."""
        weird = {"session_id": "coord-4", "some_future_field": "whatever"}
        prompt = generate_worker_prompt("worker-1", "Ringo", coordinator=weird)
        assert "coord-4" in prompt

    def test_codex_prompt_also_renders_coordinator_section(self):
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", agent_type="codex",
            coordinator=self.FULL_COORDINATOR,
        )
        assert "Your coordinator:" in prompt
        assert "coord-abc-123" in prompt
        assert "tmux switch-client -t 'maniple-verify-design'" in prompt

    def test_codex_prompt_omitted_when_no_coordinator(self):
        prompt = generate_worker_prompt(
            "worker-1", "Ringo", agent_type="codex", coordinator=None
        )
        assert "Your coordinator:" not in prompt

    def test_default_coordinator_param_is_none(self):
        """Existing callers that don't pass coordinator=... keep working
        (backward compatible default) and get no section."""
        prompt = generate_worker_prompt("worker-1", "Ringo")
        assert "Your coordinator:" not in prompt
