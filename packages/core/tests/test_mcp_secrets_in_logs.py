"""Секрет из адреса MCP-сервера не попадает в лог хоста.

Хост, настроивший корневое логирование, держит логгер ``httpx`` не ниже
WARNING: на INFO тот пишет адрес каждого запроса со строкой запроса.
Сообщения автоповтора вызова тула несут описание ошибки без секретов
определения сервера.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core.tools.connect_mcp.api import acall_mcp_tool
from gc_core.utils import aretry
from gc_core.utils.logging_module import configure_host_logging
from gc_testkit.mcp import FakeClock

_SECRET = "URLSECRET123"


class _Deny(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _deny(self):
        self.send_response(401)
        self.send_header("content-length", "0")
        self.end_headers()

    do_POST = do_GET = do_DELETE = _deny


@pytest.fixture
def host_logging():
    """Логирование хоста, как его ставит сервер; уровень ``httpx``
    возвращается после теста."""
    httpx_logger = logging.getLogger("httpx")
    saved = httpx_logger.level
    configure_host_logging(logging.INFO)
    yield
    httpx_logger.setLevel(saved)


@pytest.fixture
def deny_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Deny)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp?apikey={_SECRET}"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_host_logging_keeps_httpx_at_warning(host_logging):
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.parametrize("transport", ["stream_http", "http", "sse"])
async def test_retry_and_http_logs_carry_no_url_secret(
    transport, host_logging, deny_server, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(aretry, "_wait_before_retry", FakeClock().sleep)
    registry = {
        "mcp_s_echo": {
            "tool": {"parameters": {}},
            "metadata": {"mcp_server_key": "s", "mcp_tool_name": "echo"},
        }
    }
    servers = {"s": {"transport": transport, "url": deny_server}}

    with pytest.raises(Exception):  # noqa: B017 — исход здесь не важен
        await acall_mcp_tool(
            "mcp_s_echo", {}, servers, registry, default_use_retry=True
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("MCP tool call" in message for message in messages)
    assert not [message for message in messages if _SECRET in message]
