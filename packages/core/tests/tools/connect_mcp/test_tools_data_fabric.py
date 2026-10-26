"""Сквозной путь MCP: дискавери → фабрика из tools_data → вызов тула.

Ровно этот путь и оказался непокрытым. Прежние тесты либо создавали
``MCPToolWrapper`` напрямую, либо останавливались на дискавери, — и сборка
``tools_data`` разъехалась с фабрикой незамеченной: ``fabric()`` писала
``os_backend=os_backend``, а у ``_assemble_mcp_tools_data`` такого параметра
не было. Дискавери проходила, модель видела инструменты, и КАЖДЫЙ вызов
ЛЮБОГО MCP-тула падал ``NameError`` в момент создания обёртки.

Поэтому здесь ходят через публичный вход (``aget_mcp_tools_data_for_config``)
и обязательно дёргают ``tool_fabric`` — то есть делают то же, что диспетчер
в ``tools/__init__.py`` делает на каждый вызов.
"""

from gc_core.tools.connect_mcp import agent_tools
from gc_core.tools.connect_mcp.agent_tools import (
    MCPToolWrapper,
    aget_mcp_tools_data_for_config,
)
from gc_testkit.settings import make_settings

_TOOL_NAME = "mcp_srv_echo"
_REGISTRY = {
    _TOOL_NAME: {
        "tool": {
            "name": _TOOL_NAME,
            "description": "echo",
            "parameters": {"type": "object", "properties": {}},
        },
        "metadata": {"mcp_server_key": "srv", "mcp_tool_name": "echo"},
    }
}
_CONFIG = {
    "mcp.servers": {
        "srv": {"transport": "stream_http", "url": "http://srv.invalid/mcp"}
    },
}


class _FakeCache:
    """Кэш с готовым результатом дискавери: сеть здесь не при чём."""

    def __init__(self):
        self.rebound = None
        self.os_backend = None

    def rebind(self, cfg):
        self.rebound = cfg

    async def aget_tools(self, *, on_progress=None):
        return _REGISTRY

    def get_status(self):
        return {"srv": {"ok": True}}


class _SandboxBackend:
    """Опознаваемый бэкенд — проверяем, что он доехал до обёртки."""


async def test_fabric_from_public_entry_builds_a_wrapper():
    """Главная проверка: фабрика из tools_data вызывается и отдаёт обёртку."""
    tools_data, status = await aget_mcp_tools_data_for_config(
        make_settings(_CONFIG), tools_cache=_FakeCache()
    )

    assert _TOOL_NAME in tools_data, tools_data
    assert status == {"srv": {"ok": True}}

    fabric = tools_data[_TOOL_NAME]["tool_fabric"]
    tool = fabric()  # ровно так зовёт диспетчер, на каждый вызов тула

    assert isinstance(tool, MCPToolWrapper)


async def test_fabric_carries_os_backend_to_the_wrapper():
    """`execute_on: os_backend` обязан пережить сборку tools_data.

    Бэкенд нужен на ВЫЗОВЕ, а не только на дискавери: без него сайдкар-сервер,
    видимый лишь внутри песочницы, будет вызван из процесса агента.
    """
    backend = _SandboxBackend()
    tools_data, _ = await aget_mcp_tools_data_for_config(
        make_settings(_CONFIG), tools_cache=_FakeCache(), os_backend=backend
    )

    tool = tools_data[_TOOL_NAME]["tool_fabric"]()

    assert tool._os_backend is backend


async def test_acall_passes_os_backend_down_to_the_client(monkeypatch):
    """И дальше вниз: обёртка отдаёт бэкенд в `acall_mcp_tool`."""
    seen = {}

    async def _fake_acall(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(agent_tools, "acall_mcp_tool", _fake_acall)

    backend = _SandboxBackend()
    tools_data, _ = await aget_mcp_tools_data_for_config(
        make_settings(_CONFIG), tools_cache=_FakeCache(), os_backend=backend
    )
    out = await tools_data[_TOOL_NAME]["tool_fabric"]().acall()

    assert out == "ok"
    assert seen.get("os_backend") is backend, seen


def test_no_sync_entry_remains():
    """Синхронного входа у сборки tools_data больше нет.

    Здесь стоял тест «синхронный вход собирает тот же tools_data». Он проверял
    вторую форму, которая была тонкой обёрткой над корутиной через свой loop, и
    после перехода дискавери на нативный путь осталась без вызывающих. Проверка
    самой фабрики никуда не делась — она в тесте выше, на корутинной форме.
    """
    assert not hasattr(agent_tools, "get_mcp_tools_data_for_config")
    assert hasattr(agent_tools, "aget_mcp_tools_data_for_config")
