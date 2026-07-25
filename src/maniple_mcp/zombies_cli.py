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
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Optional

from .config import ConfigError, load_config


WorkerZombieClass = Literal["orphaned", "forgotten", "ok", "closed"]

# Matches the `--settings .../worker-<id>.json` argv pattern every worker is
# launched with (see build_stop_hook_settings_file() in iterm_utils.py) --
# used for the best-effort ps scan that catches pre-feature workers (no
# manifest yet) and confirms manifest-sourced workers' processes are alive.
_SETTINGS_FILE_RE = re.compile(r"--settings\s+\S*/worker-([A-Za-z0-9._-]+)\.json")

TmuxRunner = Callable[[list], str]


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


def _coordinator_is_alive(
    pid: Optional[int], recorded_start_time: Optional[str]
) -> tuple[bool, bool]:
    """Check if coordinator process is alive (and not defunct via PID reuse).

    Returns:
        (alive, defunct) tuple. alive=True means process exists; defunct=True means
        process exists but start-time doesn't match (PID was reused).
    """
    if not pid:
        # No PID captured at all -- coordinator identity is unknown, not
        # verifiably alive. Caller treats this the same as dead (orphaned).
        return False, False

    try:
        os.kill(pid, 0)  # Check without sending signal
    except ProcessLookupError:
        return False, False
    except PermissionError:
        # Process exists but is owned by another user -- it is NOT dead,
        # just unverifiable further. Fall through to the start-time check.
        pass
    except OSError:
        # Unexpected error querying the PID; can't confirm death, so don't
        # falsely report a live coordinator as dead (best-effort/fail-open,
        # same philosophy as the pause hooks).
        return True, False

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
            from .session_state import list_sessions

            for sid, _jsonl_path, mtime in list_sessions(project_path):
                if sid == session_id:
                    age_seconds = time.time() - mtime
                    return age_seconds / 3600
        except Exception:
            pass

    # Fall back to spawned_at
    if spawned_at:
        try:
            spawned = datetime.fromisoformat(spawned_at)
            # Manifest spawned_at is UTC ISO (spec component 3), typically
            # with a trailing "Z" -- fromisoformat() parses that as
            # timezone-AWARE, while datetime.now() is naive. Subtracting a
            # naive datetime from an aware one raises TypeError, so match
            # awareness on whichever side spawned_at gave us.
            now = datetime.now(spawned.tzinfo) if spawned.tzinfo else datetime.now()
            age = now - spawned
            return age.total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    # No data; assume very recent (0 hours)
    return 0.0


def _run_tmux(args: list) -> str:
    """Shell out to the real `tmux` binary. Raises on failure/absence --
    callers are expected to catch (OSError, subprocess.SubprocessError)."""
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        raise subprocess.SubprocessError(result.stderr)
    return result.stdout


def _tmux_pane_session_name(
    pane_id: str, tmux_runner: Optional[TmuxRunner] = None
) -> Optional[str]:
    """Resolve a tmux pane_id (a worker's or coordinator's terminal native
    ID) to its current session_name, via `tmux list-panes -a`. None on any
    failure (tmux unavailable, pane no longer exists) -- best-effort."""
    runner = tmux_runner or _run_tmux
    try:
        output = runner(["list-panes", "-a", "-F", "#{pane_id} #{session_name}"])
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == pane_id:
            return parts[1]
    return None


def _tmux_attached(
    terminal_id: Optional[str], tmux_runner: Optional[TmuxRunner] = None
) -> Optional[bool]:
    """Is the worker's tmux session currently attached to a client?

    None means "not applicable / unknown" -- non-tmux terminal_id (e.g.
    iTerm), tmux unavailable, or the pane/session can no longer be
    resolved. Rendered as a flag column, not a classification (spec:
    "unattached shown as a flag column, not a class").
    """
    if not terminal_id or not terminal_id.startswith("tmux:"):
        return None
    pane_id = terminal_id.split(":", 1)[1]
    session_name = _tmux_pane_session_name(pane_id, tmux_runner=tmux_runner)
    if session_name is None:
        return None

    runner = tmux_runner or _run_tmux
    try:
        output = runner(["list-sessions", "-F", "#{session_name} #{session_attached}"])
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == session_name:
            return parts[1] != "0"
    return None


