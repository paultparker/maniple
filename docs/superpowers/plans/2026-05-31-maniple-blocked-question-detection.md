# Blocked-on-Question Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a maniple master session detect when a worker is blocked on an `AskUserQuestion` prompt (which stop-hook idle detection misses), surface the parsed question + options, and answer single-select questions by option number.

**Architecture:** Add a pure JSONL scanner (`find_pending_question`) to `session_state.py`; add two MCP tools (`wait_for_worker`, `answer_worker_question`) that reuse the existing registry/terminal-backend/JSONL machinery; add a `worker_waiting_input` event type. Policy stays in the master's prompt — these are detection + answer primitives only.

**Tech Stack:** Python 3.12, `uv`, `pytest` (`asyncio_mode = "auto"`), FastMCP tool pattern, tmux/iTerm2 terminal backends.

**Repo / branch:** `~/code/maniple`, branch `feature/blocked-question-detection` (spec already committed there).

**Run all tests:** `cd ~/code/maniple && uv run pytest tests/ -q`

---

## File Structure

- **Create** `src/maniple_mcp/tools/wait_for_worker.py` — blocking wait tool returning `idle｜waiting_input｜stuck`.
- **Create** `src/maniple_mcp/tools/answer_worker_question.py` — send an option number to a worker's pane, with a race guard.
- **Create** `tests/test_pending_question.py` — unit tests for the detector + answer-validation helpers.
- **Modify** `src/maniple_mcp/session_state.py` — add `find_pending_question()` and `validate_answer_index()` (pure helpers).
- **Modify** `src/maniple/events.py` — add `"worker_waiting_input"` to `EventType`.
- **Modify** `src/maniple_mcp/tools/__init__.py` — register the two new tools.

Detection logic lives in `session_state.py` next to the existing `is_session_stopped` (files that change together live together). The MCP tools are thin wrappers that delegate to the pure helpers, so the risky logic is unit-tested without an MCP context.

---

## Task 1: Pending-question detector (`find_pending_question`)

The core new logic. A worker is blocked on a question when its JSONL contains an
`AskUserQuestion` tool-use with no later `tool_result` for that id. (Note: the existing
`parse_session` discards `tool_result` blocks, so this is a dedicated raw scan.)

**Files:**
- Modify: `src/maniple_mcp/session_state.py`
- Test: `tests/test_pending_question.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pending_question.py`:

```python
"""Tests for AskUserQuestion pending-question detection and answer validation."""

import json
from pathlib import Path

from maniple_mcp.session_state import find_pending_question, validate_answer_index


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _ask(tool_use_id: str, *, multi=False, questions=None) -> dict:
    """An assistant message that calls AskUserQuestion."""
    qs = questions or [
        {
            "question": "Which logging level should this project use?",
            "header": "Log Level",
            "multiSelect": multi,
            "options": [
                {"label": "INFO", "description": "Standard verbosity."},
                {"label": "DEBUG", "description": "Verbose diagnostics."},
            ],
        }
    ]
    return {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": None,
        "timestamp": "2026-05-31T18:00:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "AskUserQuestion",
                 "input": {"questions": qs}},
            ],
        },
    }


def _answer(tool_use_id: str) -> dict:
    """A user message carrying the tool_result that answers the question."""
    return {
        "type": "user",
        "uuid": "u2",
        "parentUuid": "a1",
        "timestamp": "2026-05-31T18:01:00Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "INFO"},
            ],
        },
    }


def test_detects_pending_single_select(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1")])
    q = find_pending_question(p)
    assert q is not None
    assert q["tool_use_id"] == "toolu_1"
    assert q["question"].startswith("Which logging level")
    assert q["header"] == "Log Level"
    assert q["multiSelect"] is False
    assert [o["label"] for o in q["options"]] == ["INFO", "DEBUG"]
    assert q["answerable"] is True
    assert q["reason"] is None


def test_answered_question_is_not_pending(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1"), _answer("toolu_1")])
    assert find_pending_question(p) is None


def test_multiselect_is_pending_but_not_answerable(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1", multi=True)])
    q = find_pending_question(p)
    assert q is not None
    assert q["answerable"] is False
    assert q["reason"] == "multiSelect"


def test_multi_question_is_pending_but_not_answerable(tmp_path):
    p = tmp_path / "s.jsonl"
    two = [
        {"question": "Q1?", "header": "H1", "multiSelect": False,
         "options": [{"label": "A", "description": ""}]},
        {"question": "Q2?", "header": "H2", "multiSelect": False,
         "options": [{"label": "B", "description": ""}]},
    ]
    _write_jsonl(p, [_ask("toolu_1", questions=two)])
    q = find_pending_question(p)
    assert q["answerable"] is False
    assert q["reason"] == "multi_question"
    assert q["num_questions"] == 2


def test_returns_latest_unanswered_when_multiple(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1"), _answer("toolu_1"), _ask("toolu_2")])
    q = find_pending_question(p)
    assert q["tool_use_id"] == "toolu_2"


def test_no_question_returns_none(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [{"type": "assistant", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hi"}]}}])
    assert find_pending_question(p) is None


def test_missing_file_returns_none(tmp_path):
    assert find_pending_question(tmp_path / "nope.jsonl") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/maniple && uv run pytest tests/test_pending_question.py -q`
