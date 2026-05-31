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

### Key enabling finding

When a worker calls `AskUserQuestion`, the tool-use is written to its JSONL transcript as
**structured data** — `questions[].{question, header, multiSelect, options[].{label,
description}}` — in the **same order the worker displayed them** (verified: sending the
literal option number selected the correct displayed option even when the worker reordered
options). maniple already parses worker JSONL. So detection needs **zero screen-scraping**:
scan the JSONL for an `AskUserQuestion` tool-use with no answer yet.

## Requirements

1. Detect when a worker is blocked on an `AskUserQuestion` prompt — deterministically,
   without ANSI/screen parsing.
2. Surface the parsed question + options to the master through maniple's existing tool
   surface.
3. Let the master answer a single-select question by option number.
4. Keep escalation **policy in the master's prompt** (LLM judgment) for v1 — not hardwired.
5. Reuse maniple's existing machinery (JSONL reader, event log, registry, tmux `send_text`).
6. Implement **in-tree** in `~/code/maniple`; aim to upstream as a PR.

## Architecture

Add a JSONL-based pending-question detector to maniple's session-state layer, and surface
it through maniple's existing event stream plus a small new MCP tool surface. The master
makes one wait call after dispatching work and learns "done vs asking vs hung"; it applies
its prompt policy and either answers or escalates to the human.

```
worker calls AskUserQuestion
        │  (structured tool-use written to worker JSONL)
        ▼
session_state.pending_question()   ← new detector (JSONL scan, no screen-scrape)
        │
        ├── wait_for_worker(...) returns state = waiting_input + parsed question/options
        ├── examine_worker(...) includes pending_question
        └── poll_worker_changes(...) reports worker_waiting_input events
        ▼
master applies CLAUDE.md policy (LLM judgment)
        ├── routine    → answer_worker_question(worker, option_index) → send_text("k")
        └── ambiguous  → escalate to human → (human decides) → answer_worker_question(...)
```

## Components (all in `~/code/maniple`, mirroring existing patterns)

### 1. Detection — `src/maniple_mcp/session_state.py`
Add `pending_question()` to `SessionState`. Scans parsed messages for the **latest
`AskUserQuestion` tool-use that has no following answer** (no subsequent message carrying a
`tool_result` for that `tool_use_id`). Returns `None` or:

```
{
  "tool_use_id": str,
  "question": str,
  "header": str,
  "multiSelect": bool,
  "options": [ { "label": str, "description": str }, ... ]   # display order
}
```

This is the only substantively new logic. If `questions[]` contains more than one question,
return a marker indicating multi-question (handled by escalation in v1).

### 2. Master-facing tools — `src/maniple_mcp/tools/`
Follow the existing `register_tools(mcp)` pattern (one file per tool).

- **`wait_for_worker(session_ids, timeout, poll_interval)`** — *primary.* Blocks until any
  listed worker reaches its first resolved state and reports which: `idle` (finished),
  `waiting_input` (asking — includes parsed question/options), or `stuck`. Mirrors
  `wait_idle_workers`. One call tells the master "done vs asking vs hung," so it does not
  juggle separate idle/question polls. (A worker with a pending question is **not** idle;
  this tool reconciles the two states in one place.)

- **`answer_worker_question(session_id, option_index)`** — `option_index` is 1-based into
  the real options array. Re-reads the worker's `pending_question()` and **aborts if the
  `tool_use_id` changed** (race guard) or if it is not answerable as a single number
  (multiSelect / multi-question / out-of-range). On success, sends the number via the
  existing tmux `send_text` primitive (number is a hotkey; no Enter).

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
- **non-tmux / missing pane** → return a clear error.

## Error handling

- All new tools return structured error dicts (matching maniple's existing
  `get_session_or_error` convention) rather than raising, so the master can react.
- `answer_worker_question` validates before sending; never blindly types into a pane.
- Detection tolerates malformed/partial JSONL lines (skip-and-continue, as existing parsing
  does).

## Testing

- **Unit:** synthetic JSONL fixtures — pending single-select, answered, multiSelect,
  multi-question, and no-question — asserting `pending_question()` parses/identifies each
  correctly. Use maniple's existing `tests/` fixture pattern.
- **Integration:** reuse the smoke-test harness — spawn a real worker that asks an
  `AskUserQuestion`; assert `wait_for_worker` returns `waiting_input` with correctly parsed
  options, `answer_worker_question` lands, and the worker proceeds with the chosen option.
- **Regression:** existing stop-hook idle detection and `wait_idle_workers` behavior
  unchanged.

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
- Any pane/ANSI screen-scraping (the JSONL path makes it unnecessary).
- The master/escalation policy itself (lives in the master session's `CLAUDE.md`, not in maniple).