def _ps_scan(ps_output: Optional[str] = None) -> dict:
    """Best-effort scan of `ps -eo pid,command` output for worker processes,
    matched via the `--settings .../worker-<id>.json` argv pattern every
    worker is launched with. Returns {session_id: pid}.

    Used two ways by discover_workers(): (1) to confirm a manifest-sourced
    worker's process is actually still running (worker_alive), and (2) to
    discover pre-feature workers that have no manifest at all (coordinator
    reported as UNKNOWN -> classified orphaned, matching the live example
    that motivated this feature -- see spec design center).

    ps_output is injectable for testing; when None, shells out to the real
    `ps` binary. Never raises -- returns {} on any failure (missing `ps`,
    timeout, etc).
    """
    if ps_output is None:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ps_output = result.stdout
        except (OSError, subprocess.SubprocessError):
            return {}

    found: dict = {}
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, command = parts
        match = _SETTINGS_FILE_RE.search(command)
        if not match:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        found[match.group(1)] = pid
    return found


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


def discover_workers(
    workers_dir: Optional[Path] = None,
    *,
    ps_output: Optional[str] = None,
    tmux_runner: Optional[TmuxRunner] = None,
) -> list[dict]:
    """Discover workers from manifest files, UNIONed with a best-effort ps
    scan for worker processes.

    Sources:
    1. ~/.maniple/workers/*.json manifests.
    2. Best-effort ps scan (`--settings .../worker-<id>.json` argv pattern)
       -- confirms manifest-sourced workers' processes are alive
       (worker_alive), and discovers pre-feature workers that have no
       manifest at all (coordinator reported UNKNOWN, classified orphaned).

    ps_output/tmux_runner are injectable for testing (see _ps_scan /
    _tmux_attached); both default to shelling out to the real `ps`/`tmux`
    binaries.

    Returns:
        List of worker dicts, each with at minimum: session_id, name,
        agent_type, idle_age_hours, terminal_id, worker_alive,
        tmux_attached, coordinator_pid, coordinator_alive, class.
    """
    if workers_dir is None:
        workers_dir = Path.home() / ".maniple" / "workers"

    ps_matches = _ps_scan(ps_output)  # {session_id: pid}
    try:
        threshold = load_config().zombies.idle_threshold_hours
    except ConfigError:
        threshold = 2.0

    workers_by_id: dict[str, dict] = {}

    # 1. Read manifests.
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

                terminal_id = worker_info.get("terminal_id")

                # Build worker dict
                worker = {
                    "session_id": session_id,
                    "name": worker_info.get("name", session_id),
                    "agent_type": worker_info.get("agent_type", "claude"),
                    "model": worker_info.get("model"),
                    "idle_age_hours": idle_age,
                    "terminal_id": terminal_id,
                    "worker_alive": session_id in ps_matches,
                    "tmux_attached": _tmux_attached(terminal_id, tmux_runner=tmux_runner),
                    "coordinator_pid": coor_pid,
                    "coordinator_pid_start": coor_pid_start,
                    "coordinator_session_id": coordinator_info.get("session_id"),
                    "coordinator_session_name": coordinator_info.get("session_name"),
                    "coordinator_window_index": coordinator_info.get("window_index"),
                    "coordinator_iterm_session_id": coordinator_info.get("iterm_session_id"),
                    "coordinator_alive": coor_alive and not coor_defunct,
                    "coordinator_defunct": coor_defunct,
                    "project_dir": coordinator_info.get("project_dir"),
                    "closed_at": data.get("closed_at"),
                    "source": "manifest",
                }

                worker["class"] = classify_worker(worker, threshold)

                workers_by_id[session_id] = worker

            except (json.JSONDecodeError, KeyError, TypeError):
                # Manifest is malformed; skip (best-effort)
                continue

    # 2. Pre-feature workers found only via ps-scan (no manifest at all).
    # Coordinator is unidentifiable -- classified orphaned, matching the
    # live example that motivated this feature (spec design center).
    for session_id in ps_matches:
        if session_id in workers_by_id:
            continue
        worker = {
            "session_id": session_id,
            "name": session_id,
            "agent_type": None,
            "model": None,
            "idle_age_hours": None,
            "terminal_id": None,
            "worker_alive": True,
            "tmux_attached": None,
            "coordinator_pid": None,
            "coordinator_pid_start": None,
            "coordinator_session_id": None,
            "coordinator_session_name": None,
            "coordinator_window_index": None,
            "coordinator_iterm_session_id": None,
            "coordinator_alive": False,
            "coordinator_defunct": False,
            "project_dir": None,
            "closed_at": None,
            "source": "ps-scan",
        }
        worker["class"] = classify_worker(worker, threshold)
        workers_by_id[session_id] = worker

    return list(workers_by_id.values())


def format_zombies_report(
    workers: list[dict],
    as_json: bool = False,
    *,
    tmux_runner: Optional[TmuxRunner] = None,
) -> str:
    """Format zombies report as human table or JSON.

    Human output: table + per-zombie (orphaned/forgotten only) action
    blocks with exact reconnect commands. JSON output: structured dict
    with workers array + metadata.

    tmux_runner is injectable for testing the worker-connect command's
    pane->session_name resolution; defaults to shelling out to `tmux`.
    """
    if as_json:
        return _format_json_report(workers)
    return _format_human_report(workers, tmux_runner=tmux_runner)