Expected: FAIL with `ImportError: cannot import name 'find_pending_question'`.

- [ ] **Step 3: Implement the detector**

Append to `src/maniple_mcp/session_state.py` (after `is_session_stopped`):

```python
# =============================================================================
# AskUserQuestion pending-question detection
# =============================================================================

def _build_pending_question(tool_use_id: str, tool_input: dict) -> dict:
    """Shape a parsed AskUserQuestion input into a pending-question dict.

    answerable is True only for a single, single-select question (the only
    shape v1 can answer with one option number). multiSelect / multi-question
    are surfaced but flagged for escalation.
    """
    questions = tool_input.get("questions") or []
    num = len(questions)
    first = questions[0] if questions else {}
    options = [
        {"label": o.get("label", ""), "description": o.get("description", "")}
        for o in (first.get("options") or [])
        if isinstance(o, dict)
    ]
    multi = bool(first.get("multiSelect", False))

    answerable = True
    reason: str | None = None
    if num != 1:
        answerable, reason = False, "multi_question"
    elif multi:
        answerable, reason = False, "multiSelect"
    elif not options:
        answerable, reason = False, "no_options"

    return {
        "tool_use_id": tool_use_id,
        "question": first.get("question", ""),
        "header": first.get("header", ""),
        "multiSelect": multi,
        "options": options,
        "num_questions": num,
        "answerable": answerable,
        "reason": reason,
    }


def find_pending_question(jsonl_path: Path) -> Optional[dict]:
    """Return the latest unanswered AskUserQuestion in a worker's JSONL, or None.

    A question is pending if an AskUserQuestion tool_use exists with no later
    tool_result carrying its tool_use_id. Returns the parsed question/options
    (see _build_pending_question) or None if the worker is not blocked on one.
    """
    pending_inputs: dict[str, dict] = {}
    order: list[str] = []
    answered: set[str] = set()

    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use" and item.get("name") == "AskUserQuestion":
                        tid = item.get("id")
                        if tid:
                            pending_inputs[tid] = item.get("input", {}) or {}
                            order.append(tid)
                    elif item.get("type") == "tool_result":
                        tid = item.get("tool_use_id")
                        if tid:
                            answered.add(tid)
    except (OSError, FileNotFoundError):
        return None

    for tid in reversed(order):
        if tid not in answered:
            return _build_pending_question(tid, pending_inputs[tid])
    return None


def validate_answer_index(question: dict, option_index: int) -> Optional[str]:
    """Return None if option_index (1-based) is a valid answer, else an error string."""
    if not question.get("answerable", False):
        return f"not answerable ({question.get('reason')})"
    n = len(question.get("options") or [])
    if not isinstance(option_index, int) or option_index < 1 or option_index > n:
        return f"option_index {option_index} out of range 1..{n}"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/maniple && uv run pytest tests/test_pending_question.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Add validation tests and commit**

Append to `tests/test_pending_question.py`:

```python
def test_validate_answer_index_ok():
    q = {"answerable": True, "reason": None, "options": [{"label": "A"}, {"label": "B"}]}
    assert validate_answer_index(q, 1) is None
    assert validate_answer_index(q, 2) is None


