"""Tests for the adaptive, load-aware stagger between worker launches.

spawn_workers used to start all workers' agent processes ~simultaneously via
asyncio.gather, which spiked system load (each worker session spawns a dozen+
MCP server processes). Before each gap between worker launches, spawn_workers
now reads the current 1-min load average and picks a per-gap delay:
- load >= config.spawn.stagger_load_threshold: the full stagger_max_seconds.
- otherwise: an exponential ramp by gap index, capped at stagger_max_seconds.
A per-call `stagger_seconds` parameter on the tool overrides this with a flat
delay for every gap (0 disables the stagger entirely).

These tests mock both asyncio.sleep and os.getloadavg -- never real sleeps or
real machine load.
"""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

import maniple_mcp.session_state as session_state
from maniple_mcp.config import DefaultsConfig, SpawnConfig, default_config
from maniple_mcp.coordinator_identity import CoordinatorIdentity
from maniple_mcp.registry import SessionRegistry
from maniple_mcp.tools import spawn_workers as spawn_workers_module
from tests.test_spawn_workers_defaults import FakeBackend


@pytest.fixture(autouse=True)
def _empty_coordinator_identity(monkeypatch):
    monkeypatch.setattr(
        spawn_workers_module, "get_coordinator_identity", lambda: CoordinatorIdentity()
    )
    monkeypatch.setattr(spawn_workers_module, "write_worker_manifest", lambda **kwargs: None)


async def _run_spawn(
    tmp_path,
    monkeypatch,
    *,
    worker_count: int,
    spawn_config: SpawnConfig | None = None,
    stagger_seconds: float | None = None,
    load_sequence: list[float] | None = None,
):
    """Spawn `worker_count` claude workers against a FakeBackend, mocking
    asyncio.sleep and os.getloadavg. Returns (result, sleep_calls, load_calls)."""

    config = default_config()
    config.defaults = DefaultsConfig(
        agent_type="claude", skip_permissions=False, use_worktree=False, layout="new",
    )
    if spawn_config is not None:
        config.spawn = spawn_config
    monkeypatch.setattr(spawn_workers_module, "load_config", lambda: config)
    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda *_: "cli:claude")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
    monkeypatch.setattr(
        spawn_workers_module, "generate_worker_prompt", lambda *args, **kwargs: "PROMPT"
    )
    monkeypatch.setattr(
        spawn_workers_module,
        "get_coordinator_guidance",
        lambda *args, **kwargs: "GUIDANCE",
    )

    async def fake_await_marker_in_jsonl(*args, **kwargs):
        return None

    monkeypatch.setattr(session_state, "await_marker_in_jsonl", fake_await_marker_in_jsonl)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *args, **kwargs: "MARKER")

    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(spawn_workers_module.asyncio, "sleep", fake_sleep)

    load_calls = {"count": 0}
    sequence = list(load_sequence or [])

    def fake_getloadavg():
        idx = min(load_calls["count"], len(sequence) - 1) if sequence else 0
        load_calls["count"] += 1
        value = sequence[idx] if sequence else 0.0
        return (value, value, value)

    monkeypatch.setattr(spawn_workers_module.os, "getloadavg", fake_getloadavg)

    backend = FakeBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    workers = [
        {"project_path": str(repo_path), "name": f"Worker{i}"} for i in range(worker_count)
    ]

    payload = {"workers": workers}
    if stagger_seconds is not None:
        payload["stagger_seconds"] = stagger_seconds

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run(payload, context=ctx)
    return result, sleep_calls, load_calls["count"]


@pytest.mark.asyncio
async def test_low_load_ramp_produces_exponential_delays(tmp_path, monkeypatch):
    """Low load (< threshold): delays ramp 4s, 8s, 16s... capped at
    stagger_max_seconds, for successive gaps. (spawn_workers caps a single
    call at 4 workers -- MAX_PANES_PER_TAB -- so this exercises 3 gaps: the
    third already hits the cap, proving both the ramp and the cap.)"""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=4,
        spawn_config=spawn_config,
        load_sequence=[1.0, 1.0, 1.0],
    )

    assert sleep_calls == [4, 8, 16]
    assert result["count"] == 4


@pytest.mark.asyncio
async def test_high_load_produces_flat_max_delay(tmp_path, monkeypatch):
    """Load >= threshold: every gap uses the full stagger_max_seconds delay,
    not the ramp."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=4,
        spawn_config=spawn_config,
        load_sequence=[23.1, 23.1, 23.1],
    )

    assert sleep_calls == [16, 16, 16]
    assert result["count"] == 4


@pytest.mark.asyncio
async def test_load_crossing_threshold_switches_delay_type_per_gap(tmp_path, monkeypatch):
    """getloadavg is read fresh for each gap (not cached): load starts low
    (ramp applies) then crosses the threshold mid-spawn (flat max applies to
    the remaining gaps)."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=4,
        spawn_config=spawn_config,
        load_sequence=[1.0, 12.0, 12.0],
    )

    # gap0: load 1.0 (< 10) -> ramp 4s; gap1: load 12.0 (>= 10) -> flat 16s;
    # gap2: load 12.0 (>= 10) -> flat 16s.
    assert sleep_calls == [4, 16, 16]
    assert load_calls == 3


@pytest.mark.asyncio
async def test_explicit_override_wins_over_ramp_and_load(tmp_path, monkeypatch):
    """An explicit stagger_seconds on the tool call forces a flat delay for
    every gap, regardless of load or the configured ramp/threshold, and
    os.getloadavg is never consulted."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=3,
        spawn_config=spawn_config,
        stagger_seconds=3,
        load_sequence=[999.0, 999.0],  # would force flat-max under the adaptive path
    )

    assert sleep_calls == [3, 3]
    assert load_calls == 0


@pytest.mark.asyncio
async def test_zero_override_disables_stagger_entirely(tmp_path, monkeypatch):
    """stagger_seconds=0 disables the stagger completely: no sleep calls at
    all, and every worker is still launched."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=3,
        spawn_config=spawn_config,
        stagger_seconds=0,
        load_sequence=[1.0],
    )

    assert sleep_calls == []
    assert result["count"] == 3


@pytest.mark.asyncio
async def test_no_trailing_delay_after_last_worker(tmp_path, monkeypatch):
    """For N workers there are only N-1 gaps: no sleep is issued after the
    last worker's launch is initiated."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=3,
        spawn_config=spawn_config,
        load_sequence=[1.0, 1.0],
    )

    assert len(sleep_calls) == 2
    assert result["count"] == 3


@pytest.mark.asyncio
async def test_single_worker_has_no_gaps_no_stagger(tmp_path, monkeypatch):
    """A single worker has zero gaps: no sleep, no getloadavg call."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path, monkeypatch, worker_count=1, spawn_config=spawn_config,
    )

    assert sleep_calls == []
    assert load_calls == 0
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_result_reports_stagger_summary(tmp_path, monkeypatch):
    """The spawn result surfaces what staggering happened, so the coordinator
    session can see it."""
    spawn_config = SpawnConfig(stagger_load_threshold=10.0, stagger_max_seconds=16)
    result, sleep_calls, load_calls = await _run_spawn(
        tmp_path,
        monkeypatch,
        worker_count=3,
        spawn_config=spawn_config,
        load_sequence=[1.0, 1.0],
    )

    assert "staggered" in result["coordinator_guidance"].lower()
