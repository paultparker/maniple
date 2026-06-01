"""
Tests for multiSelect answer support in answer_worker_question.

TDD tests written BEFORE implementation. These tests define the expected behavior
for answering a multiSelect AskUserQuestion via keystroke sequence.

Keystroke sequence for a multiSelect with N options (1-based indices):
1. For each chosen index i: send str(i) to toggle its checkbox
2. Navigate to "Submit": send Down arrow (N+1) times
3. Send Enter to reach the "Review / Ready to submit?" confirmation screen
4. Send "1" to confirm submission

Down arrow escape sequence: \\x1b[B
Enter: \\x0d

Both backends accept raw escape sequences via send_text:
- iTerm: async_send_text passes bytes straight to the pty
- tmux: send-keys -l sends literal text, which the pane TUI interprets as escape sequences
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest


def _load_tool_module(name: str):
    """
    Load a tool module by file path, bypassing tools/__init__.py to avoid
    a pre-existing circular import (registry → terminal_backends → utils →
    errors → registry) that only resolves when the full test suite runs and
    another test has already forced registry.py to finish loading.
    """
    here = Path(__file__).parent.parent / "src" / "maniple_mcp" / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"maniple_mcp.tools.{name}", str(here)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"maniple_mcp.tools.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


awq_module = _load_tool_module("answer_worker_question")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOWN = "\x1b[B"
ENTER = "\x0d"


def _write_marker(tmp_path: Path, monkeypatch, marker_id: str, payload: dict) -> None:
    # Patch PENDING_DIR on the tool module itself (not session_state, since
    # PENDING_DIR is defined locally in answer_worker_question.py for this worktree).
    monkeypatch.setattr(awq_module, "PENDING_DIR", tmp_path)
    (tmp_path / f"{marker_id}.json").write_text(json.dumps(payload))


def _single_select_payload(tool_use_id="toolu_single"):
    return {
        "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {
            "questions": [{
                "question": "Which option?",
                "header": "Pick one",
                "multiSelect": False,
                "options": [
                    {"label": "Alpha", "description": ""},
                    {"label": "Beta", "description": ""},
                    {"label": "Gamma", "description": ""},
                ],
            }]
        },
    }


def _multiselect_payload(tool_use_id="toolu_multi", n_options=4):
    option_labels = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    return {
        "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {
            "questions": [{
                "question": "Which options do you want?",
                "header": "Pick many",
                "multiSelect": True,
                "options": [
                    {"label": option_labels[i], "description": ""}
                    for i in range(n_options)
                ],
            }]
        },
    }


def _capture_tool(module):
    """Capture the function registered via @mcp.tool()."""
    captured = {}

    def capture():
        def decorator(func):
            captured["func"] = func
            return func
        return decorator

    mcp = MagicMock()
    mcp.tool = capture
    module.register_tools(mcp)
    return captured["func"]


def _ctx(session_id="w1", backend=None):
    """Build a fake MCP context with a registry and backend."""
    terminal_session = MagicMock()
    terminal_session.session_id = session_id

    session = MagicMock()
    session.session_id = session_id
    session.terminal_session = terminal_session

    registry = MagicMock()
    registry.resolve.return_value = session

    if backend is None:
        backend = MagicMock()
        backend.send_text = AsyncMock()

    ctx = MagicMock()
    ctx.request_context.lifespan_context.registry = registry
    ctx.request_context.lifespan_context.terminal_backend = backend
    return ctx, session, backend


# ---------------------------------------------------------------------------
# Existing single-select behavior: MUST keep working unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_select_sends_digit_only(tmp_path, monkeypatch):
    """Single-select: send one digit, no Enter."""
    _write_marker(tmp_path, monkeypatch, "w1", _single_select_payload())
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_index=2)

    assert result.get("success") is True
    assert result["option_index"] == 2
    assert result["chosen_label"] == "Beta"
    # Exactly one send_text call with the digit "2"
    backend.send_text.assert_awaited_once_with(session.terminal_session, "2")


@pytest.mark.asyncio
async def test_single_select_race_guard(tmp_path, monkeypatch):
    """Race guard: stale expected_tool_use_id aborts the answer."""
    _write_marker(tmp_path, monkeypatch, "w1", _single_select_payload("toolu_single"))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_index=1, expected_tool_use_id="toolu_stale")

    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_select_out_of_range(tmp_path, monkeypatch):
    """Out-of-range option_index returns error."""
    _write_marker(tmp_path, monkeypatch, "w1", _single_select_payload())
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_index=99)

    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_blocked_returns_error(tmp_path, monkeypatch):
    """When no pending question, return error."""
    monkeypatch.setattr(awq_module, "PENDING_DIR", tmp_path)  # empty dir
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_index=1)

    assert "error" in result
    backend.send_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# NEW: multiSelect validation — reject wrong parameter combination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_option_index_rejected_for_multiselect(tmp_path, monkeypatch):
    """Passing option_index (single-select param) to a multiSelect question is an error."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload())
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_index=1)

    assert "error" in result
    assert "multiSelect" in result["error"].lower() or "option_indices" in result["error"].lower()
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_indices_rejected_for_single_select(tmp_path, monkeypatch):
    """Passing option_indices (multiSelect param) to a single-select question is an error."""
    _write_marker(tmp_path, monkeypatch, "w1", _single_select_payload())
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[1, 2])

    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_indices_out_of_range(tmp_path, monkeypatch):
    """Index 0 or > N is rejected for multiSelect."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload(n_options=4))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    # Index 0 is out of range (1-based)
    result = await tool(ctx, session_id="w1", option_indices=[0, 2])
    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_indices_too_high(tmp_path, monkeypatch):
    """Index > N is rejected for multiSelect."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload(n_options=4))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    # Index 5 is out of range for 4 options
    result = await tool(ctx, session_id="w1", option_indices=[1, 5])
    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_neither_param_provided_returns_error(tmp_path, monkeypatch):
    """Neither option_index nor option_indices provided is an error."""
    _write_marker(tmp_path, monkeypatch, "w1", _single_select_payload())
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1")

    assert "error" in result
    backend.send_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# NEW: multiSelect keystroke sequence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiselect_keystroke_sequence_4options_indices_3_4(tmp_path, monkeypatch):
    """
    For N=4 options, choosing indices [3, 4]:
    Sequence: "3", "4", Down×5, Enter, "1"

    Down×5 because: from current position (no cursor movement after toggle),
    we need to navigate to Submit which is N+1=5 positions down from option 1.
    """
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload(n_options=4))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[3, 4])

    assert result.get("success") is True
    assert result["option_indices"] == [3, 4]
    assert "Alpha" not in result["chosen_labels"]
    assert "Gamma" in result["chosen_labels"]
    assert "Delta" in result["chosen_labels"]

    calls = backend.send_text.call_args_list
    # Build expected sequence
    expected = [
        call(session.terminal_session, "3"),
        call(session.terminal_session, "4"),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, ENTER),
        call(session.terminal_session, "1"),
    ]
    assert calls == expected


