"""
Answer worker question tool.

Sends an option number to a worker blocked on an AskUserQuestion prompt.
The option number is a hotkey that selects AND submits, so no Enter is sent.
"""

from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..session_state import find_pending_question, validate_answer_index
from ..utils import error_response, HINTS


def register_tools(mcp: FastMCP) -> None:
    """Register answer_worker_question tool on the MCP server."""

    @mcp.tool()
    async def answer_worker_question(
        ctx: Context[ServerSession, "AppContext"],
        session_id: str,
        option_index: int,
        expected_tool_use_id: str | None = None,
    ) -> dict:
        """
        Answer a worker that is blocked on a single-select AskUserQuestion.

        Sends the 1-based option_index to the worker's pane (the number selects
        and submits). Use wait_for_worker first to learn the question, options,
        and tool_use_id.

        Args:
            session_id: Worker to answer. Accepts internal ID, terminal ID, or name.
            option_index: 1-based index into the question's real options.
            expected_tool_use_id: If given, abort unless the worker is still
                blocked on this exact question (race guard).

        Returns:
            Dict with success, session_id, option_index, chosen_label.
        """
        app_ctx = ctx.request_context.lifespan_context
        registry = app_ctx.registry
        backend = app_ctx.terminal_backend

        session = registry.resolve(session_id)
        if not session:
            return error_response(
                f"Session not found: {session_id}", hint=HINTS["session_not_found"]
            )

        question = find_pending_question(session.session_id)
        if question is None:
            return error_response(
                f"{session_id} is not blocked on a question right now."
            )

        if expected_tool_use_id and question["tool_use_id"] != expected_tool_use_id:
            return error_response(
                "Worker has moved on (tool_use_id changed); not answering a stale question.",
            )

        err = validate_answer_index(question, option_index)
        if err:
            return error_response(
                f"Cannot answer {session_id}: {err}. "
                f"Escalate to the human instead.",
            )

        # Number key selects AND submits in the AskUserQuestion menu; no Enter.
        await backend.send_text(session.terminal_session, str(option_index))

        return {
            "success": True,
            "session_id": session.session_id,
            "option_index": option_index,
            "chosen_label": question["options"][option_index - 1]["label"],
        }
