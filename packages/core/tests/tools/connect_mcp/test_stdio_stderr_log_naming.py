"""Лог stderr — по имени сервера; причина падения есть и у ``stream_stdio``.

Лог назывался по команде и хэшу аргументов: два сервера с одной командой и
разным env/cwd делили один файл, и причина одного могла прийти другому.
Имя сервера на пути дискавери и вызова известно — им лог и назван.

``stream_stdio`` (SDK ``stdio_client``) отдавал stderr сервера в stderr
родителя: под TUI он терялся, а упавший при старте сервер на ``/mcp`` был
без причины.

Каталог состояния — изолированный ``GC_HOME`` набора (плагин gc_testkit).
"""

import sys

import pytest
from gc_core.tools.connect_mcp import persistent_stdio, transport
from gc_core.tools.connect_mcp.registry import _adiscover_one

_CRASH_LINE = "ModuleNotFoundError: No module named mcp.server.fastmcp"

#: Одна команда и одни аргументы на оба сервера; различает их только env.
_WHO_CRASH_SCRIPT = (
    "import os, sys; "
    "sys.stderr.write('who=' + os.environ['WHO'] + '\\n'); "
    "sys.stderr.write("
    "'ModuleNotFoundError: No module named mcp.server.fastmcp\\n'); "
    "sys.exit(1)"
)


def _cfg(transport_name: str, who: str, **extra) -> dict:
    return {
        "transport": transport_name,
        "command": sys.executable,
        "args": ["-c", _WHO_CRASH_SCRIPT],
        "env": {"WHO": who},
        **extra,
    }


@pytest.fixture
async def persistent_registry():
    """Долгоживущие сессии теста гасятся в любом исходе."""
    registry = persistent_stdio.PersistentStdioRegistry.instance()
    yield registry
    await registry.aclose_all()


_TRANSPORTS = [
    pytest.param("stdio", {}, id="stdio"),
    pytest.param("stdio", {"persistent_session": True}, id="stdio-persistent"),
    pytest.param("stream_stdio", {}, id="stream_stdio"),
]


@pytest.mark.parametrize(("transport_name", "extra"), _TRANSPORTS)
async def test_same_command_different_servers_get_their_own_logs(
    transport_name, extra, persistent_registry
):
    reasons = {}
    for who in ("alpha", "beta"):
        name = f"{who}_{transport_name}"
        _tools, status = await _adiscover_one(
            name, _cfg(transport_name, who, **extra), timeout=10
        )
        assert status["status"] == "failed", status
        reasons[who] = status["reason"]

    for who, other in (("alpha", "beta"), ("beta", "alpha")):
        log_path = transport._stderr_log_path(
            _cfg(transport_name, who), f"{who}_{transport_name}"
        )
        assert log_path.name == f"{who}_{transport_name}.stderr.log"
        text = log_path.read_text(encoding="utf-8")
        assert f"who={who}" in text
        assert f"who={other}" not in text
        assert str(log_path) in reasons[who], reasons[who]


async def test_stream_stdio_crash_before_initialize_names_the_real_cause():
    _tools, status = await _adiscover_one(
        "pw", _cfg("stream_stdio", "pw"), timeout=10
    )

    assert status["status"] == "failed"
    reason = status["reason"]
    assert "сервер завершился при старте" in reason, reason
    assert _CRASH_LINE in reason, reason
    assert "pw.stderr.log" in reason, reason


async def test_stream_stdio_silent_crash_keeps_the_old_reason():
    """Пустой stderr — причины нет, сообщение прежнее."""
    cfg = {
        "transport": "stream_stdio",
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(1)"],
    }
    _tools, status = await _adiscover_one("mute_stream", cfg, timeout=10)

    assert status["status"] == "failed"
    assert "завершился при старте" not in (status["reason"] or "")


def test_server_name_is_sanitized_for_the_filesystem():
    path = transport._stderr_log_path({"command": "x"}, "../evil name/ok")
    assert path.parent.name == "mcp"
    assert "/" not in path.name and " " not in path.name
    assert path.name.endswith(".stderr.log")


def test_without_a_name_the_log_is_named_by_the_command():
    path = transport._stderr_log_path({"command": "/usr/bin/ddgs-mcp"})
    assert path.name == "ddgs-mcp.stderr.log"


async def test_stream_stdio_persistent_crash_names_the_real_cause():
    """Долгоживущая SDK-сессия: та же причина при входе, лог — по имени."""
    from gc_core.tools.connect_mcp import stream_client

    cfg = _cfg("stream_stdio", "persist", persistent_session=True)
    # Класс берём из модуля сейчас: плагин набора между тестами
    # перезагружает пакет ``tools``, и класс импорта модуля теста — уже
    # другой объект.
    from gc_core.tools.connect_mcp.transport import McpServerExitedError

    with pytest.raises(McpServerExitedError) as info:
        await stream_client._call_tool_stream_stdio_persistent(
            server_key="stream_stdio::test-persist",
            server_cfg=cfg,
            timeout=10,
            tool_name="noop",
            arguments={},
            server_name="persist_pw",
        )

    assert _CRASH_LINE in str(info.value)
    assert info.value.log_path.name == "persist_pw.stderr.log"
