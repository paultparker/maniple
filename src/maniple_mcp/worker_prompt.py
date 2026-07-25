"""Worker pre-prompt generation for coordinated team sessions."""

from typing import Literal, Optional

from .issue_tracker import BACKEND_REGISTRY, IssueTrackerBackend, detect_issue_tracker
from .utils.constants import ISSUE_TRACKER_HELP_TOOL

# Valid agent types for prompt generation
AgentType = Literal["claude", "codex"]


def _resolve_issue_tracker_backend(
    project_path: Optional[str],
) -> IssueTrackerBackend | None:
    """Detect the issue tracker backend for a project path."""
    if not project_path:
        return None
    return detect_issue_tracker(project_path)


def _format_tracker_command(
    backend: IssueTrackerBackend,
    command: str,
    **kwargs: str,
) -> str:
    """Format a tracker command template with the provided arguments."""
    template = backend.commands.get(command)
    if not template:
        return ""
    return template.format(**kwargs)


def _supported_tracker_list() -> str:
    """Return a readable list of supported issue trackers."""
    return ", ".join(sorted(BACKEND_REGISTRY.keys()))


def _enabled_context_pause():
    """Return the config's ContextPauseConfig if context_pause is enabled
    and config can be read, else None.

    Loads config lazily (same pattern as get_claude_command /
    build_stop_hook_settings_file) so this stays monkeypatchable in tests.
    The caller should omit the heads-up paragraph entirely when this
    returns None. A single load covers all of threshold/max_tokens/
    large_window_tokens -- the heads-up paragraph needs all three to
    describe the step-function pause point (large windows use a flat
    max_tokens cap; smaller windows use threshold * window instead).
    """
    try:
        from .config import ConfigError, load_config

        context_pause = load_config().context_pause
    except ConfigError:
        return None

    return context_pause if context_pause.enabled else None


def _enabled_usage_pause():
    """Return the config's UsagePauseConfig if usage_pause is enabled and
    config can be read, else None. Mirrors _enabled_context_pause() for the
    sibling usage_pause feature (the account's rolling 5-hour usage window,
    i.e. Claude plan session credit quota -- not context)."""
    try:
        from .config import ConfigError, load_config

        usage_pause = load_config().usage_pause
    except ConfigError:
        return None

    return usage_pause if usage_pause.enabled else None


def _format_coordinator_identity_line(coordinator: dict) -> str:
    """Build the 'Your coordinator: ...' identity sentence from whichever
    fields are known. Reads keys defensively via .get() so an unexpected
    shape (a future schema bump, or a partial capture) never raises --
    same fail-open philosophy as CoordinatorIdentity itself."""
    parts = []
    session_id = coordinator.get("session_id")
    pid = coordinator.get("pid")
    session_name = coordinator.get("session_name")
    window_index = coordinator.get("window_index")
    iterm_session_id = coordinator.get("iterm_session_id")

    if session_id:
        parts.append(f"Claude session `{session_id}`")
    if pid:
        parts.append(f"PID {pid}")
    if session_name:
        tmux_part = f"tmux `{session_name}`"
        if window_index is not None:
            tmux_part += f" (window {window_index})"
        parts.append(tmux_part)
    elif iterm_session_id:
        parts.append(f"iTerm session `{iterm_session_id}`")

    return "Your coordinator: " + ", ".join(parts) + "."


def _build_coordinator_reconnect_commands(coordinator: dict) -> list[str]:
    """Build whichever of the two reconnect commands can be formed from the
    known fields. Alive needs only a tmux session name; dead needs both
    project_dir and session_id (both required for `claude --resume`)."""
    commands = []
    session_name = coordinator.get("session_name")
    project_dir = coordinator.get("project_dir")
    session_id = coordinator.get("session_id")

    if session_name:
        commands.append(
            f"alive → `tmux switch-client -t '{session_name}'` "
            f"(or `tmux attach -t '{session_name}'` from outside tmux)"
        )
    if project_dir and session_id:
        commands.append(
            f"dead → `cd {project_dir} && claude --resume {session_id}`"
        )
    return commands


