"""CLI for `maniple zombies` -- zombie worker report (component 5, spec v1).

Report-only v1: no reaping, no kill flag. Sources manifests in ~/.maniple/workers/
and best-effort ps scan (worker-<id>.json pattern) for pre-feature workers with
unknown coordinators. Classifies: orphaned (coordinator dead/defunct),
forgotten (coordinator alive, idle >= threshold), ok, closed.
Output: human table + per-zombie action blocks + --json flag.
Exit code: 0 always (report tool).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from .config import load_config


WorkerZombieClass = Literal["orphaned", "forgotten", "ok", "closed"]


@dataclass
class WorkerZombieStatus:
    """Status of a single worker from the zombies report."""

    session_id: str
    name: str
    agent_type: str
    idle_age_hours: float
    terminal_id: Optional[str]
    terminal_attached: bool
    coordinator_pid: Optional[int]
    coordinator_pid_start: Optional[str]
    coordinator_session_id: Optional[str]
    coordinator_alive: bool
    coordinator_defunct: bool  # PID alive but start-time mismatch
    project_dir: Optional[str]
    class_: WorkerZombieClass
    closed_at: Optional[str]


def _get_process_start_time(pid: int) -> Optional[str]:
    """Get process start time for PID via ps command."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _coordinator_is_alive(pid: int, recorded_start_time: Optional[str]) -> tuple[bool, bool]:
    """Check if coordinator process is alive (and not defunct via PID reuse).

    Returns:
        (alive, defunct) tuple. alive=True means process exists; defunct=True means
        process exists but start-time doesn't match (PID was reused).
    """
    try:
        os.kill(pid, 0)  # Check without sending signal
    except ProcessLookupError:
        return False, False
    except OSError:
        return False, False

    # Process exists. Check if it's the same one (start time match).
    if recorded_start_time is None:
        # No recorded start time, can't verify. Assume it's the same.
        return True, False

    current_start = _get_process_start_time(pid)
    if current_start is None:
        # Can't get current start time. Assume it's still the same.
        return True, False

    # Both times available: compare
    if current_start == recorded_start_time:
        return True, False
    else:
        # PID reused (different start time)
        return False, True


def get_idle_age(
    session_id: str,
    spawned_at: Optional[str] = None,
    project_path: Optional[str] = None,
) -> float:
    """Get idle age in hours from JSONL mtime or manifest spawned_at.

    Tries JSONL mtime first (via session_state); falls back to manifest
    spawned_at timestamp. Returns hours since last activity.
    """
    # Try to find JSONL mtime
    if project_path:
        try:
            from .session_state import get_project_dir, list_sessions

            project_dir = get_project_dir(project_path)
            for sid, jsonl_path, mtime in list_sessions(project_path):
                if sid == session_id:
                    age_seconds = time.time() - mtime
                    return age_seconds / 3600
        except Exception:
            pass

    # Fall back to spawned_at
    if spawned_at:
        try:
            spawned = datetime.fromisoformat(spawned_at)
            age = datetime.now() - spawned
            return age.total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    # No data; assume very recent (0 hours)
    return 0.0


def classify_worker(
    worker: dict, idle_threshold_hours: float = 2.0
) -> WorkerZombieClass:
    """Classify a worker as orphaned/forgotten/ok/closed.

    A worker is:
    - `closed` if it has a closed_at timestamp (regardless of coordinator state)
    - `orphaned` if coordinator is dead/defunct (regardless of idle time)
    - `forgotten` if coordinator is alive but worker is idle >= threshold
    - `ok` if coordinator is alive and worker is idle < threshold
    """
    # Closed takes precedence
    if worker.get("closed_at"):
        return "closed"

    coordinator_pid = worker.get("coordinator_pid")
    coordinator_pid_start = worker.get("coordinator_pid_start")
    idle_age_hours = worker.get("idle_age_hours", 0.0)

    # No coordinator info: treat as orphaned (unknown coordinator)
    if not coordinator_pid:
        return "orphaned"

    # Check coordinator liveness
    alive, defunct = _coordinator_is_alive(coordinator_pid, coordinator_pid_start)

    if not alive or defunct:
        return "orphaned"

    # Coordinator alive. Check idle age.
    if idle_age_hours >= idle_threshold_hours:
        return "forgotten"

    return "ok"


