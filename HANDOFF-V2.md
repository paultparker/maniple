# Usage-Pause v2 Handoff

**STATUS: ALL 8 ITEMS DONE.** Final full suite: 812 passed, 1 e2e deselected
(`uv run pytest -q -m "not e2e"`). Commits, in order: `9101c09` (item 2),
`5725356` (item 3), `1b1b525` (shared `usage_override.py` module backing
items 4-6), `1e86933` (item 4 — MCP tools, docstring-contract design
change applied), `ca2b333` (items 5-6 — CLI subcommands), `a888d85` (item 7
— worker prompt), `98f8c3c` (item 8 — docs). Design change from the
coordinator (not in the original 8-item spec below): the coordinator may
only call `override_usage_pause` with the user's explicit, per-continue
permission, never on its own judgment — enforced via docstring contract in
three places (tool docstring, hook deny-message, worker prompt). See each
item's section below for what was done and any deviations.

Handing off mid-task due to context exhaustion in the previous agent. This file
summarizes where things stand against the 8-item spec from the coordinator's
"Usage-pause v2" message (escalating override ladder feature, built on top of
commit `8660f27`).

WIP commit: `fcbe641` — tests were passing at the time of handoff (755 passed,
1 e2e deselected), but I was mid-task, not at a clean stopping point per item.

## Status by spec item

