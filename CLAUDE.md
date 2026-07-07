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

If you get "pytest not found" or similar errors, run `uv sync` first to install dependencies.

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

## Project Structure

```
src/maniple_mcp/
├── server.py                  # FastMCP server entry point, registers all tools
├── registry.py                # Worker tracking (ManagedSession, SessionRegistry)
├── session_state.py           # JSONL parsing for Claude/Codex conversation logs
├── config.py                  # Config schema + loading (~/.maniple/config.json)
├── config_cli.py              # `maniple config` CLI subcommands
├── idle_detection.py          # Stop hook completion detection
├── logging_setup.py           # Logging configuration
├── terminal_backends/         # Terminal backend implementations
│   ├── base.py                # Backend interface
│   ├── tmux.py                # tmux backend (primary, cross-platform)
│   └── iterm.py               # iTerm2 backend (macOS)
├── cli_backends/              # Agent CLI backends
│   ├── base.py                # CLI backend interface
│   ├── claude.py              # Claude Code worker invocation
│   └── codex.py               # OpenAI Codex worker invocation
├── iterm_utils.py             # Low-level iTerm2 API wrappers
├── issue_tracker/             # Issue tracker abstraction + detection
├── schemas/                   # Structured output schemas (e.g. codex)
├── profile.py                 # iTerm2 profile/theme management
├── colors.py                  # Golden ratio tab color generation
├── formatting.py              # Title/badge formatting utilities
├── names.py                   # Worker name generation (themed name sets)
├── worker_prompt.py           # Worker system prompt generation
├── context_pause_hook.py      # Generates the context-pause PreToolUse hook script (Claude only)
├── worktree.py                # Git worktree management
├── subprocess_cache.py        # Cached subprocess calls
├── tools/                     # MCP tool implementations (one per file)
│   ├── spawn_workers.py       # Create worker sessions
│   ├── list_workers.py        # List managed workers
│   ├── examine_worker.py      # Get detailed worker status
│   ├── message_workers.py     # Send prompts to workers
│   ├── check_idle_workers.py  # Check if workers are idle
│   ├── wait_idle_workers.py   # Wait for workers to finish
│   ├── wait_for_worker.py     # Wait for a worker (idle | waiting_input | stuck)
│   ├── answer_worker_question.py # Answer a worker's pending AskUserQuestion
│   ├── list_blocked_workers.py   # List workers blocked on input
│   ├── read_worker_logs.py    # Get conversation history
│   ├── annotate_worker.py     # Add coordinator notes
│   ├── close_workers.py       # Terminate workers
│   ├── discover_workers.py    # Find orphaned tmux/iTerm sessions
│   ├── adopt_worker.py        # Import orphaned sessions
│   ├── prune_recovered_workers.py # Drop unrecoverable workers from registry
│   ├── poll_worker_changes.py # Read worker lifecycle event log
│   ├── worker_events.py       # Worker event types/log
│   ├── list_worktrees.py      # List git worktrees
│   └── issue_tracker_help.py  # Issue tracker quick reference (Beads/Pebbles)
└── utils/                     # Shared utilities
    ├── constants.py           # Shared constants
    ├── env_vars.py            # Env var resolution with fallbacks
    ├── errors.py              # Error response helpers
    └── worktree_detection.py  # Worktree path detection

commands/                      # Slash commands for Claude Code
scripts/                       # Utility scripts
tests/                         # Pytest unit tests
```

## Makefile Targets

```bash
make help                  # Show available targets
make install-commands      # Install slash commands to ~/.claude/commands/
make install-commands-force # Overwrite existing commands
make test                  # Run pytest
make sync                  # Sync dependencies
```

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

### Context-Pause (Claude Code workers only)
`build_stop_hook_settings_file()` also injects a no-matcher PreToolUse hook (governed by `config.context_pause`, on by default at 75% of a 1M-token window) that blocks a worker's tool calls once its context usage crosses the threshold, except for `Write`/`Read`/`TodoWrite` — enough to write a handoff and end its turn. The 1M default matches current Opus/Sonnet/Fable models (2026-07 catalog); the hook script detects a Haiku model id in the transcript (case-insensitive substring match) and caps the effective window at Haiku 4.5's real 200K there instead — no full model map. The hook script itself is generated by `context_pause_hook.py` (stdlib-only, self-contained — it must not import `maniple_mcp` since it runs standalone in the worker's shell) and fails open on any error. **Codex workers are excluded** — no hook mechanism exists for Codex.

### Layout Options
These pane layouts apply to the **iTerm2 backend only**. The **tmux backend ignores `layout`** and creates one new window per worker in a per-project tmux session (no pane packing / no auto slot-reuse).
- `single`: 1 pane, full window (main)
- `vertical`: 2 panes side by side (left, right)
- `horizontal`: 2 panes stacked (top, bottom)
- `quad`: 4 panes in 2x2 grid (top_left, top_right, bottom_left, bottom_right)
- `triple_vertical`: 3 panes side by side (left, middle, right)

Note: the 1–4 workers-per-`spawn_workers`-call cap is enforced for **both** backends (input validation), but on tmux that's an artificial per-call limit, not a window constraint — call `spawn_workers` again to add more windows to the same session.

## Running & Testing

```bash
# Sync dependencies (with dev tools)
uv sync --group dev

# Run tests
uv run --group dev pytest

# Run server directly (debugging)
uv run python -m maniple_mcp
```

## Pull Requests

Use this checklist in PR descriptions:

```markdown
## PR Checklist
- [ ] Backend parity: tmux + iterm
- [ ] Tests cover both backends (or exception + follow-up issue link)
- [ ] `uv run pytest` passes
```

## Requirements
- Python 3.11+
- uv package manager
- A terminal backend: tmux (macOS/Linux) **or** iTerm2 (macOS, Python API enabled)
