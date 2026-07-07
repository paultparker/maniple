"""Tests for the override_usage_pause / clear_usage_override MCP tools.

Exercises the tool wrapper wiring (worker resolution, error handling,
result shape) against a real SessionRegistry -- the ladder-advance logic
itself is covered exhaustively in test_usage_override.py.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from maniple_mcp.registry import SessionRegistry
from maniple_mcp.tools import clear_usage_override as clear_usage_override_module
from maniple_mcp.tools import override_usage_pause as override_usage_pause_module
from maniple_mcp.tools import register_all_tools


def test_both_tools_are_registered_on_the_server():
    """Regression guard: the two tools must actually be wired into
    register_all_tools, not just importable -- a prior WIP commit added the
    imports but forgot the register_tools(mcp) calls, silently leaving both
    tools absent from the running server."""
    mcp = FastMCP("test")
    register_all_tools(mcp, ensure_connection=lambda: None)
    tool_names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert "override_usage_pause" in tool_names
    assert "clear_usage_override" in tool_names


@pytest.fixture()
def override_dir(tmp_path):
    d = tmp_path / "usage_override"
    d.mkdir()
    return d


def _build_tool(module, tool_name, registry, override_dir, monkeypatch):
    monkeypatch.setattr(
        "maniple_mcp.usage_override.default_override_dir", lambda: override_dir
    )
    mcp = FastMCP("test")
    module.register_tools(mcp)
    tool = mcp._tool_manager.get_tool(tool_name)
    app_ctx = SimpleNamespace(registry=registry)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    return tool, ctx


@pytest.fixture()
def registry():
    return SessionRegistry()


class TestOverrideUsagePause:
    @pytest.mark.asyncio
    async def test_unknown_worker_reports_error(self, registry, override_dir, monkeypatch):
        tool, ctx = _build_tool(
            override_usage_pause_module, "override_usage_pause", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["nonexistent"]}, context=ctx)

        assert result["results"]["nonexistent"]["error"]

    @pytest.mark.asyncio
    async def test_advances_worker_rung(self, registry, override_dir, monkeypatch):
        session = registry.add(MagicMock(), "/test/path", name="Groucho")
        tool, ctx = _build_tool(
            override_usage_pause_module, "override_usage_pause", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["Groucho"]}, context=ctx)

        entry = result["results"]["Groucho"]
        assert entry["new_rung"] == 0.90
        on_disk = json.loads((override_dir / f"{session.session_id}.json").read_text())
        assert on_disk["threshold"] == 0.90

    @pytest.mark.asyncio
    async def test_already_unlimited_reported_not_errored(
        self, registry, override_dir, monkeypatch
    ):
        session = registry.add(MagicMock(), "/test/path", name="Chico")
        (override_dir / f"{session.session_id}.json").write_text(
            json.dumps({"threshold": None, "expires_at": time.time() + 100})
        )
        tool, ctx = _build_tool(
            override_usage_pause_module, "override_usage_pause", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["Chico"]}, context=ctx)

        entry = result["results"]["Chico"]
        assert entry["new_rung"] == "unlimited"
        assert entry["already_unlimited"] is True

    @pytest.mark.asyncio
    async def test_expires_at_from_state_file(
        self, registry, override_dir, monkeypatch, tmp_path
    ):
        state_file = tmp_path / "state.json"
        resets_at = time.time() + 999
        state_file.write_text(
            json.dumps({"rate_limits": {"five_hour": {"resets_at": resets_at}}})
        )
        monkeypatch.setattr(
            override_usage_pause_module,
            "load_config",
            lambda: SimpleNamespace(
                usage_pause=SimpleNamespace(state_file=str(state_file))
            ),
        )
        registry.add(MagicMock(), "/test/path", name="Harpo")
        tool, ctx = _build_tool(
            override_usage_pause_module, "override_usage_pause", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["Harpo"]}, context=ctx)

        assert result["results"]["Harpo"]["expires_at"] == resets_at

    @pytest.mark.asyncio
    async def test_missing_state_file_falls_back_to_now_plus_5h(
        self, registry, override_dir, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            override_usage_pause_module,
            "load_config",
            lambda: SimpleNamespace(
                usage_pause=SimpleNamespace(state_file=str(tmp_path / "nope.json"))
            ),
        )
        registry.add(MagicMock(), "/test/path", name="Zeppo")
        tool, ctx = _build_tool(
            override_usage_pause_module, "override_usage_pause", registry, override_dir, monkeypatch
        )

        before = time.time()
        result = await tool.run({"workers": ["Zeppo"]}, context=ctx)
        after = time.time()

        expires_at = result["results"]["Zeppo"]["expires_at"]
        assert before + 5 * 3600 - 1 <= expires_at <= after + 5 * 3600 + 1


class TestClearUsageOverride:
    @pytest.mark.asyncio
    async def test_clears_worker_override(self, registry, override_dir, monkeypatch):
        session = registry.add(MagicMock(), "/test/path", name="Zeppo")
        (override_dir / f"{session.session_id}.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": time.time() + 100})
        )
        tool, ctx = _build_tool(
            clear_usage_override_module, "clear_usage_override", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["Zeppo"]}, context=ctx)

        assert result["results"]["Zeppo"]["cleared"] is True
        assert not (override_dir / f"{session.session_id}.json").exists()

    @pytest.mark.asyncio
    async def test_accepts_literal_global_scope(self, registry, override_dir, monkeypatch):
        (override_dir / "global.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": time.time() + 100})
        )
        tool, ctx = _build_tool(
            clear_usage_override_module, "clear_usage_override", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["global"]}, context=ctx)

        assert result["results"]["global"]["cleared"] is True

    @pytest.mark.asyncio
    async def test_unknown_worker_reports_error(self, registry, override_dir, monkeypatch):
        tool, ctx = _build_tool(
            clear_usage_override_module, "clear_usage_override", registry, override_dir, monkeypatch
        )

        result = await tool.run({"workers": ["nonexistent"]}, context=ctx)

        assert result["results"]["nonexistent"]["error"]
