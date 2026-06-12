# Design: Blocked-on-Question Detection for maniple

*Date: 2026-05-31*
*Status: Approved (brainstorming complete) — ready for implementation planning*

## Context

We want a master Claude Code session to orchestrate **parallel, independent** worker
Claude Code sessions in tmux (watchable panes), with **no `--dangerously-skip-permissions`**
so workers can ask questions, and a **three-tier escalation** (worker → master answers
routine per policy → human only for ambiguous) where the policy is **user-configurable,
not hardwired**, and the whole thing is **token-efficient**.

`maniple` (`Martian-Engineering/maniple`, cloned to `~/code/maniple`) was selected as the
off-the-shelf foundation — it spawns/manages real CC sessions in tmux, keeps workers at
`skip_permissions:false`, reads worker JSONL for progress, and leaves policy entirely to
the master's prompt. A live smoke test (2026-05-31) verified the full three-tier loop
works.

Background research: `~/Dropbox/code/talkbox-api/docs/multi-agent-tmux-research.md`.

### The gap this design closes

maniple's idle detection is **stop-hook based** and reports a binary idle/busy state.
A worker blocked on an interactive `AskUserQuestion` multiple-choice menu is **mid-turn**,
so the Stop hook does **not** fire — verified empirically. maniple therefore reports a
worker-waiting-on-a-question as *busy*, and the master cannot use `wait_idle_workers` /
`check_idle_workers` to notice it. In the smoke test the master compensated by manually
scraping panes via `tmux capture-pane` (with temp-file workarounds), which was slow
(~3 min, high-effort Opus) and clumsy.

### Key finding (REVISED 2026-05-31 after live verification)

An initial draft of this spec assumed the `AskUserQuestion` tool-use could be read from the
worker's **JSONL** while pending. **This was verified false.** A live probe showed that while
a worker is blocked on the menu, its JSONL contains **no `assistant` entry at all** — Claude
Code does not flush the assistant message (with the tool-use) to disk until the question is
**answered**. By the time the tool-use is on disk, its `tool_result` is too. So a JSONL scan
can never see a *pending* question. (The earlier "verification" was on already-answered
transcripts; the smoke-test master detected blocks by pane-scraping, which hid the gap.)

**The mechanism that works:** a worker-side **`PreToolUse` hook matching `AskUserQuestion`**
fires the instant the tool is invoked — *while the worker is blocked, before any answer* —
and receives the **complete structured payload** on stdin (verified live):

```json
{ "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
  "tool_use_id": "toolu_...", "session_id": "...", "transcript_path": "...",
  "tool_input": { "questions": [ { "question": "...", "header": "...", "multiSelect": false,
     "options": [ {"label":"INFO","description":"..."}, {"label":"DEBUG","description":"..."} ] } ] } }
```

Detection is therefore **hook-driven**, not JSONL-driven: maniple injects `PreToolUse`
(write a "pending" marker file) and `PostToolUse` (delete it on answer) hooks into each
worker — the **same `--settings` file mechanism it already uses to inject the Stop hook**.
Still zero screen-scraping; structured options; deterministic; available the moment the
worker blocks. Answering is unchanged and verified: send the option **number** (a hotkey
that selects+submits) via the tmux `send_text` primitive.

## Requirements

1. Detect when a worker is blocked on an `AskUserQuestion` prompt — deterministically,
   without ANSI/screen parsing.
2. Surface the parsed question + options to the master through maniple's existing tool
   surface.
3. Let the master answer a single-select question by option number.
4. Keep escalation **policy in the master's prompt** (LLM judgment) for v1 — not hardwired.
5. Reuse maniple's existing machinery (worker `--settings` hook injection, event log,
   registry, `send_text`).
6. Implement **in-tree** in `~/code/maniple`; aim to upstream as a PR.

## Architecture

maniple injects `PreToolUse`/`PostToolUse` hooks for `AskUserQuestion` into each worker
(via the existing worker `--settings` file). The `PreToolUse` hook writes the structured
payload to a per-worker marker file; the `PostToolUse` hook deletes it on answer. A marker-
file detector surfaces this through maniple's event stream and a small new MCP tool surface.
The master makes one wait call after dispatching work and learns "done vs asking vs hung";
it applies its prompt policy and either answers or escalates to the human.

