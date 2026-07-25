"""
Worker manifest: a persisted breadcrumb linking a worker session to its
coordinator, written best-effort at spawn and updated at close.

~/.maniple/workers/<worker_session_id>.json

The info matters most when the coordinator is dead/defunct -- registry
state is in-memory per coordinator MCP server, so this manifest is what
lets a zombie-worker report identify (or offer a reconnect command for) the
coordinator even after it has exited. Written/updated best-effort: a
failure here must never block or fail a spawn or close.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .coordinator_identity import CoordinatorIdentity

logger = logging.getLogger("maniple.worker_manifest")

SCHEMA_VERSION = 1
MANIFEST_DIR = Path.home() / ".maniple" / "workers"


def manifest_path(worker_session_id: str) -> Path:
    return MANIFEST_DIR / f"{worker_session_id}.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_worker_manifest(
    *,
    worker_session_id: str,
    name: str,
    agent_type: str,
    terminal_id: str,
    project_path: str,
    worktree_path: Optional[str],
    main_repo_path: Optional[str],
    model: Optional[str],
    coordinator: CoordinatorIdentity,
) -> None:
    """Best-effort: write the worker manifest at spawn. Never raises -- a
    manifest write failure must never block or fail a spawn."""
    try:
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "worker": {
                "session_id": worker_session_id,
                "name": name,
                "agent_type": agent_type,
                "terminal_id": terminal_id,
                "project_path": project_path,
                "worktree_path": worktree_path,
                "main_repo_path": main_repo_path,
                "model": model,
                "spawned_at": _utc_now_iso(),
            },
            "coordinator": coordinator.to_dict(),
        }
        manifest_path(worker_session_id).write_text(json.dumps(manifest, indent=2))
    except Exception:
        logger.debug(
            "worker_manifest: failed to write manifest for %s",
            worker_session_id,
            exc_info=True,
        )


def stamp_worker_closed(worker_session_id: str) -> None:
    """Best-effort: stamp closed_at on the worker's manifest, if it exists.
    Never raises. Never deletes the file -- a closed manifest is cheap
    history that lets a zombie report distinguish closed-cleanly from
    vanished."""
    try:
        path = manifest_path(worker_session_id)
        if not path.exists():
            return
        manifest = json.loads(path.read_text())
        manifest.setdefault("worker", {})["closed_at"] = _utc_now_iso()
        path.write_text(json.dumps(manifest, indent=2))
    except Exception:
        logger.debug(
            "worker_manifest: failed to stamp closed_at for %s",
            worker_session_id,
            exc_info=True,
        )