def test_validate_answer_index_out_of_range():
    q = {"answerable": True, "reason": None, "options": [{"label": "A"}]}
    assert validate_answer_index(q, 2) is not None
    assert validate_answer_index(q, 0) is not None


def test_validate_answer_index_not_answerable():
    q = {"answerable": False, "reason": "multiSelect", "options": [{"label": "A"}]}
    assert "multiSelect" in validate_answer_index(q, 1)
```

Run: `cd ~/code/maniple && uv run pytest tests/test_pending_question.py -q`
Expected: PASS (10 passed).

```bash
git add src/maniple_mcp/session_state.py tests/test_pending_question.py
git commit -m "feat: detect pending AskUserQuestion in worker JSONL"
```

---

## Task 2: `answer_worker_question` MCP tool

Thin wrapper: resolve the worker, re-read its pending question (race guard via
`tool_use_id`), validate the index, send the number via the terminal backend.

**Files:**
- Create: `src/maniple_mcp/tools/answer_worker_question.py`
- Modify: `src/maniple_mcp/tools/__init__.py`

- [ ] **Step 1: Write the tool**

Create `src/maniple_mcp/tools/answer_worker_question.py`:

```python
"""
Answer worker question tool.

Sends an option number to a worker blocked on an AskUserQuestion prompt.
The option number is a hotkey that selects AND submits, so no Enter is sent.
"""

from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..session_state import find_pending_question, validate_answer_index
from ..utils import error_response, HINTS


def register_tools(mcp: FastMCP) -> None:
    """Register answer_worker_question tool on the MCP server."""

    @mcp.tool()
    async def answer_worker_question(
        ctx: Context[ServerSession, "AppContext"],
        session_id: str,
        option_index: int,
        expected_tool_use_id: str | None = None,
    ) -> dict:
        """
        Answer a worker that is blocked on a single-select AskUserQuestion.

        Sends the 1-based option_index to the worker's pane (the number selects
        and submits). Use wait_for_worker first to learn the question, options,
        and tool_use_id.

        Args:
            session_id: Worker to answer. Accepts internal ID, terminal ID, or name.
            option_index: 1-based index into the question's real options.
            expected_tool_use_id: If given, abort unless the worker is still
                blocked on this exact question (race guard).

        Returns:
            Dict with success, session_id, option_index, chosen_label.
        """
        app_ctx = ctx.request_context.lifespan_context
        registry = app_ctx.registry
        backend = app_ctx.terminal_backend

        session = registry.resolve(session_id)
        if not session:
            return error_response(
                f"Session not found: {session_id}", hint=HINTS["session_not_found"]
            )

        jsonl_path = session.get_jsonl_path()
        if not jsonl_path:
            return error_response(
                f"No JSONL file for: {session_id}", hint=HINTS["no_jsonl_file"]
            )

        question = find_pending_question(jsonl_path)
        if question is None:
            return error_response(
                f"{session_id} is not blocked on a question right now."
            )

        if expected_tool_use_id and question["tool_use_id"] != expected_tool_use_id:
            return error_response(
                "Worker has moved on (tool_use_id changed); not answering a stale question.",
            )

        err = validate_answer_index(question, option_index)
        if err:
            return error_response(
                f"Cannot answer {session_id}: {err}. "
                f"Escalate to the human instead.",
            )

        # Number key selects AND submits in the AskUserQuestion menu; no Enter.
        await backend.send_text(session.terminal_session, str(option_index))

        return {
            "success": True,
            "session_id": session.session_id,
            "option_index": option_index,
            "chosen_label": question["options"][option_index - 1]["label"],
        }