@pytest.mark.asyncio
async def test_multiselect_keystroke_sequence_2options_index_1(tmp_path, monkeypatch):
    """
    For N=2 options, choosing index [1]:
    Sequence: "1", Down×3, Enter, "1"
    (N+1 = 3 Downs)
    """
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu_2opt",
        "tool_input": {
            "questions": [{
                "question": "Two options?",
                "header": "H",
                "multiSelect": True,
                "options": [
                    {"label": "Yes", "description": ""},
                    {"label": "No", "description": ""},
                ],
            }]
        },
    }
    _write_marker(tmp_path, monkeypatch, "w1", payload)
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[1])

    assert result.get("success") is True
    assert result["chosen_labels"] == ["Yes"]

    calls = backend.send_text.call_args_list
    expected = [
        call(session.terminal_session, "1"),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, ENTER),
        call(session.terminal_session, "1"),
    ]
    assert calls == expected


@pytest.mark.asyncio
async def test_multiselect_keystroke_sequence_3options_indices_1_2_3(tmp_path, monkeypatch):
    """
    For N=3 options, choosing all [1, 2, 3]:
    Sequence: "1", "2", "3", Down×4, Enter, "1"
    (N+1 = 4 Downs)
    """
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload(n_options=3))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[1, 2, 3])

    assert result.get("success") is True
    assert set(result["chosen_labels"]) == {"Alpha", "Beta", "Gamma"}

    calls = backend.send_text.call_args_list
    expected = [
        call(session.terminal_session, "1"),
        call(session.terminal_session, "2"),
        call(session.terminal_session, "3"),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, DOWN),
        call(session.terminal_session, ENTER),
        call(session.terminal_session, "1"),
    ]
    assert calls == expected


@pytest.mark.asyncio
async def test_multiselect_race_guard(tmp_path, monkeypatch):
    """Race guard fires for multiSelect too."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload("toolu_multi"))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[1, 2], expected_tool_use_id="toolu_stale")

    assert "error" in result
    backend.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiselect_race_guard_passes_when_id_matches(tmp_path, monkeypatch):
    """Race guard passes when expected_tool_use_id matches."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload("toolu_multi"))
    ctx, session, backend = _ctx()
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="w1", option_indices=[1], expected_tool_use_id="toolu_multi")

    assert result.get("success") is True


@pytest.mark.asyncio
async def test_multiselect_session_not_found(tmp_path, monkeypatch):
    """Session not found returns error."""
    _write_marker(tmp_path, monkeypatch, "w1", _multiselect_payload())
    ctx, session, backend = _ctx()
    ctx.request_context.lifespan_context.registry.resolve.return_value = None
    tool = _capture_tool(awq_module)

    result = await tool(ctx, session_id="missing", option_indices=[1])

    assert "error" in result
    backend.send_text.assert_not_awaited()