def _build_coordinator_section(coordinator: Optional[dict], worker_session_id: str) -> str:
    """Build the coordinator-identity section (spec component 4).

    `coordinator` is a plain dict matching the shape of the (sibling-owned)
    CoordinatorIdentity.to_dict() -- every field Optional. This module never
    imports the sibling's coordinator_identity module; it decouples through
    this dict shape instead. Renders only fields that are known, and omits
    the section entirely when no identity is available at all.
    """
    if not coordinator:
        return ""
    if not any(coordinator.get(key) for key in (
        "session_id", "pid", "session_name", "iterm_session_id", "project_dir"
    )):
        return ""

    identity_line = _format_coordinator_identity_line(coordinator)
    reconnect_commands = _build_coordinator_reconnect_commands(coordinator)

    section = f"\n**{identity_line}**\n"
    if reconnect_commands:
        section += (
            "To hand a human a reconnect command: "
            + "; ".join(reconnect_commands)
            + ".\n"
        )
    section += f"Your manifest: `~/.maniple/workers/{worker_session_id}.json`.\n"
    return section


def _build_tracker_workflow_section(
    issue_id: str,
    backend: IssueTrackerBackend | None,
    step_number: int,
) -> tuple[str, str | None]:
    """Build the issue tracker workflow section and show command hint."""
    if backend:
        update_cmd = _format_tracker_command(
            backend,
            "update",
            issue_id=issue_id,
            status="in_progress",
        )
        close_cmd = _format_tracker_command(
            backend,
            "close",
            issue_id=issue_id,
        )
        show_cmd = _format_tracker_command(backend, "show", issue_id=issue_id) or None
        # Provide backend-specific commands when a tracker is detected.
        section = f"""
{step_number}. **Issue tracker workflow.** You're working on `{issue_id}`. Follow this workflow:
   - Mark in progress: `{update_cmd}`
   - Implement the changes
   - Close issue: `{close_cmd}`
   - Commit with issue reference: `git add -A && git commit -m "{issue_id}: <summary>"`

   Use the {backend.name} CLI (`{backend.cli}`) for issue tracker commands.
"""
        return section, show_cmd

    supported = _supported_tracker_list()
    # Fall back to generic instructions when no tracker is detected.
    section = f"""
{step_number}. **Issue tracker workflow.** You're working on `{issue_id}`. Follow this workflow:
   - Mark in progress in the issue tracker
   - Implement the changes
   - Close the issue when done
   - Commit with issue reference: `git add -A && git commit -m "{issue_id}: <summary>"`

   No issue tracker detected. Supported trackers: {supported}. Use `{ISSUE_TRACKER_HELP_TOOL}` for guidance.
"""
    return section, None


def generate_worker_prompt(
    session_id: str,
    name: str,
    *,
    agent_type: AgentType = "claude",
    use_worktree: bool = False,
    issue_id: Optional[str] = None,
    project_path: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    coordinator: Optional[dict] = None,
) -> str:
    """Generate the pre-prompt text for a worker session.

    Args:
        session_id: The unique identifier for this worker session
        name: The friendly name assigned to this worker
        agent_type: The type of agent CLI ("claude" or "codex")
        use_worktree: Whether this worker is in an isolated worktree
        issue_id: Optional issue tracker ID (if provided, this is the assignment)
        project_path: Optional project path for issue tracker detection
        custom_prompt: Optional additional instructions from the coordinator
        coordinator: Optional dict matching CoordinatorIdentity.to_dict()'s
            shape (pid, pid_start, session_id, project_dir, session_name,
            window_index, pane_index, iterm_session_id -- all Optional).
            Renders a short "who's your coordinator + how to reconnect"
            section when known fields are present; omitted entirely when
            None or fully unknown.

    Returns:
        The formatted pre-prompt string to inject into the worker session

    Note:
        The iTerm-specific marker for session recovery is emitted separately
        via generate_marker_message() in session_state.py, which is called
        before the worker prompt is sent. This marker is only used for Claude
        workers since Codex doesn't parse JSONL markers.
    """
    if agent_type == "codex":
        return _generate_codex_worker_prompt(
            session_id=session_id,
            name=name,
            use_worktree=use_worktree,
            issue_id=issue_id,
            project_path=project_path,
            custom_prompt=custom_prompt,
            coordinator=coordinator,
        )
    # Default to Claude prompt for unknown agent types to maintain backward compatibility
    return _generate_claude_worker_prompt(
        session_id=session_id,
        name=name,
        use_worktree=use_worktree,
        issue_id=issue_id,
        project_path=project_path,
        custom_prompt=custom_prompt,
        coordinator=coordinator,
    )


