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
                    question = find_pending_question(session.session_id)
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
