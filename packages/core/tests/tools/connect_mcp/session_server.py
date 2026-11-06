"""Настоящий MCP server с JSON-RPC ошибкой, а не CallToolResult.isError."""

import asyncio
import json
import os
import sys

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError

app = FastMCP("session-owner-test", host="127.0.0.1", port=int(sys.argv[2]))
counts = {}


def audit(event):
    with open(sys.argv[3], "a") as stream:
        stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")


@app.tool()
async def action(ctx: Context, delay: float = 0, fail: bool = False) -> str:
    assert not fail  # Ошибка должна уйти JSON-RPC-ответом из dispatch.
    key = id(ctx.session)
    counts[key] = counts.get(key, 0) + 1
    audit("call")
    await ctx.report_progress(0, 1, "started")
    await asyncio.sleep(delay)
    return json.dumps(
        {"count": counts[key], "session": key, "pid": os.getpid()}
    )


original_call = app._mcp_server.request_handlers[types.CallToolRequest]


async def dispatch(request):
    if request.params.arguments.get("fail"):
        audit("rpc-error")
        raise McpError(
            types.ErrorData(code=-32603, message="deliberate error")
        )
    return await original_call(request)


app._mcp_server.request_handlers[types.CallToolRequest] = dispatch
app.run(transport=sys.argv[1])