def _generate_claude_worker_prompt(
    session_id: str,
    name: str,
    use_worktree: bool = False,
    issue_id: Optional[str] = None,
    project_path: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    coordinator: Optional[dict] = None,
) -> str:
    """Generate the pre-prompt for a Claude Code worker session.

    Claude Code workers have access to:
    - claude-team MCP markers for session recovery
    - Stop hook idle detection
    - Full MCP tool ecosystem

    Args:
        session_id: The unique identifier for this worker session
        name: The friendly name assigned to this worker
        use_worktree: Whether this worker is in an isolated worktree
        issue_id: Optional issue tracker ID
        project_path: Optional project path for issue tracker detection
        custom_prompt: Optional additional instructions
        coordinator: Optional CoordinatorIdentity.to_dict()-shaped dict

    Returns:
        Formatted pre-prompt for Claude worker
    """
    # Detect issue tracker backend so we can use the right commands.
    tracker_backend = _resolve_issue_tracker_backend(project_path)

    # Build optional sections with dynamic numbering
    next_step = 4
    extra_sections = ""

    # Issue tracker section (if issue ID provided)
    if issue_id:
        tracker_section, show_cmd = _build_tracker_workflow_section(
            issue_id=issue_id,
            backend=tracker_backend,
            step_number=next_step,
        )
        extra_sections += tracker_section
        next_step += 1

    # Commit section (if worktree but issue tracker section didn't already cover commit)
    if use_worktree and not issue_id:
        commit_section = f"""
{next_step}. **Commit when done.** You're in an isolated worktree branch — commit your
   completed work so it can be easily cherry-picked or merged. Use a clear
   commit message summarizing what you did. Don't push; the coordinator
   handles that.
"""
        extra_sections += commit_section

    # Context-pause heads-up (Claude only -- Codex has no hook mechanism).
    # Omitted entirely if context_pause is disabled or config can't be read.
    # The actual pause point is a step function of window size: windows at
    # or above large_window_tokens pause at a flat max_tokens cap
    # (threshold doesn't apply there at all), while smaller windows (e.g.
    # Haiku) pause at threshold * window instead. Both regimes are named
    # here since the worker's model (and thus which regime applies) isn't
    # known at prompt-generation time.
    context_pause = _enabled_context_pause()
    if context_pause is not None:
        threshold_percent = round(context_pause.threshold * 100)
        max_tokens = context_pause.max_tokens
        large_window_tokens = context_pause.large_window_tokens
        extra_sections += f"""
**Context-window heads-up:** your tool calls will be blocked automatically
(except Write/Read/TodoWrite) once your context usage crosses the pause
point, so you can still save a brief handoff. On large context windows
(>= {large_window_tokens:,} tokens) that's a flat {max_tokens:,}-token cap;
on smaller windows (e.g. Haiku) it's ~{threshold_percent}% of your window
instead. If that happens, write a short handoff file (current state, what's
done, next steps) and end your turn — the coordinator will pick up from
there.
"""

    # Usage-pause heads-up (Claude only -- Codex has no hook mechanism).
    # Sibling to context-pause, but for the account's rolling 5-hour usage
    # window (Claude plan session credit quota) instead of context. Omitted
    # entirely if usage_pause is disabled or config can't be read.
    usage_pause = _enabled_usage_pause()
    if usage_pause is not None:
        usage_threshold_percent = round(usage_pause.threshold * 100)
        extra_sections += f"""
**Plan usage heads-up:** once your account's 5-hour session usage hits
~{usage_threshold_percent}%, your tool calls will be blocked automatically
(except Write/Read/TodoWrite and the scheduling tools
ScheduleWakeup/CronCreate/CronList/CronDelete) so you can still save a brief
handoff and schedule a continuation for after the window resets. If that
happens, write a short handoff file (current state, what's done, next steps)
and end your turn — the coordinator will pick up from there. The coordinator
can grant you a continue past this threshold (an escalating override ladder)
via the `override_usage_pause` tool, but only with the user's explicit permission
for that specific continue — it will not do this on its own judgment.
"""

    # Coordinator-identity section: who spawned this worker + how a human
    # can reconnect to them. Omitted entirely when no identity is known.
    extra_sections += _build_coordinator_section(coordinator, session_id)

    # Closing/assignment section - 4 cases based on issue_id and custom_prompt
    if issue_id and custom_prompt:
        # Case 2: issue_id + custom instructions
        show_hint = (
            f"Use `{show_cmd}` for details." if show_cmd else "Use your issue tracker for details."
        )
        closing = f"""=== YOUR ASSIGNMENT ===

The coordinator assigned you `{issue_id}` ({show_hint}) and included
the following instructions:

{custom_prompt}

Get to work!"""
    elif issue_id:
        # Case 1: issue_id only
        show_hint = (
            f"Use `{show_cmd}` for details." if show_cmd else "Use your issue tracker for details."
        )
        closing = f"""=== YOUR ASSIGNMENT ===

Your assignment is `{issue_id}`. {show_hint} Get to work!"""
    elif custom_prompt:
        # Case 3: custom instructions only
        closing = f"""=== YOUR ASSIGNMENT ===

The coordinator assigned you the following task:

{custom_prompt}

Get to work!"""
    else:
        # Case 4: no issue_id, no instructions - coordinator will message shortly
        closing = "Alright, you're all set. The coordinator will send your first task shortly."

    return f'''Hey {name}! Welcome to the team.

You're part of a coordinated `claude-team` session. Your coordinator has tasks
for you. Do your best to complete the work you've been assigned autonomously.
However, if you have questions/comments/concerns for your coordinator, you can
ask a question in chat and end your turn. `claude-team` will automatically report
your session as idle to the coordinator so they can respond.

=== THE DEAL ===

1. **Do the work fully.** Either complete it or explain what's blocking you in
   your response. The coordinator reads your output to understand what happened.

2. **When you're done,** leave a clear summary in your response. Your completion
   wil be detected automatically — just finish your work and the system handles the rest.

3. **If blocked,** explain what you need in your response. The coordinator will
   read your conversation history and address it.
{extra_sections}
{closing}
'''


