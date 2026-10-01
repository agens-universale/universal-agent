"""Opt-in retry ownership for durable MCP hosts; local behaviour is unchanged."""

from contextlib import contextmanager
from contextvars import ContextVar

from gc_core.execution.contracts import OutcomeUnknown

_DURABLE = ContextVar("durable_mcp_retry_owner", default=False)
_PROGRESS = ContextVar("durable_mcp_progress", default=None)


class McpSessionLostError(OutcomeUnknown):
    """A durable caller must decide recovery instead of getting an empty session."""


def durable_calls_enabled() -> bool:
    return _DURABLE.get()


def require_no_reconnect():
    if _DURABLE.get():
        raise McpSessionLostError(
            "MCP session lost; automatic repeat is disabled"
        )


async def call_tool_with_progress(session, name, arguments):
    callback = _PROGRESS.get()
    options = {"progress_callback": callback} if callback is not None else {}
    return await session.call_tool(name, arguments or {}, **options)


@contextmanager
def mcp_progress(callback):
    token = _PROGRESS.set(callback)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


@contextmanager
def durable_mcp_calls():
    token = _DURABLE.set(True)
    try:
        yield
    finally:
        _DURABLE.reset(token)
