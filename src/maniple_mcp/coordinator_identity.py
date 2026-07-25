"""
Coordinator identity capture.

Captures a best-effort snapshot of the coordinator Claude Code session that
launched this MCP server: its PID (+ start time, for a PID-reuse guard), its
Claude session id (when available), its project directory, and its tmux/
iTerm location. Used to breadcrumb workers back to their coordinator so a
zombie report can still identify (or reconnect to) the coordinator even
after the coordinator process has exited.

Same fail-open philosophy as the context-pause/usage-pause hooks: capture is
best-effort end to end and must NEVER raise, degrading to a partial (or
fully empty) CoordinatorIdentity on any failure.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("maniple.coordinator_identity")

MAX_PPID_HOPS = 5


@dataclass(frozen=True)
class CoordinatorIdentity:
    """Best-effort snapshot of the coordinator's identity. Every field is
    Optional -- any piece may be unavailable depending on install method
    (venv vs uvx) or terminal backend (tmux vs iTerm vs neither)."""

    pid: Optional[int] = None
    pid_start: Optional[str] = None  # `ps -o lstart=` output, for PID-reuse guard
    session_id: Optional[str] = None  # CLAUDE_CODE_SESSION_ID (version-dependent)
    project_dir: Optional[str] = None  # CLAUDE_PROJECT_DIR
    tmux_env: Optional[str] = None  # raw TMUX env var
    tmux_pane_env: Optional[str] = None  # raw TMUX_PANE env var
    tmux_session_name: Optional[str] = None  # derived via `tmux display`
    tmux_window_index: Optional[str] = None  # derived via `tmux display`
    tmux_pane_index: Optional[str] = None  # derived via `tmux display`
    iterm_session_id: Optional[str] = None  # ITERM_SESSION_ID

    def tmux_location(self) -> Optional[str]:
        """`session_name:window_index.pane_index`, or None if any part is
        missing (e.g. not running inside tmux)."""
        if (
            self.tmux_session_name
            and self.tmux_window_index is not None
            and self.tmux_pane_index is not None
        ):
            return f"{self.tmux_session_name}:{self.tmux_window_index}.{self.tmux_pane_index}"
        return None

    def to_dict(self) -> dict:
        """Full field dump, used for the persisted worker manifest."""
        return dataclasses.asdict(self)

    def to_env_vars(self) -> dict[str, str]:
        """MANIPLE_COORDINATOR_* env vars for worker launch, omitting any
        field that's unknown (never export an empty value)."""
        env: dict[str, str] = {}
        if self.pid is not None:
            env["MANIPLE_COORDINATOR_PID"] = str(self.pid)
        if self.pid_start:
            env["MANIPLE_COORDINATOR_PID_START"] = self.pid_start
        if self.session_id:
            env["MANIPLE_COORDINATOR_SESSION_ID"] = self.session_id
        if self.project_dir:
            env["MANIPLE_COORDINATOR_PROJECT_DIR"] = self.project_dir
        location = self.tmux_location()
        if location:
            env["MANIPLE_COORDINATOR_TMUX"] = location
        if self.iterm_session_id:
            env["MANIPLE_COORDINATOR_ITERM"] = self.iterm_session_id
        return env


def _run(cmd: list[str], timeout: float = 2.0) -> Optional[str]:
    """Run a subprocess and return its stripped stdout, or None on any
    failure (non-zero exit, timeout, missing binary, empty output)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _is_claude_command(comm: str) -> bool:
    return os.path.basename(comm.strip()) == "claude"


def _find_coordinator_pid(max_hops: int = MAX_PPID_HOPS) -> Optional[int]:
    """Walk the ppid chain from this process up to `max_hops` hops looking
    for a process whose command is the claude CLI (venv install = 1 hop;
    uvx inserts one `uv` hop first)."""
    try:
        pid = os.getpid()
        for _ in range(max_hops):
            ppid_str = _run(["ps", "-o", "ppid=", "-p", str(pid)])
            if not ppid_str:
                return None
            try:
                ppid = int(ppid_str)
            except ValueError:
                return None
            if ppid <= 1:
                return None
            comm = _run(["ps", "-o", "comm=", "-p", str(ppid)])
            if comm and _is_claude_command(comm):
                return ppid
            pid = ppid
        return None
    except Exception:
        logger.debug("coordinator_identity: ppid walk failed", exc_info=True)
        return None


def _pid_start_time(pid: int) -> Optional[str]:
    try:
        return _run(["ps", "-o", "lstart=", "-p", str(pid)])
    except Exception:
        return None


def _tmux_pane_location(
    tmux_pane: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Derive (session_name, window_index, pane_index) for a tmux pane id
    via `tmux display -p -t <pane>`."""
    try:
        output = _run([
            "tmux",
            "display",
            "-p",
            "-t",
            tmux_pane,
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}",
        ])
    except Exception:
        return (None, None, None)
    if not output:
        return (None, None, None)
    parts = output.split("\t")
    if len(parts) != 3:
        return (None, None, None)
    session_name, window_index, pane_index = parts
    return (session_name or None, window_index or None, pane_index or None)


def capture_coordinator_identity() -> CoordinatorIdentity:
    """Best-effort capture of the coordinator's identity. Never raises --
    any failing step is skipped and the corresponding field(s) stay None."""
    pid: Optional[int] = None
    pid_start: Optional[str] = None
    session_id: Optional[str] = None
    project_dir: Optional[str] = None
    tmux_env: Optional[str] = None
    tmux_pane_env: Optional[str] = None
    tmux_session_name: Optional[str] = None
    tmux_window_index: Optional[str] = None
    tmux_pane_index: Optional[str] = None
    iterm_session_id: Optional[str] = None

    try:
        pid = _find_coordinator_pid()
        if pid is not None:
            pid_start = _pid_start_time(pid)
    except Exception:
        logger.debug("coordinator_identity: pid capture failed", exc_info=True)

    try:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or None
        tmux_env = os.environ.get("TMUX") or None
        tmux_pane_env = os.environ.get("TMUX_PANE") or None
        iterm_session_id = os.environ.get("ITERM_SESSION_ID") or None
    except Exception:
        logger.debug("coordinator_identity: env capture failed", exc_info=True)

    if tmux_pane_env:
        try:
            tmux_session_name, tmux_window_index, tmux_pane_index = _tmux_pane_location(
                tmux_pane_env
            )
        except Exception:
            logger.debug("coordinator_identity: tmux display failed", exc_info=True)

    try:
        return CoordinatorIdentity(
            pid=pid,
            pid_start=pid_start,
            session_id=session_id,
            project_dir=project_dir,
            tmux_env=tmux_env,
            tmux_pane_env=tmux_pane_env,
            tmux_session_name=tmux_session_name,
            tmux_window_index=tmux_window_index,
            tmux_pane_index=tmux_pane_index,
            iterm_session_id=iterm_session_id,
        )
    except Exception:
        logger.debug("coordinator_identity: final assembly failed", exc_info=True)
        return CoordinatorIdentity()


# Cached once per process (analogous to "capture at MCP server startup") --
# the identity doesn't change over the server's lifetime.
_cached_identity: Optional[CoordinatorIdentity] = None


def get_coordinator_identity(force_refresh: bool = False) -> CoordinatorIdentity:
    """Return the cached CoordinatorIdentity, capturing it on first call."""
    global _cached_identity
    if force_refresh or _cached_identity is None:
        _cached_identity = capture_coordinator_identity()
    return _cached_identity


def clear_cache() -> None:
    """Clear the cached identity. Useful for testing."""
    global _cached_identity
    _cached_identity = None
