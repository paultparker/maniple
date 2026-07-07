"""
Override usage-pause tool.

Provides override_usage_pause for granting workers a continue past the
usage-pause hook's threshold via the escalating override ladder (see
usage_override.py and usage_pause_hook.py).
"""

from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..config import ConfigError, load_config
from ..registry import SessionRegistry
from ..usage_override import advance_override, resolve_expires_at
from ..utils import error_response, get_session_or_error


def register_tools(mcp: FastMCP) -> None:
    """Register override_usage_pause tool on the MCP server."""

    @mcp.tool()
    async def override_usage_pause(
        ctx: Context[ServerSession, "AppContext"],
        workers: list[str],
    ) -> dict:
        """
        Grant workers a continue past the usage-pause threshold.

        ⚠️ **IMPORTANT**: Only call this after the human user has given
        explicit permission for THIS SPECIFIC continue, in THIS session.
        Never call it on your own judgment, even if a worker looks close to
        finishing or the pause seems inconvenient. When a worker pauses,
        relay the pause to the user (what it hit, current rung) and ask —
        call this tool only after they approve.

        Advances each worker's usage-pause override one rung up the
        escalating ladder: base -> 90% -> 95% -> unlimited. Use this when a
        worker is blocked (or about to be blocked) by the usage-pause hook
        and needs to keep working past the account's 5-hour usage-window
        threshold. The override expires when the 5-hour window resets
        (read from the configured usage_pause.state_file; falls back to
        now + 5h if that can't be read).

        Workers already at the unlimited rung report "already unlimited"
        rather than erroring -- calling this repeatedly is safe.

        Args:
            workers: List of worker session IDs to grant a continue to.
                Accepts internal IDs, terminal IDs, or worker names.

        Returns:
            Dict with `results`: per-worker {new_rung, expires_at,
            already_unlimited} on success, or {error, hint} if the worker
            wasn't found.
        """
        app_ctx = ctx.request_context.lifespan_context
        registry: SessionRegistry = app_ctx.registry

        if not workers:
            return error_response("'workers' is required and must be non-empty")

        try:
            state_file = load_config().usage_pause.state_file
        except ConfigError:
            state_file = "/nonexistent-usage-pause-state-file"
        expires_at = resolve_expires_at(state_file)

        results = {}
        for worker_id in workers:
            session = get_session_or_error(registry, worker_id)
            if isinstance(session, dict):
                results[worker_id] = session
                continue
            results[worker_id] = advance_override(session.session_id, expires_at)

        return {"success": True, "results": results}
