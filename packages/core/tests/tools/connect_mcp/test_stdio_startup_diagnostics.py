"""Причина падения stdio MCP-сервера при старте видна на экране ``/mcp``.

Сервер, упавший до ``initialize`` (``ModuleNotFoundError`` в его venv),
раньше выглядел как «нет соединения с ddgs-mcp»: stderr процесса уходил в
``DEVNULL``, и настоящая причина не доходила никуда. Теперь stderr пишется в
лог сервера в каталоге состояния gc, а последняя непустая строка лога
попадает в ``reason`` дискавери.

Каталог состояния — изолированный ``GC_HOME`` набора (плагин gc_testkit).
"""

import sys

import pytest
from gc_core.tools.connect_mcp import persistent_stdio, transport
from gc_core.tools.connect_mcp.registry import _adiscover_one

_CRASH_LINE = "ModuleNotFoundError: No module named mcp.server.fastmcp"

_CRASH_SCRIPT = (
    "import sys; sys.stderr.write("
    "'ModuleNotFoundError: No module named mcp.server.fastmcp\\n'); "
    "sys.exit(1)"
)

#: Долгоживущий сервер, который до ответа на ``initialize`` пишет в stderr
#: заметно больше ёмкости pipe (64 КиБ). С непрочитанным ``PIPE`` он встал
#: бы на записи навсегда, и ``initialize`` не дождался бы ответа.
_CHATTY_SCRIPT = """
import json, sys
for _ in range(4096):
    sys.stderr.write("x" * 511 + "\\n")
sys.stderr.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                  "serverInfo": {"name": "chatty", "version": "0"}}
    else:
        result = {"tools": [{"name": "ping", "inputSchema": {}}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": result}) + "\\n")
    sys.stdout.flush()
"""


def _cfg(script: str, *, persistent: bool = False) -> dict:
    cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", script],
    }
    if persistent:
        cfg["persistent_session"] = True
    return cfg


@pytest.fixture
async def persistent_registry():
    """Долгоживущие сессии теста гасятся в любом исходе."""
    registry = persistent_stdio.PersistentStdioRegistry.instance()
    yield registry
    await registry.aclose_all()


async def test_crash_before_initialize_names_the_real_cause():
    tools, status = await _adiscover_one(
        "ddg", _cfg(_CRASH_SCRIPT), timeout=10
    )

    assert tools == []
    assert status["status"] == "failed"
    reason = status["reason"]
    assert "сервер завершился при старте" in reason, reason
    assert _CRASH_LINE in reason, reason
    log_path = transport._stderr_log_path(_cfg(_CRASH_SCRIPT))
    assert str(log_path) in reason, reason
    assert _CRASH_LINE in log_path.read_text(encoding="utf-8")


async def test_persistent_crash_before_initialize_names_the_real_cause(
    persistent_registry,
):
    cfg = _cfg(_CRASH_SCRIPT, persistent=True)
    tools, status = await _adiscover_one("ddg_persist", cfg, timeout=10)

    assert tools == []
    assert status["status"] == "failed"
    assert "сервер завершился при старте" in status["reason"], status
    assert _CRASH_LINE in status["reason"], status
    assert str(transport._stderr_log_path(cfg)) in status["reason"], status


async def test_silent_crash_keeps_the_old_reason():
    """Пустой stderr — причины нет, сообщение прежнее."""
    _tools, status = await _adiscover_one(
        "mute", _cfg("import sys; sys.exit(1)"), timeout=10
    )

    assert status["status"] == "failed"
    assert "завершился при старте" not in (status["reason"] or "")


async def test_chatty_stderr_does_not_block_a_one_shot_server():
    request = transport.aget_transport(_cfg(_CHATTY_SCRIPT))

    _headers, body = await request("", {}, dict(transport._INIT_PAYLOAD), 10.0)

    assert body.get("result"), body


async def test_chatty_stderr_does_not_block_a_persistent_server(
    persistent_registry,
):
    cfg = _cfg(_CHATTY_SCRIPT, persistent=True)
    tools, status = await _adiscover_one("chatty", cfg, timeout=10)

    assert status["status"] == "connected", status
    assert [t["name"] for t in tools] == ["ping"]
    log_text = transport._stderr_log_path(cfg).read_text(encoding="utf-8")
    assert len(log_text) > 1024 * 1024