```

- [ ] **Step 2: Register the tool**

In `src/maniple_mcp/tools/__init__.py`, add the import next to the others:

```python
from . import answer_worker_question
```

and add this line inside `register_all_tools`, in the "Tools that don't need ensure_connection" block:

```python
    answer_worker_question.register_tools(mcp)
```

- [ ] **Step 3: Verify it imports and registers**

Run: `cd ~/code/maniple && uv run python -c "from maniple_mcp.tools import answer_worker_question; print('ok')"`
Expected: prints `ok` (no ImportError).

- [ ] **Step 4: Commit**

```bash
git add src/maniple_mcp/tools/answer_worker_question.py src/maniple_mcp/tools/__init__.py
git commit -m "feat: add answer_worker_question MCP tool"
```

---

## Task 3: `worker_waiting_input` event type

**Files:**
- Modify: `src/maniple/events.py`
- Test: `tests/test_pending_question.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pending_question.py`:

```python
def test_worker_waiting_input_event_roundtrips(tmp_path, monkeypatch):
    import maniple.events as events
    # Both append_event and read_events_since resolve the log via get_events_path().
    monkeypatch.setattr(events, "get_events_path", lambda: tmp_path / "events.jsonl")
    ev = events.WorkerEvent(
        ts="2026-05-31T18:00:00Z",
        type="worker_waiting_input",
        worker_id="abc123",
        data={"question": "Which DB?", "tool_use_id": "toolu_9"},
    )
    events.append_event(ev)
    loaded = events.read_events_since()
    assert any(e.type == "worker_waiting_input" and e.worker_id == "abc123" for e in loaded)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/maniple && uv run pytest tests/test_pending_question.py -k waiting_input_event -q`
Expected: FAIL — the `Literal` type rejects `"worker_waiting_input"` (or a typing/validation error on construction).

- [ ] **Step 3: Add the event type**

In `src/maniple/events.py`, extend the `EventType` literal:

```python
EventType = Literal[
    "snapshot",
    "worker_started",
    "worker_idle",
    "worker_active",
    "worker_waiting_input",
    "worker_closed",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/code/maniple && uv run pytest tests/test_pending_question.py -k waiting_input_event -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maniple/events.py tests/test_pending_question.py
git commit -m "feat: add worker_waiting_input event type"
```

---

## Task 4: `wait_for_worker` MCP tool

One blocking call that returns the first worker to reach a resolved state:
`idle`, `waiting_input` (with the parsed question), or — on timeout — `stuck` vs
`working`. Emits a `worker_waiting_input` event when it observes a blocked worker.

**Files:**
- Create: `src/maniple_mcp/tools/wait_for_worker.py`
- Modify: `src/maniple_mcp/tools/__init__.py`

- [ ] **Step 1: Write the tool**

Create `src/maniple_mcp/tools/wait_for_worker.py`:

```python
"""
Wait for worker tool.

Blocks until any listed worker reaches a resolved state and reports which:
- "idle":          finished its turn (stop hook fired)
- "waiting_input": blocked on an AskUserQuestion (includes the parsed question)
On timeout, classifies unresolved workers as "stuck" (no JSONL activity past the
stale threshold) or "working".
"""

import asyncio
import time
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

import maniple.events as events
from ..session_state import find_pending_question
from ..utils import error_response, HINTS


def register_tools(mcp: FastMCP) -> None:
    """Register wait_for_worker tool on the MCP server."""

    @mcp.tool()
    async def wait_for_worker(
        ctx: Context[ServerSession, "AppContext"],
        session_ids: list[str],
        timeout: float | None = 600.0,
        poll_interval: float | None = 2.0,
        stale_threshold_minutes: float | None = 10.0,
    ) -> dict:
        """
        Wait until any worker is idle OR blocked on a question.

        Returns as soon as ONE worker resolves, so the master can react to the
        first event (done vs asking) instead of polling idle and questions
        separately. A worker blocked on a question is NOT idle, so use this
        instead of wait_idle_workers when workers may ask questions.

        Args:
            session_ids: Workers to wait on. Accepts internal IDs, terminal IDs, or names.
            timeout: Max seconds to wait (default 600).
            poll_interval: Seconds between checks (default 2).
            stale_threshold_minutes: On timeout, workers idle-on-disk longer than
                this are reported as "stuck" (default 10).

        Returns:
            On resolve: {resolved: {session_id, state, question}, timed_out: False}
              - state is "idle" or "waiting_input"
              - question is the parsed AskUserQuestion dict (only for waiting_input), else None
            On timeout: {resolved: None, timed_out: True, workers: [{session_id, state}]}
              - state is "stuck" or "working"
        """
        timeout = timeout if timeout is not None else 600.0
        poll_interval = poll_interval if poll_interval is not None else 2.0
        stale_threshold_minutes = (
            stale_threshold_minutes if stale_threshold_minutes is not None else 10.0
        )

        app_ctx = ctx.request_context.lifespan_context
        registry = app_ctx.registry

        if not session_ids:
            return error_response(
                "session_ids is required and must contain at least one session ID",
                hint=HINTS["registry_empty"],
            )

        resolved = registry.resolve  # local alias
        missing = [sid for sid in session_ids if not resolved(sid)]
        if missing:
            return error_response(
                f"Sessions not found: {', '.join(missing)}",
                hint=HINTS["session_not_found"],
            )

        deadline = time.monotonic() + timeout
        while True:
            for sid in session_ids:
                session = resolved(sid)
                if not session:
                    continue
                jsonl_path = session.get_jsonl_path()

                if jsonl_path:
                    question = find_pending_question(jsonl_path)
                    if question is not None:
                        events.append_event(events.WorkerEvent(
                            ts=_now_iso(),
                            type="worker_waiting_input",
                            worker_id=session.session_id,
                            data={
                                "tool_use_id": question["tool_use_id"],
                                "question": question["question"],
                                "answerable": question["answerable"],
                            },
                        ))
                        return {
                            "resolved": {
                                "session_id": session.session_id,
                                "state": "waiting_input",
                                "question": question,
                            },
                            "timed_out": False,
                        }

                if session.is_idle():
                    return {
                        "resolved": {
                            "session_id": session.session_id,
                            "state": "idle",
                            "question": None,
                        },
                        "timed_out": False,
                    }

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval)

        # Timed out: classify remaining workers as stuck vs working.
        stale_seconds = stale_threshold_minutes * 60.0
        now = time.time()
        workers = []
        for sid in session_ids:
            session = resolved(sid)
            jsonl_path = session.get_jsonl_path() if session else None
            age = None
            if jsonl_path:
                try:
                    age = now - jsonl_path.stat().st_mtime
                except OSError:
                    age = None
            state = "stuck" if (age is not None and age > stale_seconds) else "working"
            workers.append({
                "session_id": session.session_id if session else sid,
                "state": state,
            })

        return {"resolved": None, "timed_out": True, "workers": workers}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 2: Register the tool**

