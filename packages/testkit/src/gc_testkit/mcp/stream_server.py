"""Тестовый MCP-сервер FastMCP для потоковых транспортов.

argv: транспорт FastMCP (``streamable-http`` | ``sse``) и порт. Журнал —
``<GC_TEST_CONTROL>.mcp-journal`` в формате stdio-сервера: ``start <pid>``,
``exit <pid>`` и ``req <method> <id> <session>`` на КАЖДЫЙ запрос, включая
``initialize``. Перехват стоит в ``ServerSession._received_request``: туда
приходят все запросы, а ``initialize`` дальше неё не уходит — обработчики
FastMCP его не видят. ``session`` — номер сессии от монотонного счётчика,
присвоенный объекту сессии на первом её запросе (не ``id()``: адрес собранной
сессии достаётся новой); та же величина — ``session`` в ответе тула, по ней
тест сверяет, что список и вызов шли одной сессией.

Тул ``action(delay, fail)`` → ``{"count", "session", "pid"}``; ``count`` —
номер вызова в этой сессии, ``fail`` отдаёт ошибку JSON-RPC.
"""

import asyncio
import itertools
import json
import os
import sys

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.shared.exceptions import McpError

PID = os.getpid()
CONTROL = os.environ.get("GC_TEST_CONTROL", "")
JOURNAL = CONTROL + ".mcp-journal"  # gc_testkit.mcp.JOURNAL_SUFFIX
_SESSION_ATTR = "_gc_test_session_number"
_session_numbers = itertools.count(1)


def log(line: str) -> None:
    with open(JOURNAL, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def session_number(session) -> int:
    """Номер сессии; присваивается на первом её запросе."""
    number = getattr(session, _SESSION_ATTR, None)
    if number is None:
        number = next(_session_numbers)
        setattr(session, _SESSION_ATTR, number)
    return number


def log_every_request() -> None:
    received = ServerSession._received_request

    async def logged(self, responder):
        method = responder.request.root.method
        log(f"req {method} {responder.request_id} {session_number(self)}")
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
        key = session_number(ctx.session)
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
    if not CONTROL:
        sys.exit("GC_TEST_CONTROL не задан: журнал писать некуда")
    log(f"start {PID}")
    log_every_request()
    try:
        build_app(int(sys.argv[2])).run(transport=sys.argv[1])
    finally:
        log(f"exit {PID}")


if __name__ == "__main__":
    main()