def _generate_codex_worker_prompt(
    session_id: str,
    name: str,
    use_worktree: bool = False,
    issue_id: Optional[str] = None,
    project_path: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    coordinator: Optional[dict] = None,
) -> str:
    """Generate the pre-prompt for an OpenAI Codex worker session.

    Codex workers differ from Claude:
    - No claude-team MCP markers (Codex doesn't parse JSONL markers)
    - No Stop hook idle detection (uses output pattern matching or timeouts)
    - Runs with --dangerously-bypass-approvals-and-sandbox instead of --dangerously-skip-permissions

    Args:
        session_id: The unique identifier for this worker session
        name: The friendly name assigned to this worker
        use_worktree: Whether this worker is in an isolated worktree
        issue_id: Optional issue tracker ID
        project_path: Optional project path for issue tracker detection
        custom_prompt: Optional additional instructions
        coordinator: Optional CoordinatorIdentity.to_dict()-shaped dict

    Returns:
        Formatted pre-prompt for Codex worker
    """
    # Detect issue tracker backend so we can use the right commands.
    tracker_backend = _resolve_issue_tracker_backend(project_path)

    # Build optional sections with dynamic numbering
    next_step = 4
    extra_sections = ""

    # Issue tracker section (if issue ID provided) - same workflow as Claude
    if issue_id:
        tracker_section, show_cmd = _build_tracker_workflow_section(
            issue_id=issue_id,
            backend=tracker_backend,
            step_number=next_step,
        )
        extra_sections += tracker_section
        next_step += 1

    # Commit section (if worktree but issue tracker section didn't already cover commit)
    if use_worktree and not issue_id:
        commit_section = f"""
{next_step}. **Commit when done.** You're in an isolated worktree branch — commit your
   completed work so it can be easily cherry-picked or merged. Use a clear
   commit message summarizing what you did. Don't push; the coordinator
   handles that.
"""
        extra_sections += commit_section

    # Coordinator-identity section: who spawned this worker + how a human
    # can reconnect to them. Omitted entirely when no identity is known.
    extra_sections += _build_coordinator_section(coordinator, session_id)

    # Closing/assignment section - 4 cases based on issue_id and custom_prompt
    if issue_id and custom_prompt:
        closing = f"""=== YOUR ASSIGNMENT ===

The coordinator assigned you `{issue_id}` ({f"Use `{show_cmd}` for details." if show_cmd else "Use your issue tracker for details."}) and included
the following instructions:

{custom_prompt}

Get to work!"""
    elif issue_id:
        closing = f"""=== YOUR ASSIGNMENT ===

Your assignment is `{issue_id}`. {f"Use `{show_cmd}` for details." if show_cmd else "Use your issue tracker for details."} Get to work!"""
    elif custom_prompt:
        closing = f"""=== YOUR ASSIGNMENT ===

The coordinator assigned you the following task:

{custom_prompt}

Get to work!"""
    else:
        closing = "Alright, you're all set. The coordinator will send your first task shortly."

    # Codex prompt differs from Claude in key ways:
    # - No reference to claude-team MCP markers
    # - No "automatically report your session as idle" - Codex doesn't use stop hooks
    # - Simpler coordination model (output-based status checking)
    return f'''Hey {name}! Welcome to the team.

You're part of a coordinated multi-agent team. Your coordinator has tasks for you.
Do your best to complete the work you've been assigned autonomously.

If you have questions or concerns, clearly state them at the end of your response
and wait for further instructions. The coordinator will check your progress periodically.

=== THE DEAL ===

1. **Do the work fully.** Either complete it or explain what's blocking you.
   The coordinator reads your output to understand what happened.

2. **When you're done,** leave a clear summary of what you accomplished.
   End your response with "COMPLETED" or "BLOCKED: <reason>" so the coordinator
   can easily assess your status.

3. **If blocked,** explain what you need. The coordinator will read your output
   and address it.
{extra_sections}
{closing}
'''