In `src/maniple_mcp/tools/__init__.py`, add the import:

```python
from . import wait_for_worker
```

and inside `register_all_tools`, in the "Tools that don't need ensure_connection" block:

```python
    wait_for_worker.register_tools(mcp)
```

- [ ] **Step 3: Verify import + full suite still green**

Run: `cd ~/code/maniple && uv run python -c "from maniple_mcp.tools import wait_for_worker; print('ok')"`
Expected: prints `ok`.

Run: `cd ~/code/maniple && uv run pytest tests/ -q`
Expected: PASS (existing suite + the new pending-question tests; no regressions).

- [ ] **Step 4: Commit**

```bash
git add src/maniple_mcp/tools/wait_for_worker.py src/maniple_mcp/tools/__init__.py
git commit -m "feat: add wait_for_worker MCP tool (idle | waiting_input | stuck)"
```

---

## Task 5: End-to-end integration test (real worker)

Proves the full path with a real worker session, mirroring the validated smoke test.
This is a `slow` test (spawns a real `claude`), so it is opt-in.

**Files:**
- Create: `tests/test_blocked_question_e2e.py`

- [ ] **Step 1: Write the integration test**

Create `tests/test_blocked_question_e2e.py`:

```python
"""End-to-end: a real worker blocks on AskUserQuestion; detector + answer work.

Opt-in (spawns a real claude session in tmux). Run explicitly:
    uv run pytest tests/test_blocked_question_e2e.py -m e2e -q
Requires: tmux, the `claude` CLI on PATH, and a logged-in Claude account.
"""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from maniple_mcp.session_state import find_pending_question

pytestmark = pytest.mark.e2e

SESSION = "maniple_e2e_test"
PROMPT = (
    "Using the AskUserQuestion tool, ask me to choose a logging level: "
    "INFO or DEBUG. Do this as your very first action and nothing else."
)


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout


def _capture() -> str:
    return _tmux("capture-pane", "-p", "-t", SESSION)


@pytest.fixture
def worker_dir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    yield d


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not installed")
@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_detect_and_answer_real_worker(worker_dir):
    _tmux("kill-session", "-t", SESSION)  # ignore failure if absent
    _tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50",
          "-c", str(worker_dir))
    try:
        _tmux("send-keys", "-t", SESSION, f'claude "{PROMPT}"', "Enter")

        # Wait for the menu to render.
        deadline = time.time() + 60
        while time.time() < deadline and "Enter to select" not in _capture():
            time.sleep(2)
        assert "Enter to select" in _capture(), "worker never showed the menu"

        # Locate the worker's JSONL and assert the detector finds the question.
        slug = ("-" + str(worker_dir).lstrip("/")).replace("/", "-").replace(".", "-")
        proj = Path.home() / ".claude" / "projects" / slug
        jsonl = max(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)

        deadline = time.time() + 30
        q = None
        while time.time() < deadline:
            q = find_pending_question(jsonl)
            if q:
                break
            time.sleep(2)
        assert q is not None and q["answerable"] is True
        labels = [o["label"] for o in q["options"]]
        assert "INFO" in labels and "DEBUG" in labels

        # Answer by sending the option number (hotkey selects + submits).
        idx = labels.index("DEBUG") + 1
        _tmux("send-keys", "-t", SESSION, "-l", str(idx))
        time.sleep(4)

        pane = _capture()
        assert "DEBUG" in pane and "Enter to select" not in pane
        assert find_pending_question(jsonl) is None  # no longer pending
    finally:
        _tmux("kill-session", "-t", SESSION)
```

