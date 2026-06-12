"""
Answer worker question tool.

Answers a worker blocked on an AskUserQuestion prompt — both single-select and
multiSelect variants.

Single-select: the caller supplies ``option_index`` (1-based). Sending one
digit selects AND submits, so no Enter is required.

multiSelect: the caller supplies ``option_indices`` (list of 1-based ints).
The TUI does not auto-submit, so we drive a full keystroke sequence:

  1. For each chosen index i: send str(i) — toggles that option's checkbox.
     (Cursor does not move after a toggle.)
  2. Navigate to "Submit": send Down arrow (N+1) times.
     The modal shows N real options, then a "Type something" free-entry row, then
     "Submit", so we need N+1 Down presses to reach Submit from the first option.
  3. Send Enter to open the "Review your answers / Ready to submit?" screen.
  4. Send "1" to confirm ("1. Submit answers").

Enter is \\x0d (carriage return). Down arrow is \\x1b[B.

Both backends (tmux and iTerm) accept raw escape sequences via ``send_text``:
- iTerm: ``session.async_send_text(text)`` writes directly to the pty.
- tmux: ``send-keys -t <pane_id> -l <text>`` sends literal bytes that the
  worker's TUI interprets as ANSI escape sequences.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..utils.errors import error_response, HINTS

# ---------------------------------------------------------------------------
# Pending-question detection (mirrors session_state.py in the main branch;
# defined locally here so this module does not require that branch's additions).
# ---------------------------------------------------------------------------

# Directory where worker hook writes pending-question markers.
# MUST match session_state.PENDING_DIR when that module is extended.
# Tests monkeypatch this attribute to redirect marker reads to tmp_path.
PENDING_DIR: Path = Path.home() / ".maniple" / "pending"

DOWN = "\x1b[B"   # ANSI cursor-down / Down-arrow
ENTER = "\x0d"    # Carriage return — the actual Enter keypress


def _find_pending_question(marker_id: str) -> Optional[dict]:
    """Return the pending AskUserQuestion for a worker, or None."""
    import sys as _sys
    _self = _sys.modules[__name__]
    marker = _self.PENDING_DIR / f"{marker_id}.json"
    try:
        payload = json.loads(marker.read_text())
    except (OSError, FileNotFoundError, ValueError):
        return None

    tool_input = payload.get("tool_input") or {}
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
        "tool_use_id": payload.get("tool_use_id", ""),
        "question": first.get("question", ""),
        "header": first.get("header", ""),
        "multiSelect": multi,
        "options": options,
        "num_questions": num,
        "answerable": answerable,
        "reason": reason,
    }


def _validate_answer_index(question: dict, option_index: int) -> Optional[str]:
    """Return None if option_index (1-based) is a valid single-select answer."""
    if not question.get("answerable", False):
        return f"not answerable ({question.get('reason')})"
    n = len(question.get("options") or [])
    if not isinstance(option_index, int) or option_index < 1 or option_index > n:
        return f"option_index {option_index} out of range 1..{n}"
    return None


def _validate_multiselect_indices(question: dict, option_indices: list[int]) -> Optional[str]:
    """
    Validate a list of 1-based option indices for a multiSelect question.

    Returns None on success, or an error string describing the first problem.
    """
    n = len(question.get("options") or [])
    if not option_indices:
        return "option_indices must be non-empty"
    for idx in option_indices:
        if not isinstance(idx, int) or idx < 1 or idx > n:
            return f"option_index {idx} out of range 1..{n}"
    return None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_tools(mcp: FastMCP) -> None:
    """Register answer_worker_question tool on the MCP server."""

    @mcp.tool()
    async def answer_worker_question(
        ctx: Context[ServerSession, "AppContext"],
        session_id: str,
        option_index: int | None = None,
        option_indices: list[int] | None = None,
        expected_tool_use_id: str | None = None,
    ) -> dict:
        """
        Answer a worker blocked on an AskUserQuestion.

        Supports both single-select and multiSelect questions.

        For single-select, supply ``option_index`` (1-based). The number key
        selects and submits simultaneously — no Enter is sent.

        For multiSelect, supply ``option_indices`` (list of 1-based ints). The
        tool drives the full keystroke sequence: toggle each chosen option, then
        navigate to Submit, press Enter to reach the confirmation screen, and
        send "1" to confirm.

        Use ``wait_for_worker`` first to learn the question type, options, and
        ``tool_use_id``.

        Args:
            session_id: Worker to answer. Accepts internal ID, terminal ID, or name.
            option_index: 1-based index for a single-select question.
            option_indices: 1-based indices for a multiSelect question.
            expected_tool_use_id: If given, abort unless the worker is still
                blocked on this exact question (race guard).

        Returns:
            For single-select: {success, session_id, option_index, chosen_label}
            For multiSelect:   {success, session_id, option_indices, chosen_labels}
        """
        app_ctx = ctx.request_context.lifespan_context
        registry = app_ctx.registry
        backend = app_ctx.terminal_backend

        session = registry.resolve(session_id)
        if not session:
            return error_response(
                f"Session not found: {session_id}", hint=HINTS["session_not_found"]
            )

        question = _find_pending_question(session.session_id)
        if question is None:
            return error_response(
                f"{session_id} is not blocked on a question right now."
            )

        if expected_tool_use_id and question["tool_use_id"] != expected_tool_use_id:
            return error_response(
                "Worker has moved on (tool_use_id changed); not answering a stale question.",
            )

        is_multi = question.get("multiSelect", False)

        # --- Validate parameter combination ---
        if option_index is None and option_indices is None:
            return error_response(
                "Provide option_index for single-select questions, or "
                "option_indices for multiSelect questions."
            )

        if not is_multi:
            # Single-select question
            if option_indices is not None:
                return error_response(
                    "option_indices is for multiSelect questions; this is a "
                    "single-select question. Use option_index instead."
                )
            if option_index is None:
                return error_response("option_index is required for single-select questions.")

            err = _validate_answer_index(question, option_index)
            if err:
                return error_response(
                    f"Cannot answer {session_id}: {err}. "
                    "Escalate to the human instead."
                )

            # Number key selects AND submits in the single-select menu; no Enter.
            await backend.send_text(session.terminal_session, str(option_index))
            return {
                "success": True,
                "session_id": session.session_id,
                "option_index": option_index,
                "chosen_label": question["options"][option_index - 1]["label"],
            }

        else:
            # multiSelect question
            if option_index is not None:
                return error_response(
                    "option_index is for single-select questions; this is a "
                    "multiSelect question. Use option_indices instead."
                )
            if option_indices is None:
                return error_response("option_indices is required for multiSelect questions.")

            err = _validate_multiselect_indices(question, option_indices)
            if err:
                return error_response(
                    f"Cannot answer {session_id}: {err}. "
                    "Escalate to the human instead."
                )

            n = len(question["options"])
            term = session.terminal_session
            chosen_labels = [question["options"][i - 1]["label"] for i in option_indices]

            # Step 1: Toggle each chosen option by sending its digit.
            for idx in option_indices:
                await backend.send_text(term, str(idx))

            # Step 2: Navigate to Submit.
            # The modal lists N real options, then "Type something", then "Submit".
            # From option-1's position, we need N+1 Down presses to reach Submit.
            for _ in range(n + 1):
                await backend.send_text(term, DOWN)

            # Step 3: Press Enter to open the confirmation screen.
            await backend.send_text(term, ENTER)

            # Step 4: Send "1" to confirm ("1. Submit answers").
            await backend.send_text(term, "1")

            return {
                "success": True,
                "session_id": session.session_id,
                "option_indices": option_indices,
                "chosen_labels": chosen_labels,
            }