```
worker invokes AskUserQuestion
        │  PreToolUse hook (fires while pending) writes payload →
        │      ~/.maniple/pending/<marker_id>.json     (marker_id = worker session_id)
        ▼
session_state.find_pending_question(session_id)   ← reads the marker file (no JSONL, no screen-scrape)
        │
        ├── wait_for_worker(...) returns state = waiting_input + parsed question/options
        └── poll_worker_changes(...) reports worker_waiting_input events
        ▼
master applies CLAUDE.md policy (LLM judgment)
        ├── routine    → answer_worker_question(worker, option_index) → send_text("k")
        └── ambiguous  → escalate to human → (human decides) → answer_worker_question(...)
        ▼
worker answers → PostToolUse hook deletes the marker file
```

## Components (all in `~/code/maniple`, mirroring existing patterns)

### 0. Worker hook injection — `src/maniple_mcp/iterm_utils.py`
Extend `build_stop_hook_settings_file(marker_id)` (used by **both** the tmux and iTerm2
backends via `start_agent_in_session`) so the generated `--settings` file also contains, in
addition to the existing `Stop` hook:
- `PreToolUse` with `matcher: "AskUserQuestion"` → a self-contained `python3 -c` command
  (stdlib only) that reads the hook payload from stdin and writes it to
  `~/.maniple/pending/<marker_id>.json` (creating the dir).
- `PostToolUse` with `matcher: "AskUserQuestion"` → a `python3 -c` command that deletes
  `~/.maniple/pending/<marker_id>.json` (missing_ok).

`marker_id` is the worker's `session_id` (the same id the Stop hook uses and that the
registry exposes as `session.session_id`), so detection can find the marker without parsing
the payload. Commands embed `marker_id` literally at file-build time.

### 1. Detection — `src/maniple_mcp/session_state.py`
Add `find_pending_question(marker_id: str) -> Optional[dict]`. Reads
`~/.maniple/pending/<marker_id>.json`; if absent/unreadable → `None`. Otherwise shapes the
payload's `tool_use_id` + `tool_input` via `_build_pending_question(...)`, returning `None`
or:

```
{
  "tool_use_id": str,
  "question": str,
  "header": str,
  "multiSelect": bool,
  "options": [ { "label": str, "description": str }, ... ],   # display order
  "num_questions": int,
  "answerable": bool,    # True only for a single single-select question
  "reason": str | None,  # "multiSelect" | "multi_question" | "no_options" when not answerable
}
```

The pure helpers `_build_pending_question(tool_use_id, tool_input)` and
`validate_answer_index(question, option_index)` are unchanged (they already shape/validate
the same `questions/options` structure — which now arrives from the hook payload instead of
the JSONL).

The substantively new logic is the hook injection (§0) and this marker reader. If
`questions[]` contains more than one question, `answerable` is `False` with reason
`"multi_question"` (handled by escalation in v1).

### 2. Master-facing tools — `src/maniple_mcp/tools/`
Follow the existing `register_tools(mcp)` pattern (one file per tool).

- **`wait_for_worker(session_ids, timeout, poll_interval)`** — *primary.* Blocks until any
  listed worker reaches its first resolved state and reports which: `idle` (finished),
  `waiting_input` (asking — includes parsed question/options), or `stuck`. Mirrors
  `wait_idle_workers`. One call tells the master "done vs asking vs hung," so it does not
  juggle separate idle/question polls. (A worker with a pending question is **not** idle;
  this tool reconciles the two states in one place.)

- **`answer_worker_question(session_id, option_index)`** — `option_index` is 1-based into
  the real options array. Re-reads the worker's pending question via
  `find_pending_question(session.session_id)` and **aborts if the `tool_use_id` changed**
  (race guard) or if it is not answerable as a single number (multiSelect / multi-question /
  out-of-range). On success, sends the number via the existing `send_text` primitive (number
  is a hotkey; no Enter).