def discover_workers(workers_dir: Optional[Path] = None) -> list[dict]:
    """Discover workers from manifest files and optional ps scan.

    Sources:
    1. ~/.maniple/workers/*.json manifests (required)
    2. Best-effort ps scan for worker processes (MANIPLE_WORKER=1 env)
       to catch pre-feature workers (coordinator UNKNOWN)

    Returns:
        List of worker dicts, each with at minimum: session_id, name,
        agent_type, idle_age_hours, terminal_id, coordinator_pid,
        coordinator_alive, class_
    """
    if workers_dir is None:
        workers_dir = Path.home() / ".maniple" / "workers"

    workers_by_id: dict[str, dict] = {}

    # 1. Read manifests
    if workers_dir.exists():
        for manifest_file in workers_dir.glob("*.json"):
            try:
                data = json.loads(manifest_file.read_text())
                worker_info = data.get("worker", {})
                coordinator_info = data.get("coordinator", {})
                session_id = worker_info.get("session_id")

                if not session_id:
                    continue

                # Compute idle age
                project_path = worker_info.get("project_path")
                spawned_at = worker_info.get("spawned_at")
                idle_age = get_idle_age(session_id, spawned_at, project_path)

                # Check coordinator liveness
                coor_pid = coordinator_info.get("pid")
                coor_pid_start = coordinator_info.get("pid_start")
                coor_alive, coor_defunct = _coordinator_is_alive(coor_pid, coor_pid_start)

                # Build worker dict
                worker = {
                    "session_id": session_id,
                    "name": worker_info.get("name", session_id),
                    "agent_type": worker_info.get("agent_type", "claude"),
                    "idle_age_hours": idle_age,
                    "terminal_id": worker_info.get("terminal_id"),
                    "coordinator_pid": coor_pid,
                    "coordinator_pid_start": coor_pid_start,
                    "coordinator_session_id": coordinator_info.get("session_id"),
                    "coordinator_alive": coor_alive and not coor_defunct,
                    "coordinator_defunct": coor_defunct,
                    "project_dir": coordinator_info.get("project_dir"),
                    "closed_at": data.get("closed_at"),
                }

                # Classify
                config = load_config()
                threshold = config.zombies.idle_threshold_hours
                worker["class"] = classify_worker(worker, threshold)

                workers_by_id[session_id] = worker

            except (json.JSONDecodeError, KeyError, TypeError):
                # Manifest is malformed; skip (best-effort)
                continue

    # 2. Best-effort ps scan for pre-feature workers
    # Look for processes with --settings .../worker-<id>.json pattern
    # (Not implemented in v1 for scope; placeholder for future)

    return list(workers_by_id.values())


def format_zombies_report(workers: list[dict], as_json: bool = False) -> str:
    """Format zombies report as human table or JSON.

    Human output: table + per-zombie action blocks.
    JSON output: structured dict with workers array + metadata.
    """
    if as_json:
        return _format_json_report(workers)
    else:
        return _format_human_report(workers)


def _format_human_report(workers: list[dict]) -> str:
    """Format as human-readable table + action blocks."""
    if not workers:
        return "No workers found.\n"

    # Sort: orphaned/forgotten first (interesting), then ok/closed
    class_order = {"orphaned": 0, "forgotten": 1, "ok": 2, "closed": 3}
    sorted_workers = sorted(
        workers, key=lambda w: (class_order.get(w.get("class"), 99), w.get("name", ""))
    )

    # Build table
    lines = []
    lines.append("=== Zombie Workers Report ===\n")
    lines.append(
        f"{'Name':<20} {'Class':<12} {'Agent':<8} {'Idle (h)':<10} {'Attached':<10} {'Coordinator':<15}"
    )
    lines.append("-" * 95)

    for worker in sorted_workers:
        name = worker.get("name", "?")[:19]
        class_ = worker.get("class", "?")[:11]
        agent = worker.get("agent_type", "?")[:7]
        idle = f"{worker.get('idle_age_hours', 0):.1f}"[:9]
        attached = "yes" if worker.get("terminal_id") else "no"
        alive = "alive" if worker.get("coordinator_alive") else "dead"
        lines.append(
            f"{name:<20} {class_:<12} {agent:<8} {idle:<10} {attached:<10} {alive:<15}"
        )

    lines.append("\n=== Action Blocks ===\n")

    # Per-zombie action blocks
    for worker in sorted_workers:
        session_id = worker.get("session_id", "?")
        name = worker.get("name", "?")
        class_ = worker.get("class", "?")
        idle_hours = worker.get("idle_age_hours", 0)

        lines.append(f"**{name}** ({session_id})")
        lines.append(f"Status: {class_} (idle {idle_hours:.1f}h)")

        # Reconnect commands
        if worker.get("terminal_id"):
            tid = worker["terminal_id"]
            if tid.startswith("tmux:"):
                pane_id = tid.split(":", 1)[1]
                lines.append(f"Connect to worker: `tmux select-pane -t {pane_id}`")
            elif tid.startswith("iterm:"):
                session_uuid = tid.split(":", 1)[1]
                lines.append(
                    f"Connect to worker (iTerm): UUID `{session_uuid}` "
                    "(use iTerm window -> Show Session UUID)"
                )

        # Coordinator reconnect
        if worker.get("coordinator_alive"):
            # Alive: switch/attach
            lines.append(f"Coordinator is alive (PID {worker.get('coordinator_pid')})")
            lines.append("(Use tmux switch-client or claude --resume)")
        else:
            # Dead: resume
            project_dir = worker.get("project_dir")
            coord_sid = worker.get("coordinator_session_id")
            if project_dir and coord_sid:
                lines.append(
                    f"Resume coordinator: `cd {project_dir} && claude --resume {coord_sid}`"
                )

        lines.append("")

    return "\n".join(lines)


def _format_json_report(workers: list[dict]) -> str:
    """Format as JSON for programmatic consumption."""
    output = {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "workers": workers,
        "summary": {
            "total": len(workers),
            "orphaned": sum(1 for w in workers if w.get("class") == "orphaned"),
            "forgotten": sum(1 for w in workers if w.get("class") == "forgotten"),
            "ok": sum(1 for w in workers if w.get("class") == "ok"),
            "closed": sum(1 for w in workers if w.get("class") == "closed"),
        },
    }
    return json.dumps(output, indent=2)


__all__ = [
    "WorkerZombieClass",
    "WorkerZombieStatus",
    "classify_worker",
    "discover_workers",
    "format_zombies_report",
    "get_idle_age",
]
