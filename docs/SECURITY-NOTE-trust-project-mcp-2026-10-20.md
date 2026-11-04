# Security note: workers auto-trust project MCP servers (`trust_project_mcp`)

_Recorded 2026-10-20. Documentation only: no code or settings were changed (maintainer's decision)._

## The issue
Every spawned worker gets a generated settings file, `~/.claude/claude-team-settings/worker-<marker>.json`. By default that file contains `"enableAllProjectMcpServers": true`. As of 2026-10-20, 394 of the 412 files in that directory had it.

With that setting, Claude Code launches **every MCP server declared in the worker's project `.mcp.json` without the usual trust prompt.** If a worker is pointed at an untrusted or third-party checkout, that repo's `.mcp.json` can start arbitrary local processes (MCP servers) with the worker's privileges, with no human approval. The automated security review (security-guidance plugin) flagged it as MEDIUM, "Agent/Subprocess Permission Bypass (auto-trust of project MCP servers)", on `worker-8f4a958b.json`.

## Why it is set
`src/maniple_mcp/iterm_utils.py:758-766` explains it. Without the setting, a worker in a repo with `.mcp.json` stops at the "New MCP server found" prompt, and maniple's agent-ready detector reports a spurious launch failure while the worker is actually alive and waiting.

## Existing mitigation
`trust_project_mcp` controls it per worker (`spawn_workers.py:91`, `iterm_utils.py:603/786`). It defaults to **True**, which is fine for your own repos. Pass `trust_project_mcp: false` when spawning a worker on any checkout you don't control; the worker then keeps the normal trust prompt.

## Options considered (not taken)
1. **Flip the default to False.** Safe by default, but workers in your own repos with `.mcp.json` would hang at the prompt again, unless the ready detector learns to recognize that prompt.
2. **Use an allowlist instead.** Set `enabledMcpjsonServers: [...]` to named, known servers instead of `enableAllProjectMcpServers`. A new or unknown server in a repo would then still prompt.
3. **Status quo**, chosen for now: keep the default and pass `trust_project_mcp: false` for untrusted repos.

Editing an existing `worker-*.json` file has no effect: the files are regenerated for each spawned worker.
