"""
Clear usage-pause override tool.

Provides clear_usage_override for reverting workers (or the globally
installed hook) back to the base usage-pause threshold.
"""

from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..registry import SessionRegistry
from ..usage_override import clear_override
from ..utils import error_response, get_session_or_error


def register_tools(mcp: FastMCP) -> None:
    """Register clear_usage_override tool on the MCP server."""

    @mcp.tool()
    async def clear_usage_override(
        ctx: Context[ServerSession, "AppContext"],
        workers: list[str],
    ) -> dict:
        """
        Clear usage-pause overrides, reverting to the base threshold.

        Args:
            workers: List of worker session IDs to clear. Accepts internal
                IDs, terminal IDs, or worker names. Also accepts the
                literal string "global" to clear the globally-installed
                usage-pause hook's override (see
                install-global-usage-guard / `maniple usage-override`).

        Returns:
            Dict with `results`: per-worker {cleared: bool} (True if an
            override existed and was removed), or {error, hint} if a
            worker wasn't found.
        """
        app_ctx = ctx.request_context.lifespan_context
        registry: SessionRegistry = app_ctx.registry

        if not workers:
            return error_response("'workers' is required and must be non-empty")

        results = {}
        for worker_id in workers:
            if worker_id == "global":
                scope = "global"
            else:
                session = get_session_or_error(registry, worker_id)
                if isinstance(session, dict):
                    results[worker_id] = session
                    continue
                scope = session.session_id
            results[worker_id] = {"cleared": clear_override(scope)}

        return {"success": True, "results": results}
