# Maniple MCP Server

An MCP server that enables a "manager" Claude Code session to spawn and orchestrate multiple "worker" Claude Code (or Codex) sessions via a terminal backend (tmux or iTerm2).

> **Two audiences for this file:** if you're a *manager session driving workers*, start with the Orchestrating Workers quickstart just below. If you're *developing this repo*, the rest (Running Tests, Project Structure, Key Modules, Implementation Details) is your code map.

## Orchestrating Workers (master quickstart)

Driving a team of workers? The core loop:

1. **`spawn_workers`** — create 1–4 workers. Worktrees are **on by default** (each worker gets its own branch + working dir). Assign work via `issue_id` (issue-tracker workflow) and/or `prompt` (custom task); omit both to spawn idle.
2. **`message_workers`** — send tasks/follow-ups. `wait_mode: "all"` fans out then waits for all; `"any"` for pipelines.
3. **`wait_idle_workers`** / **`wait_for_worker`** — block until workers finish, or until one blocks on input (`waiting_input` | `stuck`).
4. **`answer_worker_question`** — resolve a worker's pending `AskUserQuestion` (single-select and multiSelect). **`list_blocked_workers`** surfaces who's waiting.
5. **`list_workers`** / **`read_worker_logs`** / **`examine_worker`** — see your team and pull a worker's output/context to coordinate the next step. **`poll_worker_changes`** for lifecycle events.
6. **`close_workers`** — tear down (worktree branches are preserved for later merge/PR).

Worker-spawn assignment, wait modes, and worked examples (parallel worktrees, issue-tracker workflows, coordinated handoffs) live in the **README's "Usage Patterns"** section and the `spawn_workers` field reference. Spawn defaults (worktree, agent type, layout, backend) come from `~/.maniple/config.json` — see `config.py`.

## ⚠️ IMPORTANT: Running Tests

**Always use `uv run pytest` to run tests.** Do NOT use `pytest` directly.

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_tmux_backend.py

# Run with verbose output
uv run pytest -v
```

If you get "pytest not found" or similar errors, run `uv sync --group dev` first to install dependencies (the dev group includes pytest).

**DO NOT use:** `pytest`, `python -m pytest`, or `python3 -m pytest` — these will fail.

## Backend Parity Policy (tmux + iTerm)

Any feature or bugfix that touches terminal backend code **MUST** support **both** backends:
- tmux backend
- iTerm backend

"Touches terminal backend code" includes (but is not limited to): tmux/iTerm backend implementations, backend selection/routing, adoption/discovery flows, session ID formats/parsing, and any tools or utilities that interact with terminal sessions.

Changes that touch terminal backend code **MUST** include one of:
- Tests covering **both** backends where applicable, or
- An explicit, documented exception (in the PR description) that includes:
  - Rationale for why parity is not feasible right now
  - A follow-up issue to restore parity (Pebbles or GitHub issue link)

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | Entry point; registers all MCP tools from `tools/` directory |
| `registry.py` | Tracks workers, states: SPAWNING → READY → BUSY → CLOSED. `resolve()` accepts internal ID, terminal ID, or name |
| `config.py` | Config schema + load/merge (`~/.maniple/config.json`); governs spawn defaults (`use_worktree`, `agent_type`, `layout`, backend) |
| `session_state.py` | Parses Claude's JSONL files at `~/.claude/projects/{slug}/{session}.jsonl` |
| `terminal_backends/` | Terminal control (`send_text`, `send_prompt`, window/pane creation) for tmux + iTerm2 |
| `cli_backends/` | Agent CLI switch — `claude.py` vs `codex.py` worker invocation (Claude Code vs Codex) |
| `iterm_utils.py` | Low-level iTerm2 Python API wrappers (used by the iTerm backend) |
| `idle_detection.py` | Stop hook completion detection via JSONL markers |
| `context_pause_hook.py` | Generates the context-pause PreToolUse hook script (Claude only; see below) |
| `usage_pause_hook.py` | Generates the usage-pause PreToolUse hook script (Claude only; 5-hour account window, see below) |
| `names.py` | Themed name sets (Marx Brothers, LOTR, etc.) for worker identification |
| `worktree.py` | Git worktree creation/cleanup for isolated worker branches |

## Critical Implementation Details

### Worker Identification
Workers can be referenced by any of three identifiers:
- **Internal ID**: Short hex string (e.g., `3962c5c4`)
- **Terminal ID**: Prefixed terminal session ID (e.g., `iterm:6D2074A3-2D5B-...` or `tmux:%1`)
- **Worker name**: Human-friendly name (e.g., `Groucho`, `Aragorn`)

All tools accept any of these formats via `registry.resolve()`.

### Enter Key Handling
**Use `\x0d` (carriage return) for Enter, NOT `\n`**
- `\n` creates a newline in input buffer but doesn't submit
- `\x0d` triggers actual Enter keypress
- Multi-line text requires delays before Enter (bracketed paste mode)

### JSONL Session Discovery
Claude stores conversations at:
```
~/.claude/projects/{project-slug}/{session-id}.jsonl
```
Where `{project-slug}` = project path with `/` → `-` (e.g., `/Users/josh/code` → `-Users-josh-code`)

### Idle Detection (Stop Hooks)
Workers are spawned with a stop hook that fires when Claude finishes responding. The hook writes a marker to the JSONL file that `idle_detection.py` watches for. This is the primary completion detection mechanism.

### Context-Pause & Usage-Pause (Claude Code workers only; Codex excluded)
Two independent no-matcher PreToolUse hooks injected by `build_stop_hook_settings_file()`: **context-pause** (on by default) blocks a worker's tool calls — except `Write`/`Read`/`TodoWrite`, enough to write a handoff — once its context crosses a step-function limit (flat 250K cap for windows ≥300K; `threshold × window` below that; Haiku detected and capped at its real 200K window; subagents bounded via their own transcripts). **usage-pause** (on by default at 75%) does the same for the ACCOUNT's rolling 5-hour plan quota, read from the statusline cache file — but additionally allowlists the scheduling tools (`ScheduleWakeup`/`CronCreate`/`CronList`/`CronDelete`) at every rung, since the quota window resets and a paused session must be able to schedule its own continuation — with an escalating override ladder (base → 90% → 95% → unlimited) that only advances with explicit human permission — via the `override_usage_pause` MCP tool or the `maniple usage-override` CLI. Both fail open on any error. Full contracts, thresholds, and verified gotchas: `.claude/rules/pause-hooks.md` (auto-loads when working on the pause/override code).

### Layout Options
These pane layouts apply to the **iTerm2 backend only** (requires iTerm2's Python API to be enabled). The **tmux backend ignores `layout`** and creates one new window per worker in a per-project tmux session (no pane packing / no auto slot-reuse).
- `single`: 1 pane, full window (main)
- `vertical`: 2 panes side by side (left, right)
- `horizontal`: 2 panes stacked (top, bottom)
- `quad`: 4 panes in 2x2 grid (top_left, top_right, bottom_left, bottom_right)
- `triple_vertical`: 3 panes side by side (left, middle, right)

Note: the 1–4 workers-per-`spawn_workers`-call cap is enforced for **both** backends (input validation), but on tmux that's an artificial per-call limit, not a window constraint — call `spawn_workers` again to add more windows to the same session.

## Pull Requests

Use this checklist in PR descriptions:

```markdown
## PR Checklist
- [ ] Backend parity: tmux + iterm
- [ ] Tests cover both backends (or exception + follow-up issue link)
- [ ] `uv run pytest` passes
```