def get_coordinator_guidance(
    worker_summaries: list[dict],
) -> str:
    """Get the coordinator guidance text to include in spawn_workers response.

    Args:
        worker_summaries: List of dicts with keys:
            - name: Worker name
            - agent_type: Agent type ("claude" or "codex")
            - issue_id: Optional issue tracker ID
            - custom_prompt: Optional custom instructions (truncated for display)
            - awaiting_task: True if worker has no issue_id and no prompt

    Returns:
        Formatted coordinator guidance string
    """
    # Check if we have a mixed team
    agent_types = {w.get("agent_type", "claude") for w in worker_summaries}
    is_mixed_team = len(agent_types) > 1

    # Build per-worker summary lines
    worker_lines = []
    for w in worker_summaries:
        name = w["name"]
        agent_type = w.get("agent_type", "claude")
        issue_id = w.get("issue_id")
        custom_prompt = w.get("custom_prompt")
        awaiting = w.get("awaiting_task", False)

        # Add agent type indicator if mixed team
        type_indicator = f" [{agent_type}]" if is_mixed_team else ""

        if awaiting:
            worker_lines.append(
                f"- **{name}**{type_indicator}: "
                "AWAITING TASK - send them instructions now"
            )
        elif issue_id and custom_prompt:
            # Truncate custom prompt for display
            short_prompt = (
                custom_prompt[:50] + "..." if len(custom_prompt) > 50 else custom_prompt
            )
            worker_lines.append(
                f"- **{name}**{type_indicator}: `{issue_id}` + custom: \"{short_prompt}\""
            )
        elif issue_id:
            worker_lines.append(
                f"- **{name}**{type_indicator}: `{issue_id}` "
                "(issue tracker workflow: mark in_progress -> implement -> close -> commit)"
            )
        elif custom_prompt:
            short_prompt = (
                custom_prompt[:50] + "..." if len(custom_prompt) > 50 else custom_prompt
            )
            worker_lines.append(
                f"- **{name}**{type_indicator}: custom task: \"{short_prompt}\""
            )

    workers_section = "\n".join(worker_lines)

    # Build mixed team guidance if applicable
    mixed_team_section = ""
    if is_mixed_team:
        mixed_team_section = """
**Mixed team note:** You have both Claude and Codex workers:
- **Claude workers**: Idle detection via Stop hooks (automatic)
- **Codex workers**: Check status by reading their output for "COMPLETED" or "BLOCKED"
"""

    return f"""=== TEAM DISPATCHED ===

{workers_section}
{mixed_team_section}
Workers will do the work and explain their output. If blocked, they'll say so.
You review everything before it's considered done.

**Coordination style reminder:** Match your approach to the task. Hands-off for exploratory
work (check in when asked), autonomous for pipelines (wait for completion, read logs, continue).

**WORKTREE LIFECYCLE** — Workers with worktrees commit to ephemeral branches.
When you close workers:
1. Worktree directories are removed, but branches (and commits) are preserved
2. Review commits on worker branches before merging
3. Merge or cherry-pick to main, then delete the worker branch

Branches persist until explicitly deleted with `git branch -d <branch>`.
"""