def _format_human_report(
    workers: list[dict], *, tmux_runner: Optional[TmuxRunner] = None
) -> str:
    """Format as human-readable table + per-zombie action blocks."""
    if not workers:
        return "No workers found.\n"

    # Sort: orphaned/forgotten first (interesting), then ok/closed
    class_order = {"orphaned": 0, "forgotten": 1, "ok": 2, "closed": 3}
    sorted_workers = sorted(
        workers, key=lambda w: (class_order.get(w.get("class"), 99), w.get("name") or "")
    )

    # Build table
    lines = []
    lines.append("=== Zombie Workers Report ===\n")
    lines.append(
        f"{'Name':<20} {'Class':<12} {'Agent':<8} {'Idle (h)':<10} "
        f"{'Worker':<8} {'Tmux':<12} {'Coordinator':<12}"
    )
    lines.append("-" * 95)

    for worker in sorted_workers:
        name = (worker.get("name") or "?")[:19]
        class_ = (worker.get("class") or "?")[:11]
        agent = (worker.get("agent_type") or "?")[:7]
        idle_hours = worker.get("idle_age_hours")
        idle = "?" if idle_hours is None else f"{idle_hours:.1f}"
        worker_alive = "alive" if worker.get("worker_alive") else "dead"
        tmux_attached = worker.get("tmux_attached")
        tmux_flag = (
            "attached" if tmux_attached else ("unattached" if tmux_attached is False else "n/a")
        )
        coord_state = (
            "defunct" if worker.get("coordinator_defunct")
            else ("alive" if worker.get("coordinator_alive") else "dead/unknown")
        )
        lines.append(
            f"{name:<20} {class_:<12} {agent:<8} {idle:<10} "
            f"{worker_alive:<8} {tmux_flag:<12} {coord_state:<12}"
        )

    zombies = [w for w in sorted_workers if w.get("class") in ("orphaned", "forgotten")]
    if zombies:
        lines.append("\n=== Action Blocks ===\n")
        for worker in zombies:
            lines.append(_render_action_block(worker, tmux_runner=tmux_runner))
            lines.append("")

    return "\n".join(lines)


def _render_action_block(worker: dict, *, tmux_runner: Optional[TmuxRunner] = None) -> str:
    """Build the per-zombie action block: exact reconnect commands for the
    worker itself and for its coordinator (alive -> switch-client/attach,
    dead/defunct -> claude --resume)."""
    session_id = worker.get("session_id", "?")
    name = worker.get("name") or session_id
    class_ = worker.get("class", "?")
    idle_hours = worker.get("idle_age_hours")
    idle_str = "unknown" if idle_hours is None else f"{idle_hours:.1f}h"

    lines = [f"**{name}** ({session_id})", f"Status: {class_} (idle {idle_str})"]

    terminal_id = worker.get("terminal_id")
    if terminal_id and terminal_id.startswith("tmux:"):
        pane_id = terminal_id.split(":", 1)[1]
        session_name = _tmux_pane_session_name(pane_id, tmux_runner=tmux_runner)
        if session_name:
            lines.append(f"Connect to worker: `tmux attach -t '{session_name}'`")
    elif terminal_id and terminal_id.startswith("iterm:"):
        session_uuid = terminal_id.split(":", 1)[1]
        lines.append(
            f"Connect to worker: iTerm session `{session_uuid}` "
            "(`osascript -e 'tell application \"iTerm2\" to activate'` -- best-effort reveal)"
        )

    if worker.get("coordinator_alive"):
        coord_session_name = worker.get("coordinator_session_name")
        coord_iterm_id = worker.get("coordinator_iterm_session_id")
        if coord_session_name:
            lines.append(
                f"Connect to coordinator: `tmux switch-client -t '{coord_session_name}'` "
                f"(or `tmux attach -t '{coord_session_name}'` from outside tmux)"
            )
        elif coord_iterm_id:
            lines.append(
                f"Connect to coordinator: iTerm session `{coord_iterm_id}` "
                "(`osascript -e 'tell application \"iTerm2\" to activate'` -- best-effort reveal)"
            )
        else:
            lines.append(f"Coordinator is alive (PID {worker.get('coordinator_pid')})")
    else:
        project_dir = worker.get("project_dir")
        coord_sid = worker.get("coordinator_session_id")
        if project_dir and coord_sid:
            lines.append(
                f"Resume coordinator: `cd {project_dir} && claude --resume {coord_sid}`"
            )

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