### 1. Override storage format — DONE (implicitly, embedded in item 2)
Not a separate module. The shape `{"threshold": 0.90|0.95|null, "expires_at": <epoch>}`
is read directly by the hook script's `_read_override()` in
`src/maniple_mcp/usage_pause_hook.py`. Path convention: `<override_dir>/<scope>.json`.
`override_dir` is **hardcoded** (not user-configurable) as
`~/.maniple/usage_override/` — see item 3 notes below for where that constant
lives. No writer exists yet (that's item 4 — the MCP tools).

### 2. Hook script changes (usage_pause_hook.py) — DONE
Fully implemented and tested. New argv: `<base_threshold> <state_file>
<max_stale_seconds> <scope> <override_dir>`.

Implemented, in order, inside `main()`:
1. `scope == "global"` + env `MANIPLE_WORKER` set → `return 0` immediately.
2. Anti-loophole check (`_targets_override_dir()`) — Write/Edit/MultiEdit/
   NotebookEdit targeting a path that resolves (`os.path.realpath`) inside
   `override_dir` → **always deny**, checked BEFORE the allowlist. Fails open
   (skips the check, doesn't deny) if the tool has no path field / it's not a
   string / realpath fails.
3. Allowlist check (`Write`, `Read`, `TodoWrite`) → allow.
4. Override lookup (`_read_override()`): reads `<override_dir>/<scope>.json`;
   missing/malformed (missing keys, wrong types)/expired (`expires_at <=
   time.time()`) → `None` → falls back to base. If valid: `threshold is None`
   → allow immediately (unlimited rung, skips state_file read entirely);
   otherwise `effective_threshold = max(base_threshold, override_threshold)`.
5. state_file stat/mtime-staleness/JSON parse/rate_limits.five_hour.used_percentage
   extraction — unchanged from v1, all fail-open.
6. Compare `used` against `effective_threshold * 100`; deny if `>=`.
7. Deny reason: percent, effective threshold, best-effort `resets_at` local
   time (unchanged from v1), plus a continuation hint — `"...override_usage_pause
   tool."` for worker scope, `"...run: maniple usage-override"` for `scope ==
   "global"`.

Tests: `tests/test_usage_pause_hook.py` — 41 tests total (22 pre-existing +
19 new: `TestOverrideLadder`, `TestGlobalScopeManipleWorker`,
`TestAntiLoophole`). All passing as of the WIP commit. The `_run_hook()` test
helper was extended with `scope`, `override_dir`, `tool_input`, `env` kwargs
(defaults preserve old call sites unchanged — default `override_dir` is a
nonexistent path string so "no override" is the default test behavior).

Committed in `9101c09` (`feat: usage-pause escalating override ladder +
anti-loophole in hook script`).

### 3. Worker env marker (MANIPLE_WORKER=1) — DONE
Implemented in the **single shared choke point** both backends funnel
through: `AgentCLI.build_full_command()` in `src/maniple_mcp/cli_backends/base.py`.
Both `terminal_backends/tmux.py::start_agent_in_session` and
`iterm_utils.py::start_agent_in_session` call `cli.build_full_command(...,
env_vars=env)` — so injecting there covers both backends without duplicating
per-backend logic (satisfies the backend-parity policy with one change, one
test file). `MANIPLE_WORKER=1` is **always** present (even with no
`env_vars` passed) and **always wins** over a caller-supplied
`MANIPLE_WORKER` in `env_vars` (merge order: `{**(env_vars or {}), "MANIPLE_WORKER": "1"}`).

Updated 4 pre-existing exact-string-equality tests in `tests/test_cli_backends.py`
(both `ClaudeCLI` and `CodexCLI` `test_build_full_command_simple` /
`_with_bypass_approvals` / `_with_env_var`) to include the new
`MANIPLE_WORKER=1 ` prefix, and added
`test_build_full_command_always_sets_maniple_worker_env` (Claude) as the
dedicated marker test. All passing.

Committed in `5725356` (`feat: mark worker launches with MANIPLE_WORKER=1
(backend parity)`).

**Also DONE (part of item 3/2 integration, in the WIP commit `fcbe641`, not
yet in its own commit):** `iterm_utils.py::build_stop_hook_settings_file()`
now passes `scope` (= `marker_id`, i.e. the worker's own `session_id` — see
gotcha below) and `override_dir` (hardcoded `Path.home() / ".maniple" /
"usage_override"`) as the two new argv to the usage_pause hook command, both
via `shlex.quote()`. Test added:
`test_usage_pause_hook_command_includes_scope_and_override_dir` in
`tests/test_worker_hooks.py`. All 29 tests in that file pass.

**NOT verified independently** — the WIP commit bundles this iterm_utils.py
change together with the handoff-prep `git add -A`; run `uv run pytest
tests/test_worker_hooks.py -q` to double check after resuming (it passed
right before the handoff interrupt, full suite was 755 passed).

### 4. New MCP tools — DONE (commit `1e86933`, module `1b1b525`)
Both tools exist (`src/maniple_mcp/tools/override_usage_pause.py`,
`clear_usage_override.py`), registered in `tools/__init__.py`, backed by the
shared `usage_override.py` module. Scope = `session.session_id` (confirmed
no separate `stop_hook_marker_id` field). `clear_usage_override` accepts the
literal `"global"` without registry resolution. Docstring contract applied:
`override_usage_pause`'s docstring states prominently it may only be called
after explicit per-continue user permission, never on the coordinator's own
judgment. Hook deny-message hints updated to match (worker-scope hint names
the approval requirement; anti-loophole reason too). 15 tests in
`tests/test_override_usage_pause_tools.py`, including a regression test
guarding the tools/__init__.py registration wiring (a real bug in an earlier
draft — both tools were imported but never called in
`register_all_tools()`).

<details><summary>Original spec (superseded by the above — kept for context)</summary>

NOT STARTED
Neither `override_usage_pause(workers: list[str])` nor
`clear_usage_override(workers: list[str])` exist yet. Per spec:
- `override_usage_pause`: resolve each worker via `registry.resolve()` (or
  the `get_session_or_error` helper from `..utils` — see gotcha below on
  which pattern to use for list-of-workers vs single-worker tools), read
  `<override_dir>/<session.session_id>.json` (scope = the worker's
  `session_id` — **there is no separate `stop_hook_marker_id` field on
  `ManagedSession`; the marker/scope IS `session.session_id`**, confirmed by
  how `build_stop_hook_settings_file(marker_id, ...)` is invoked at spawn
  time), advance one rung (`None → 0.90 → 0.95 → None` meaning unlimited;
  reaching unlimited again should report "already unlimited" rather than
  erroring), write `expires_at` from `rate_limits.five_hour.resets_at` in the
  *configured* `usage_pause.state_file` (fallback: `time.time() + 5*3600` if
  unreadable/missing), write atomically (temp file + `os.replace`, mirroring
  `iterm_utils._write_if_changed` / the temp-file pattern already in this
  repo — do NOT just `path.write_text()` directly), return per-worker dicts
  `{"worker": ..., "new_rung": ..., "expires_at": ...}`.
- `clear_usage_override`: delete `<override_dir>/<scope>.json` for each
  worker in the list; must also accept the literal string `"global"` in the
  list (not resolved via registry — write straight to `global.json`'s path
  and delete it).
- Register both in `src/maniple_mcp/tools/__init__.py` (grep `register_tools`
  there — the previous research agent didn't get to actually reading this
  file, only inferring its existence from `server.py`'s `register_all_tools`
  call at line 350; **verify the aggregator's exact structure before adding
  entries**).
- Follow `src/maniple_mcp/tools/annotate_worker.py` as the structural
  template (see that file for the exact docstring/error-handling/return-shape
  conventions — `error_response(...)` for errors, `{"success": True, ...}`
  dicts for success, MCP tool docstring doubles as the description shown to
  the manager LLM).

No tests exist for this yet.

</details>

### 5. CLI subcommand `maniple usage-override` — DONE (commit `ca2b333`)
`maniple usage-override` (no args: advance global rung; `--clear`; `--status`)
wired into `server.py::main()`'s argparse tree, dispatching to a new
`usage_override_cli.py` helper module (mirrors `config_cli.py`'s role) which
is a thin wrapper over the shared `usage_override.py` ladder logic — no
duplicated advance/clear logic. Tests: `tests/test_usage_override_cli.py`
(helper functions) + `tests/test_server_usage_override_cli.py` (end-to-end
argparse dispatch via `main()`, with `Path.home()` monkeypatched so nothing
touches the real `~/.maniple`).

<details><summary>Original spec (superseded by the above — kept for context)</summary>

Mirror the existing `config` subcommand's argparse structure in
`src/maniple_mcp/server.py::main()` (lines ~514-609 as of this handoff — grep
`config_parser = subparsers.add_parser` to relocate). Add a sibling
`subparsers.add_parser("usage-override", ...)` block, with its own
sub-behavior:
- No args → advance the **global** rung (same ladder as the MCP tool, scope
  `"global"`), print new rung + expiry.
- `--clear` → delete `global.json`.
- `--status` → print current rung, current `used_percentage` (read from the
  configured `state_file`), and expiry.

This CLI logic should almost certainly share the same underlying
advance/clear helper functions as the MCP tools (item 4) rather than
duplicating the ladder-advance logic a third time — consider extracting a
shared `usage_override.py` module (not yet created) with the core
read/advance/clear functions, used by both the MCP tools and this CLI
dispatch block, similar to how `config_cli.py` is the shared logic layer
`server.py`'s `config` dispatch calls into.

No tests exist for this yet.

</details>

### 6. Global installer `maniple install-global-usage-guard` — DONE (commit `ca2b333`, core logic `1b1b525`)
Core logic (`usage_override.install_global_usage_guard()`) and its tests
(`TestInstallGlobalUsageGuard` in `tests/test_usage_override.py`) already
existed from the shared-module commit; this item was really just wiring the
CLI subcommand in `server.py::main()` (`--threshold`, default `0.80`) to call
it and print `script_path` + the settings.json snippet. Covered by
`TestInstallGlobalUsageGuardSubcommand` in
`tests/test_server_usage_override_cli.py`.

<details><summary>Original spec (superseded by the above — kept for context)</summary>

New CLI subcommand (same `server.py::main()` argparse tree as item 5),
accepting `--threshold` (default `0.80`). Must:
- Write the rendered hook script (`usage_pause_hook.render_hook_script()`) to
  `~/.claude/hooks/usage-pause-global.py`, using the existing atomic
  write-if-changed pattern (`iterm_utils._write_if_changed` — consider
  importing/reusing it rather than reimplementing, though it currently lives
  in `iterm_utils.py` which may or may not be the right import site for a
  CLI-only concern; could be worth relocating `_write_if_changed` to a more
  neutral shared module if this creates an awkward import).
- **Print** (not write) the exact `PreToolUse` hooks JSON snippet for
  `~/.claude/settings.json`, using `scope="global"` and the fixed
  `override_dir`. Must NOT touch `~/.claude/settings.json` itself — the user
  merges the printed snippet manually.

No tests exist for this yet.

</details>

### 7. worker_prompt.py update — DONE (commit `a888d85`)
Added one sentence to the `**Plan usage heads-up:**` f-string in
`_generate_claude_worker_prompt()`: the coordinator can grant a continue via
`override_usage_pause`, but only with the user's explicit permission for
that specific continue, never on its own judgment. Extended
`TestUsagePauseHeadsUp` in `tests/test_worker_prompt.py`.

<details><summary>Original spec (superseded by the above — kept for context)</summary>

Spec: "update the usage-pause heads-up to mention the coordinator can grant
continues (one sentence)." The existing heads-up paragraph is in
`_generate_claude_worker_prompt()` in `src/maniple_mcp/worker_prompt.py`,
right after the context-pause heads-up block — look for the `**Plan usage
heads-up:**` f-string. Add one sentence there referencing the
`override_usage_pause` tool (once item 4 exists) or the escalating-ladder
concept generally. `tests/test_worker_prompt.py`'s `TestUsagePauseHeadsUp`
class is the existing test class to extend.

</details>

### 8. Docs (README/CLAUDE.md) — DONE (commit `98f8c3c`)
Both files' "Usage-Pause (Claude Code workers only)" sections now cover the
ladder table, both MCP tools (with the explicit-permission contract on
`override_usage_pause`), both CLI subcommands, global-install behavior, and
the `MANIPLE_WORKER` exclusion mechanism.

<details><summary>Original spec (superseded by the above — kept for context)</summary>

Both files currently have a "Usage-Pause (Claude Code workers only)" section
(README: search `### Usage-Pause`; CLAUDE.md: search `### Usage-Pause`) that
only describes v1 behavior (flat threshold, no ladder). Needs: ladder table
(workers 75→90→95→unlimited; global 80→90→95→unlimited, both reset with the
5-hour window), the two new MCP tools, the CLI subcommand, global-install
steps, and the `MANIPLE_WORKER` exclusion mechanism.

</details>

## Test status at handoff (superseded — see banner at top for final tally)

Full suite (`uv run pytest -q -m "not e2e"`) was **755 passed, 1 deselected**
immediately before the interrupt (last run completed cleanly). The WIP commit
should be at or very near that state — I was told not to re-run the full
suite as part of the handoff protocol, so **please re-run it on resume** to
confirm nothing drifted between that run and the `git add -A` commit (it
shouldn't have — no files were touched in between).

## Key decisions / deviations from the literal spec text

- **`override_dir` is a hardcoded constant, not a config field.** The spec's
  item 1 says "dir `~/.maniple/usage_override/`" without saying whether it's
  configurable. I treated it as fixed (mirroring the existing hardcoded
  `~/.maniple/pending/` dir used for the AskUserQuestion marker mechanism in
  the same function) rather than adding `UsagePauseConfig.override_dir` —
  this kept the config schema smaller. **If the next agent disagrees, this
  is an easy, contained change**: the constant is currently inlined in
  `iterm_utils.py`'s `build_stop_hook_settings_file()` as `override_dir =
  Path.home() / ".maniple" / "usage_override"` — move it to config.py's
  `UsagePauseConfig` if config-ability turns out to matter. It also needs to
  be duplicated/imported into the new MCP tools (item 4) and CLI (items 5-6)
  — currently there is NO single source of truth for this path outside
  `iterm_utils.py`; **the first thing item 4/5/6 work should do is extract
  this constant into a shared location** (e.g. a small `usage_override.py`
  module) rather than re-hardcoding the path string in 3+ places.

- **Anti-loophole path check uses `os.path.realpath` equality/prefix, not
  `pathlib.Path.is_relative_to`.** Chose stdlib `os.path` deliberately since
  the hook script must stay stdlib-only and NOT import `maniple_mcp` — this
  was already true of the rest of the script, just flagging that
  `Path.is_relative_to` (Python 3.9+) would have been the more idiomatic
  choice if this were normal package code, but plain `os.path` string
  operations felt more obviously portable/dependency-free for a script that
  gets `exec`'d standalone in arbitrary worker environments.

- **`MANIPLE_WORKER=1` always wins over caller-supplied env_vars** (merge
  order puts it last). This wasn't explicit in the spec but felt like the
  safer interpretation of "workers must be launched with MANIPLE_WORKER=1" —
  treating it as an invariant rather than a default that could be
  accidentally overridden by some future caller passing `env={"MANIPLE_WORKER": "0"}`.

- **Codex also gets `MANIPLE_WORKER=1`**, per the spec's explicit "Codex too
  is harmless but optional" — since `build_full_command` is on the shared
  `AgentCLI` base/protocol used by both `ClaudeCLI` and `CodexCLI`, it was
  actually *more* work to exclude Codex than to include it, so I included it.

## Gotchas for the next agent

1. **`stop_hook_marker_id` doesn't exist as a field** — a worker's
   marker/scope IS `ManagedSession.session_id`. Don't go looking for a
   separate field; `session.session_id` is what `build_stop_hook_settings_file`
   was called with at spawn time, and it's what the usage_pause hook's
   `scope` argv is set to.

2. **Test env isolation for MANIPLE_WORKER**: `tests/test_usage_pause_hook.py`'s
   `_run_hook()` helper explicitly **scrubs `MANIPLE_WORKER` from the
   inherited subprocess environment** by default (`run_env = {k: v for k, v
   in os.environ.items() if k != "MANIPLE_WORKER"}`), so tests are
   deterministic regardless of whatever environment the test runner itself
   has. Pass `env={"MANIPLE_WORKER": "1"}` explicitly to opt into it for a
   specific test.

3. **`config_module.CONFIG_PATH` monkeypatch pattern**: tests that need a
   custom config write directly to `config_module.CONFIG_PATH` (already
   repointed to a tmp dir by an autouse fixture — check `conftest.py` if this
   is unfamiliar) via `config_module.CONFIG_PATH.write_text(json.dumps({...}))`,
   not via a `monkeypatch.setattr` call in each test.

4. **Atomic-write pattern to reuse**: `iterm_utils._write_if_changed(path,
   content)` — reads existing content, skips write if identical, otherwise
   writes to a `.{name}.tmp-{pid}` sibling file and `os.replace()`s it into
   place. The new MCP tools (item 4) writing override JSON files should use
   a similar temp-file + `os.replace` pattern (the spec explicitly says
   "Write files atomically") — you may want to extract a small generic
   `atomic_write_json(path, data)` helper rather than copy-pasting
   `_write_if_changed`'s string-diffing logic (which doesn't quite fit JSON
   writes that always change on every call, e.g. `expires_at` timestamps).

5. **`uv run pytest` only** — never bare `pytest`. Full suite takes ~64s;
   `-m "not e2e"` excludes one known-flaky real-tmux+claude E2E test
   (`tests/test_blocked_question_e2e.py`) that's environment-dependent and
   unrelated to this feature (confirmed failing even on a clean baseline in
   earlier sessions).

6. **Backend parity policy** (from `CLAUDE.md`): any change touching
   terminal backend code needs both-backend coverage or a documented
   exception. Item 3 (MANIPLE_WORKER) satisfies this trivially since it's
   one shared code path. Items 4-6 (MCP tools, CLI, installer) don't touch
   terminal backend code directly, so parity shouldn't be a concern for them
   — but double-check if the MCP tools end up needing to read
   backend-specific state.

## Files touched so far (this session, all 3 commits + WIP)

- `src/maniple_mcp/usage_pause_hook.py` — ladder + anti-loophole (item 2)
- `tests/test_usage_pause_hook.py` — 19 new tests (item 2)
- `src/maniple_mcp/cli_backends/base.py` — MANIPLE_WORKER=1 injection (item 3)
- `tests/test_cli_backends.py` — updated + 1 new test (item 3)
- `src/maniple_mcp/iterm_utils.py` — scope/override_dir argv wiring (item 3/2 integration, WIP)
- `tests/test_worker_hooks.py` — 1 new test for the above (WIP)

Not yet touched: `src/maniple_mcp/tools/` (no new tool files), `server.py`
(no new subcommands), `config.py`/`config_cli.py` (no override_dir config
field — see decision above), `worker_prompt.py`, `README.md`, `CLAUDE.md`.
