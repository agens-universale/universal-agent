"""Stateful real MCP server used by subprocess acceptance tests."""

import asyncio
import json
import os
import sys

from mcp.server.fastmcp import Context, FastMCP

transport = sys.argv[1]
port = int(sys.argv[2])
app = FastMCP("counter", host="127.0.0.1", port=port)
counters = {}


def audit(event):
    if len(sys.argv) > 3:
        with open(sys.argv[3], "a") as stream:
            stream.write(
                json.dumps({"event": event, "pid": os.getpid()}) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())


audit("open")


@app.tool()
async def increment(ctx: Context, delay: float = 0) -> str:
    key = id(ctx.session)
    counters[key] = counters.get(key, 0) + 1
    audit("increment")
    await ctx.report_progress(0, 1, "increment started")
    await asyncio.sleep(delay)
    await ctx.report_progress(1, 1, "increment complete")
    return json.dumps(
        {"count": counters[key], "pid": os.getpid(), "session": key}
    )


@app.tool()
async def value(ctx: Context) -> str:
    key = id(ctx.session)
    return json.dumps(
        {"count": counters.get(key, 0), "pid": os.getpid(), "session": key}
    )


app.run(transport=transport)
