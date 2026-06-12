# Field learnings — driving real maniple runs

Operational learnings from using maniple to coordinate real multi-worker tasks
(as the master/coordinator session). Distinct from `NOTES.md` (controller
internals); this is "what bites you when you actually run a fleet."

First captured: 2026-06-03, from a 6-worker parallel lab-extraction run
(extract → octopus-merge → analysis → integration → close-out).

---

## 1. `skip_permissions: true` hits a one-time "Bypass Permissions" gate ⚠️ (highest-impact)

`spawn_workers(skip_permissions=true)` launches workers with
`claude --dangerously-skip-permissions`. On the **first such launch per machine**,
Claude Code shows an interactive **"Bypass Permissions mode — 2. Yes, I accept"**
acceptance screen. The worker blocks there, never finishes startup, never writes
its registration marker — and `spawn_workers` returns:

```
claude failed to start in <worktree> within 30.0s. Check that 'claude' is
available and authentication is configured.
  hint: Ensure iTerm2 is running and Python API is enabled...
```

Traps in that failure:
- The **iTerm2 Python-API hint is a red herring** on a tmux backend — the real
  cause is the unaccepted gate.
- The **worktrees and tmux windows ARE created** before the error; the spawn isn't
  atomic. You're left with live-but-blocked, **unregistered** sessions.
- Spawning 4 cold-start workers at once makes it worse (contention + the gate ⇒
  all blow the 30s budget).

**Recovery / handling:**
- Accept the gate once per pane: `tmux send-keys -t <session>:<win> Down` then
  `tmux send-keys -t <session>:<win> Enter`. Acceptance **persists** (machine-level),
  so subsequent spawns start cleanly and register normally.
- Cleaner: kill the orphaned windows (`tmux kill-window`), remove their worktrees
  (`git worktree remove --force`), delete the branches, and re-spawn fresh once the
  gate is accepted.

**Product implications worth considering:**
- `spawn_workers` could detect the bypass-acceptance screen and either pre-accept,
  surface a clearer error than "failed to start within 30s", or document the
  prerequisite.
- The 30s startup budget is too tight for N simultaneous cold starts on a large
  model + big MCP toolset; consider staggering or a longer/maturity-based timeout.
- **Policy:** `--dangerously-skip-permissions` should be opt-in per run, not a
  default a coordinator reaches for unprompted.

## 2. Hard cap: **max 4 workers per `spawn_workers` call**

Returns `{"error": "Maximum 4 workers per spawn"}`. Batch larger fleets across
multiple calls (we ran 6 as 4 + 2).

## 3. A failed/partial spawn leaves **unregistered** sessions

Registration depends on a maniple correlation marker written into the worker's
JSONL during a successful spawn. If the spawn errors before that, the live
sessions have **no marker** → `discover_workers` / `adopt_worker` **cannot see or
adopt them**. They're orphans you must drive by hand (tmux) or kill. Only a clean
spawn yields a manageable worker.

## 4. Deliver long prompts via a file, not raw keystrokes

Pasting a long multi-line prompt with `tmux send-keys` is fragile — embedded
newlines submit the message early. Reliable pattern: write the full prompt to a
temp file and send a one-liner:

```
Read /tmp/<run>/worker-NN-prompt.md and follow its instructions exactly. Begin now.
```

This also works as the `spawn_workers` `prompt` field, keeping the spawn call small.

**Clean up the temp files when the run is done** — these prompt files (and any
scratch the workers drop in the same temp dir) must not linger. Move them to the
**Trash**, not a silent `rm`, so they're recoverable: `trash /tmp/<run-dir>`
(the `trash` CLI is on macOS at `/usr/bin/trash`). Treat the temp dir as run-scoped
and trash the whole thing at close-out.

## 5. No per-worker model control

`WorkerConfig` has no model field — workers inherit the **launching session's
default model** (we got Opus 4.8 everywhere; couldn't pin extraction workers to
Sonnet). Plan cost accordingly, or set the default model before spawning.

## 6. Worktree workers only see **committed** state

A worktree branches from committed HEAD. **Untracked/uncommitted files in the main
working dir are NOT present in a fresh worktree.** Commit the inputs your workers
need (e.g. the raw data file) *before* spawning, or they'll start with nothing.

## 7. Branch naming: let maniple auto-name to avoid collisions

If you pass `worktree.branch` and that branch already exists (e.g. leftovers from a
failed spawn), the spawn fails. Omitting it lets maniple auto-name (branch == the
generated worktree dir name), which sidesteps collisions. Record the auto-names
(from the spawn result / `git worktree list`) for the later merge.

## 8. Worktree lifecycle & fan-in

- `close_workers` **removes worktree dirs but keeps the branches** (and their
  commits) for merge/cherry-pick.
- Disjoint per-worker outputs (each worker writes only its own files) ⇒ a single
  **octopus merge** of all branches into main is conflict-free:
  `git merge --no-edit branch1 branch2 ... branchN`.
- Cleanup: `git branch -d` (safe, merged-only) works; `-D` (force) may be blocked
  by a permission classifier — prefer `-d` after merging.

## 9. `use_worktree: false` for in-tree edits that need uncommitted state

For a phase that must edit files carrying **pending uncommitted changes** (e.g. a
record-reconciliation step touching user-edited docs), run the worker **without a
worktree** — it operates in the main repo dir and sees the live working tree.
`project_path`/`worktree_path` come back null. Tell it **not** to `git add/commit`
so it doesn't sweep up unrelated dirty files; the master commits selectively.

## 10. Monitoring loop that worked

- `wait_for_worker` with **empty `session_ids` = wait on ALL**; returns on the
  first worker to go idle **or** block on a question. Re-call with the shrinking
  set of still-busy workers.
- `list_blocked_workers` catches `AskUserQuestion` escalations fleet-wide.
- `read_worker_logs` (1 page) gets each worker's final report.
- Independent **done-signal**: the worker's branch HEAD has advanced past the base
  commit (i.e. it actually committed its output) — more reliable than "idle,"
  which can also mean it errored or asked.

## 11. Backend

Run the master from inside tmux ⇒ tmux backend auto-selected; workers spawn as
sibling windows in a per-project `maniple-<project>` tmux session. `layout` is
ignored for tmux.
