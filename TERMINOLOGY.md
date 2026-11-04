# Terminology

Project-specific terms whose meaning isn't obvious from the code. One entry per term.

- **zombie worker** — a spawned worker Claude Code instance still running (in its tmux window / iTerm pane) after its coordinator session is dead, defunct, or has lost track of it. Heavy: each is a full Claude Code process. Distinct from a *recovered* worker (known via event log) and an *adopted* worker (re-registered via `adopt_worker`).
- **coordinator** — the manager Claude Code session whose maniple MCP server spawned and drives a set of workers. One MCP server instance (and thus one in-memory registry) per coordinator session.
- **supervisor** — informal synonym for **coordinator** (the manager session). Not used anywhere in the code; prefer *coordinator* in code and docs.
- **worker** — a spawned Claude Code (or Codex) session driven by a coordinator; identified by internal ID, terminal ID, or themed name (see CLAUDE.md "Worker Identification").
- **marker** — the `<!maniple-tmux:%1!>`-style string sent as a worker's first message so its terminal pane can be correlated with its JSONL transcript (`session_state.py`).
- **badge** (`coordinator_badge`) — coordinator-facing task label for a worker; display/branch metadata only, never sent to the worker.
- **channel** — ambiguous; always qualify. (a) *CC channel* — Claude Code's messaging-bridge mechanism: an MCP server declaring `experimental['claude/channel']`, launched via `claude --channels plugin:<name>@<marketplace>`, which injects inbound messages into the session transcript as `<channel source="…">` blocks. (b) *Discord/Slack channel* — the chat room itself, identified by a snowflake and passed around as `chat_id`. One CC channel session can serve many Discord channels, all sharing one context.
- **topic file** — proposed per-Discord-channel definition file (purpose, cwd, model, lookback depth) that a CC channel session reads to reconstruct context for that channel, alongside `fetch_messages`. Design term, not yet implemented.
- **readiness probe** / **shell-ready marker** — the `echo "MANIPLE_READY_7f3a9c"` maniple types into a freshly spawned shell to prove it accepts commands (`shell_probe.py`; re-sent every 2.5 s until seen, 10 s cap). A different "marker" from the `<!maniple-tmux:%1!>` correlation marker above — qualify which one you mean.