- [ ] **Step 2: Register the `e2e` marker**

In `pyproject.toml`, under `[tool.pytest.ini_options]`, add a markers entry (create the
key if absent) so the marker is recognized:

```toml
markers = [
    "e2e: end-to-end tests that spawn real claude sessions (opt-in)",
]
```

- [ ] **Step 3: Confirm it is excluded from the default run**

Run: `cd ~/code/maniple && uv run pytest tests/ -q -m "not e2e"`
Expected: PASS, and the e2e test is deselected (not spawning claude).

- [ ] **Step 4: Run the e2e test once manually to verify the real path**

Run: `cd ~/code/maniple && uv run pytest tests/test_blocked_question_e2e.py -m e2e -q`
Expected: PASS — worker shows menu, detector finds INFO/DEBUG, answering DEBUG lands and clears the menu.
(If it fails because of a transient claude startup prompt, re-run; this is the only non-deterministic test.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_blocked_question_e2e.py pyproject.toml
git commit -m "test: e2e blocked-question detect + answer with real worker"
```

---

## Task 6: Wire the master to use the new tools

Update the master's escalation instructions to use the deterministic tools instead of
pane-scraping. This is the payoff: no more `tmux capture-pane` workarounds.

**Files:**
- Modify: `~/code/maniple-smoketest/CLAUDE.md` (the master workspace from the smoke test)

- [ ] **Step 1: Replace the pane-polling section with tool usage**

Replace the "Critical fact" + "poll panes" + "answering" sections of
`~/code/maniple-smoketest/CLAUDE.md` with:

```markdown
## Detecting and answering worker questions

maniple now surfaces blocked questions directly — do NOT scrape tmux panes.

1. After dispatching work, call `wait_for_worker(session_ids=[...])`. It returns when a
   worker is `idle` (finished) or `waiting_input` (blocked on a question). For the latter
   the result includes `question` (the text, options, `tool_use_id`, and `answerable`).
2. Apply the escalation policy below.
3. To auto-answer a routine question, call
   `answer_worker_question(session_id, option_index, expected_tool_use_id=<from the question>)`.
   option_index is 1-based into `question.options`.
4. If `question.answerable` is false (multiSelect, multiple questions, or no listed option
   fits), you cannot answer by number — escalate to the human.
```

(Keep the existing "Escalation policy" section unchanged.)

- [ ] **Step 2: Manual end-to-end re-run of the two-worker scenario**

Restart the master session so it picks up the new maniple tools and CLAUDE.md, then re-run
the two-worker test from the smoke test (one routine logging question, one destructive DB
question). Confirm the master now uses `wait_for_worker` + `answer_worker_question` (visible
as maniple tool calls, not `tmux capture-pane`), auto-answers the routine one, and escalates
the destructive one.

Expected: same correct outcome as the original smoke test, but faster and without pane
scraping.

- [ ] **Step 3: Commit (master workspace is not the maniple repo — note only)**

The master workspace lives outside the maniple repo and may not be version controlled.
If it is a git repo, commit the CLAUDE.md change there; otherwise no commit is needed.

---

## Task 7: Open the upstream PR

**Files:** none (git/gh only)

- [ ] **Step 1: Push the branch**

```bash
cd ~/code/maniple && git push -u origin feature/blocked-question-detection
```

(If `origin` is the read-only upstream, first `gh repo fork --remote` or add your fork as
`origin` and push there.)

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Detect and answer blocked AskUserQuestion prompts" \
  --body "Adds find_pending_question (JSONL scan), wait_for_worker (idle|waiting_input|stuck), answer_worker_question, and a worker_waiting_input event. Lets a manager session detect a worker blocked on a multiple-choice prompt (which stop-hook idle detection misses) and answer single-select questions by option number. multiSelect/multi-question escalate. See docs/superpowers/specs/2026-05-31-maniple-blocked-question-detection-design.md."
```

Expected: PR URL printed.

---

## Self-Review

**Spec coverage:**
- Detection (`pending_question`) → Task 1. ✓
- `wait_for_worker` (idle｜waiting_input｜stuck) → Task 4. ✓
- `answer_worker_question` by number + race guard → Task 2. ✓
- `worker_waiting_input` event → Task 3. ✓
- v1 escalate rules (multiSelect / multi-question / custom) → Task 1 sets `answerable/reason`; Task 2 refuses and tells master to escalate; Task 6 wires it. ✓
- Testing: unit fixtures (Task 1), e2e via smoke harness (Task 5), regression (`pytest tests/` in Tasks 3–4). ✓
- In-tree + upstream → Tasks on `feature/blocked-question-detection`; Task 7 PR. ✓
- Deferred TODOs (rules fast-path, multiSelect, "Type something", iTerm2 parity) → out of scope, recorded in the spec. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows complete code with verified symbol names (`get_events_path`, `read_events_since`, `registry.resolve`, `session.get_jsonl_path`, `backend.send_text`, etc.).

**Type consistency:** `find_pending_question` returns the dict consumed by `validate_answer_index`, `answer_worker_question`, and `wait_for_worker` (keys: `tool_use_id`, `question`, `header`, `multiSelect`, `options`, `num_questions`, `answerable`, `reason`). `WorkerEvent(ts, type, worker_id, data)` matches `events.py`. Session accessors used (`registry.resolve`, `session.get_jsonl_path`, `session.is_idle`, `session.session_id`, `session.terminal_session`, `app_ctx.terminal_backend`, `backend.send_text`) match the existing tools.