### 3. Event-stream integration — `src/maniple/events.py` (+ poller)
Add `worker_waiting_input` to `EventType` and emit a `WorkerEvent` carrying the parsed
question/options in `data`, so `poll_worker_changes` and `examine_worker` also surface it.
Low cost since detection already exists. (In stdio MCP mode events are emitted during tool
calls; no background daemon is required for v1.)

## Edge cases / explicit v1 decisions

- **multiSelect question** → cannot be expressed as one number → **escalate to human**
  (surface, do not auto-answer).
- **multiple questions in one call** (`questions[]` > 1) → **escalate to human** in v1.
- **needed custom answer** (right answer is not among the listed options) → the master
  cannot answer via option number in v1 → **escalate to human**.
- **race** (worker answered/advanced between detect and answer) → `answer_worker_question`
  aborts if the pending `tool_use_id` no longer matches.
- The worker UI appends "Type something" and "Chat about this" as entries `N+1, N+2`; the
  answer helper permits only `1..N` (the real options).
- **stale marker** (worker killed while a question was pending → `PostToolUse` never ran) →
  the marker file lingers. v1: `close_workers` deletes `~/.maniple/pending/<marker_id>.json`
  for closed workers, and spawning a worker clears any pre-existing marker for its id.
- **non-tmux / missing pane** (answer path) → return a clear error.

## Error handling

- All new tools return structured error dicts (matching maniple's existing
  `get_session_or_error` convention) rather than raising, so the master can react.
- `answer_worker_question` validates before sending; never blindly types into a pane.
- The marker reader tolerates a missing/partial/malformed marker file (→ `None`).
- The injected hook commands are stdlib-only `python3 -c` snippets that must never crash the
  worker: failures to write/delete the marker are swallowed (best-effort), so a hook problem
  degrades detection but never the worker itself.

## Testing

- **Unit (detection):** write a marker JSON into a temp dir (monkeypatch the pending-dir
  path) and assert `find_pending_question(marker_id)` parses single-select / multiSelect /
  multi-question / no-options correctly, and returns `None` when the marker is absent. Plus
  the existing `_build_pending_question` / `validate_answer_index` tests.
- **Unit (hook injection):** call `build_stop_hook_settings_file(marker_id)`, load the
  written JSON, and assert it contains the `Stop` hook (unchanged) **and** `PreToolUse` /
  `PostToolUse` entries with `matcher == "AskUserQuestion"` whose commands reference
  `<marker_id>.json` under the pending dir.
- **Integration (e2e, opt-in):** build a settings file with the Pre/PostToolUse hooks
  pointed at a temp pending dir, launch a real `claude --settings <file>` worker in tmux,
  wait for the menu; assert the marker file appears **while pending** and
  `find_pending_question` parses INFO/DEBUG; answer by sending the option number; assert the
  worker proceeds and the marker is deleted by `PostToolUse`.
- **Regression:** existing stop-hook idle detection, `wait_idle_workers`, and the contents
  of the Stop hook in the settings file remain unchanged.

## Scope

**In v1:** detection (`pending_question`) + `wait_for_worker` + `answer_worker_question`
(single-select, single-question) + the `worker_waiting_input` event type. Anything not
expressible as one option number — multiSelect, multi-question, or a needed custom answer —
is surfaced and escalated to the human. Policy stays in the master's prompt.

**Deferred TODOs (explicitly tracked):**
1. **Hybrid rules fast-path** — an editable rules file so obvious cases auto-resolve
   without waking the master (token efficiency); only ambiguous questions reach the master.
2. **multiSelect answering.**
3. **Free-text "Type something" custom answers** — master supplies custom text (select the
   "Type something" option, then `send_text` the text + Enter).
4. **iTerm2 backend parity** — should come essentially free via the abstract `send_text`.

## Out of scope

- Changing maniple's stop-hook idle detection (kept as-is; `wait_for_worker` composes with it).
- Any pane/ANSI screen-scraping (the hook-marker path makes it unnecessary).
- The master/escalation policy itself (lives in the master session's `CLAUDE.md`, not in maniple).
