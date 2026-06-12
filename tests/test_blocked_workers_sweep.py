"""Tests for marker-dir-wide blocked-worker detection.

Covers the fix for the "re-block after idle goes unnoticed" gap: detection must
sweep ALL registered workers (via the marker dir), not just a caller-supplied
session_ids subset.
"""

import json
from unittest.mock import MagicMock

import pytest

import maniple_mcp.session_state as ss
from maniple_mcp.session_state import find_blocked_workers
from maniple_mcp.tools import list_blocked_workers as lbw_module
from maniple_mcp.tools import wait_for_worker as wfw_module


def _write_marker(tmp_path, marker_id, multi=False):
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_use_id": f"toolu_{marker_id}",
        "tool_input": {"questions": [{
            "question": f"Question for {marker_id}?",
            "header": "H", "multiSelect": multi,
            "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
        }]},
    }
    (tmp_path / f"{marker_id}.json").write_text(json.dumps(payload))


def _fake_session(session_id, name=None, idle=False):
    s = MagicMock()
    s.session_id = session_id
    s.name = name or session_id
    s.get_jsonl_path.return_value = f"/fake/{session_id}.jsonl"
    s.is_idle.return_value = idle
    return s


# --- pure helper: find_blocked_workers -------------------------------------

def test_find_blocked_workers_returns_only_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    _write_marker(tmp_path, "w1")
    # w2 has no marker
    blocked = find_blocked_workers(["w1", "w2", "w3"])
    assert [b["session_id"] for b in blocked] == ["w1"]
    assert blocked[0]["question"]["tool_use_id"] == "toolu_w1"


def test_find_blocked_workers_empty_when_none_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    assert find_blocked_workers(["w1", "w2"]) == []


def test_find_blocked_workers_includes_unanswerable(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    _write_marker(tmp_path, "w1", multi=True)
    blocked = find_blocked_workers(["w1"])
    assert blocked[0]["question"]["answerable"] is False


# --- tool: list_blocked_workers --------------------------------------------

def _capture_tool(module):
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


def _ctx_with_registry(registry):
    ctx = MagicMock()
    ctx.request_context.lifespan_context.registry = registry
    return ctx


@pytest.mark.asyncio
async def test_list_blocked_workers_sweeps_whole_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    _write_marker(tmp_path, "blocked1")
    # registry has two workers; only one is blocked
    s_blocked = _fake_session("blocked1", name="db-worker")
    s_idle = _fake_session("idle1", name="calc-worker")
    registry = MagicMock()
    registry.list_all.return_value = [s_blocked, s_idle]

    tool = _capture_tool(lbw_module)
    result = await tool(_ctx_with_registry(registry))

    assert result["count"] == 1
    assert result["blocked"][0]["session_id"] == "blocked1"
    assert result["blocked"][0]["name"] == "db-worker"
    assert result["blocked"][0]["question"]["tool_use_id"] == "toolu_blocked1"


@pytest.mark.asyncio
async def test_list_blocked_workers_empty_when_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    registry = MagicMock()
    registry.list_all.return_value = [_fake_session("idle1")]

    tool = _capture_tool(lbw_module)
    result = await tool(_ctx_with_registry(registry))
    assert result["count"] == 0 and result["blocked"] == []


# --- wait_for_worker: sweep-all default ------------------------------------

@pytest.mark.asyncio
async def test_wait_for_worker_empty_session_ids_sweeps_all_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    _write_marker(tmp_path, "reblocked")
    s = _fake_session("reblocked", name="calc-worker")
    registry = MagicMock()
    registry.resolve.side_effect = lambda sid: s if sid == "reblocked" else None
    registry.list_all.return_value = [s]

    tool = _capture_tool(wfw_module)
    result = await tool(_ctx_with_registry(registry), session_ids=[], timeout=2.0, poll_interval=0.1)

    assert result["timed_out"] is False
    assert result["resolved"]["session_id"] == "reblocked"
    assert result["resolved"]["state"] == "waiting_input"
