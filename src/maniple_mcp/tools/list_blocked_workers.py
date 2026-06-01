"""
List blocked workers tool.

Sweeps the whole registry for workers currently blocked on an AskUserQuestion,
regardless of any watch set. Catches a worker that re-blocks after going idle —
which session-scoped polling (wait_for_worker with an explicit list) would miss.
"""

from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..session_state import find_blocked_workers


def register_tools(mcp: FastMCP) -> None:
    """Register list_blocked_workers tool on the MCP server."""

    @mcp.tool()
    async def list_blocked_workers(
        ctx: Context[ServerSession, "AppContext"],
    ) -> dict:
        """
        List every worker currently blocked on an AskUserQuestion.

        Sweeps ALL registered workers (via the pending-marker dir), not a
        caller-supplied list, so it catches a worker that re-blocks after it had
        already gone idle — something wait_for_worker (scoped to the session_ids
        you hand it) does not. Poll this to stay aware of questions across the
        whole fleet, then answer with answer_worker_question or escalate.

        Returns:
            Dict with:
                - blocked: list of {session_id, name, question} for blocked workers
                  (question includes options, tool_use_id, and answerable)
                - count: number of blocked workers
        """
        app_ctx = ctx.request_context.lifespan_context
        registry = app_ctx.registry

        sessions = registry.list_all()
        names = {s.session_id: getattr(s, "name", None) or s.session_id for s in sessions}

        blocked = find_blocked_workers([s.session_id for s in sessions])
        for entry in blocked:
            entry["name"] = names.get(entry["session_id"], entry["session_id"])

        return {"blocked": blocked, "count": len(blocked)}
