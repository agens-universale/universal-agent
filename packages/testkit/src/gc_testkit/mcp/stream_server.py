"""Тестовый MCP-сервер FastMCP для потоковых транспортов.

argv: транспорт FastMCP (``streamable-http`` | ``sse``) и порт. Журнал —
``<GC_TEST_CONTROL>.log`` в формате stdio-сервера: ``start <pid>``,
``exit <pid>`` и ``req <method> <id> <session>`` на КАЖДЫЙ запрос, включая
``initialize``. Перехват стоит в ``ServerSession._received_request``: туда
приходят все запросы, а ``initialize`` дальше неё не уходит — обработчики
FastMCP его не видят. ``session`` — та же величина, что ``session`` в ответе
тула: по ней тест сверяет, что список и вызов шли одной сессией.

Тул ``action(delay, fail)`` → ``{"count", "session", "pid"}``; ``count`` —
номер вызова в этой сессии, ``fail`` отдаёт ошибку JSON-RPC.
"""

import asyncio
import json
import os
import sys

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.shared.exceptions import McpError

PID = os.getpid()
JOURNAL = os.environ.get("GC_TEST_CONTROL", "") + ".log"


def log(line: str) -> None:
    with open(JOURNAL, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def log_every_request() -> None:
    received = ServerSession._received_request

    async def logged(self, responder):
        method = responder.request.root.method
        log(f"req {method} {responder.request_id} {id(self)}")
        await received(self, responder)

    ServerSession._received_request = logged


def build_app(port: int) -> FastMCP:
    app = FastMCP("gc-test-stream", host="127.0.0.1", port=port)
    counts: dict = {}

    @app.tool()
    async def action(
        ctx: Context, delay: float = 0, fail: bool = False
    ) -> str:
        del fail  # ошибку отдаёт `dispatch` ответом JSON-RPC
        key = id(ctx.session)
        counts[key] = counts.get(key, 0) + 1
        await ctx.report_progress(0, 1, "started")
        await asyncio.sleep(delay)
        return json.dumps({"count": counts[key], "session": key, "pid": PID})

    handlers = app._mcp_server.request_handlers
    original_call = handlers[types.CallToolRequest]

    async def dispatch(request):
        if (request.params.arguments or {}).get("fail"):
            raise McpError(
                types.ErrorData(code=-32603, message="deliberate error")
            )
        return await original_call(request)

    handlers[types.CallToolRequest] = dispatch
    return app


def main() -> None:
    log(f"start {PID}")
    log_every_request()
    try:
        build_app(int(sys.argv[2])).run(transport=sys.argv[1])
    finally:
        log(f"exit {PID}")


if __name__ == "__main__":
    main()
