"""Асинхронный вызов MCP-тула (Задача 20 плана async-переезда).

Ключевое свойство: у SDK-транспортов вызов идёт ПРЯМО на loop вызывающего,
без единого потока. Легаси-транспорты (свои http/stdio на requests и
subprocess) блокирующие — их прервать нечем, поэтому они уходят в поток,
который можно бросить по дедлайну.
"""
import asyncio
import inspect
import threading

import pytest

from gc_core.tools.connect_mcp import api, client_port, stream_client
from gc_core.tools.connect_mcp.agent_tools import MCPToolWrapper

_REGISTRY = {
    "mcp_srv_echo": {
        "tool": {"name": "mcp_srv_echo", "parameters": {"type": "object",
                                                        "properties": {}}},
        "metadata": {"mcp_server_key": "srv", "mcp_tool_name": "echo"},
    }
}
_SDK_CFG = {"srv": {"transport": "stream_http", "url": "http://srv.invalid/mcp"}}
_LEGACY_CFG = {"srv": {"transport": "http", "url": "http://srv.invalid/mcp"}}


def test_async_entry_exists_and_is_a_coroutine():
    assert inspect.iscoroutinefunction(api.acall_mcp_tool)
    assert not inspect.iscoroutinefunction(api.call_mcp_tool)


async def test_sdk_transport_runs_without_spawning_a_thread(monkeypatch):
    """SDK-путь исполняется на текущем loop — новых потоков не появляется."""
    spawned = []
    real_thread = threading.Thread

    class _Counting(real_thread):
        def start(self):
            spawned.append(self.name)
            return super().start()

    async def _fake_call(**kwargs):
        return {"content": [{"type": "text", "text": "ответ тула"}]}

    # Диспетчер теперь один — порт клиента (`McpClientPort.acall_tool`), а не
    # switch по транспортам внутри `api`. Свойство проверяется то же, но точка
    # подмены переехала туда, где SDK-путь реально выбирается.
    monkeypatch.setattr(stream_client, "acall_tool_stream_http", _fake_call)
    monkeypatch.setattr(client_port, "acall_tool_stream_http", _fake_call)
    monkeypatch.setattr(threading, "Thread", _Counting)

    out = await api.acall_mcp_tool(
        "mcp_srv_echo", {}, _SDK_CFG, _REGISTRY, timeout=5
    )

    assert "ответ тула" in out
    assert spawned == [], f"SDK-путь завёл потоки: {spawned}"


async def test_legacy_http_runs_natively_without_a_thread(monkeypatch):
    """Легаси-HTTP идёт нативно: под ним ``httpx``, а у него есть AsyncClient.

    Раньше этот путь уходил в ``run_abandonable`` как «не имеющий асинхронной
    формы». Форма была всё это время — не использовалась, и каждый вызов тула
    по транспорту ``http``/``sse`` стоил отдельного ОС-потока.
    """
    from gc_core.tools.connect_mcp import client as legacy_client

    spawned = []
    real_thread = threading.Thread

    class _Counting(real_thread):
        def start(self):
            spawned.append(self.name)
            return super().start()

    async def _fake_session(*_a, **_k):
        return "sid-1"

    async def _fake_call(**kwargs):
        return {"content": [{"type": "text", "text": "ответ легаси"}]}

    monkeypatch.setattr(client_port, "_aget_or_init_session_id", _fake_session)
    monkeypatch.setattr(client_port, "_acall_mcp_tool", _fake_call)
    monkeypatch.setattr(legacy_client, "_acall_mcp_tool", _fake_call)
    monkeypatch.setattr(threading, "Thread", _Counting)

    out = await api.acall_mcp_tool(
        "mcp_srv_echo", {}, _LEGACY_CFG, _REGISTRY, timeout=5
    )

    assert "ответ легаси" in out
    assert spawned == [], f"легаси-HTTP снова завёл потоки: {spawned}"


async def test_persistent_stdio_still_goes_to_an_abandonable_thread(monkeypatch):
    """А вот долгоживущая stdio-сессия — по-прежнему в поток, и это осознанно.

    Asyncio-подпроцесс привязан к создавшему его loop (проверено прямым
    запуском: на втором ``asyncio.run`` он отдаёт «Future attached to a
    different loop»), а эту сессию заводит дискавери, зовут тулы и закрывает
    ``arun`` — три разных loop. Поэтому ``subprocess.Popen`` и поток-демон.
    """
    seen = {}
    cfg = {"srv": {"transport": "stdio", "command": "x", "persistent_session": True}}

    def _fake_sync(self, tool_name, arguments, *, timeout):
        seen["thread"] = threading.current_thread().name
        return {"tool": "mcp_srv_echo", "ok": True}

    monkeypatch.setattr(
        client_port.AgentRuntimeClient, "call_tool", _fake_sync, raising=True
    )

    out = await api.acall_mcp_tool("mcp_srv_echo", {}, cfg, _REGISTRY, timeout=5)

    assert '"ok": true' in out
    assert seen["thread"] != threading.main_thread().name, (
        "персистентная сессия должна была уйти с основного потока"
    )


async def test_both_entries_share_preparation(monkeypatch):
    """Ошибки валидации одинаковы у обоих входов — подготовка общая."""
    with pytest.raises(ValueError, match="Unknown tool"):
        api.call_mcp_tool("mcp_srv_missing", {}, _SDK_CFG, _REGISTRY)

    with pytest.raises(ValueError, match="Unknown tool"):
        await api.acall_mcp_tool("mcp_srv_missing", {}, _SDK_CFG, _REGISTRY)


def test_tool_wrapper_exposes_both_entries():
    """Диспетчер тулов смотрит на acall — без него MCP шёл бы в поток."""
    tool = MCPToolWrapper("mcp_srv_echo", _SDK_CFG, _REGISTRY, 5.0, False)

    assert inspect.iscoroutinefunction(tool.acall)
    assert not inspect.iscoroutinefunction(tool.__call__)


async def test_dispatcher_prefers_acall(monkeypatch):
    """aapply_tool обязан взять быстрый путь, а не уводить тул в поток."""
    from gc_core.tools import aapply_tool

    used = {}

    class _Tool:
        async def acall(self, **kw):
            used["path"] = "acall"
            return "быстрый путь"

        def __call__(self, **kw):
            used["path"] = "__call__"
            return "поток"

    data = {"t": {"tool_fabric": lambda **kw: _Tool()}}
    out = await aapply_tool("t", {}, data, ctx=None, config_dict={})

    assert used["path"] == "acall"
    assert out == "быстрый путь"
