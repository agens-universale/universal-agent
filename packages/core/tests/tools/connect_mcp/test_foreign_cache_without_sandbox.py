"""Чужой кэш без песочницы, когда песочница есть у нас, — слышно.

Кэшем владеет вызывающий: приложение, поднимающее агентов пачками, заводит
один `ToolsCache` и отдаёт его каждому. Бэкенд кэш получает ТОЛЬКО в
конструкторе — `rebind` перепривязывает конфиг, но не бэкенд, и такого
параметра у него нет.

Значит кэш, созданный как `ToolsCache(cfg)`, останется без песочницы навсегда,
даже если агенту её передали. Дискавери `execute_on: os_backend` пойдёт мимо
неё: для stdio это запуск команды сервера в процессе агента. Сам вызов тула
уйдёт правильно — расхождение видно только по debug-логу, которого в проде
никто не читает.

Чинить за владельца нельзя (бэкенд — его выбор), а молчать нельзя тем более:
здесь проверяется, что расхождение попадает в лог как предупреждение — и что
на законных конфигурациях оно молчит.
"""

import logging

from gc_core.tools.connect_mcp.agent_tools import _prepare_mcp_tools_data
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_testkit.settings import make_settings


class _Sandbox:
    is_sandbox = True


class _Host:
    is_sandbox = False


def _cfg(execute_on: str = "os_backend") -> dict:
    return {
        "mcp-servers": {
            "fs": {
                "transport": "stdio",
                "command": "srv",
                "execute_on": execute_on,
            },
        }
    }


def test_a_foreign_cache_without_a_sandbox_is_reported(caplog):
    """Кэш без песочницы + песочница у агента + сервер, которому она нужна."""
    cache = ToolsCache(_cfg())  # ровно то, чему учила прежняя дока

    with caplog.at_level(logging.WARNING):
        _prepare_mcp_tools_data(
            make_settings(_cfg()), tools_cache=cache, os_backend=_Sandbox()
        )

    assert any(
        "fs" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    ), (
        f"обход песочницы прошёл молча: в логах ничего выше debug ({[r.message for r in caplog.records]})"
    )


def test_a_foreign_cache_with_the_sandbox_says_nothing(caplog):
    """Владелец передал бэкенд — предупреждать не о чем."""
    cache = ToolsCache(_cfg(), os_backend=_Sandbox())

    with caplog.at_level(logging.WARNING):
        _prepare_mcp_tools_data(
            make_settings(_cfg()), tools_cache=cache, os_backend=_Sandbox()
        )

    assert not caplog.records, (
        f"лишний шум: {[r.message for r in caplog.records]}"
    )


def test_no_sandbox_at_all_says_nothing(caplog):
    """CLI на хосте: одним конфигом пользуются и с песочницей, и без неё.

    Это законный режим, и предупреждать в нём — шуметь на каждом запуске.
    """
    cache = ToolsCache(_cfg())

    with caplog.at_level(logging.WARNING):
        _prepare_mcp_tools_data(
            make_settings(_cfg()), tools_cache=cache, os_backend=_Host()
        )

    assert not caplog.records, (
        f"лишний шум: {[r.message for r in caplog.records]}"
    )


def test_servers_that_do_not_ask_for_the_sandbox_say_nothing(caplog):
    """`execute_on: agent` — умолчание, ему бэкенд кэша безразличен."""
    cache = ToolsCache(_cfg("agent"))

    with caplog.at_level(logging.WARNING):
        _prepare_mcp_tools_data(
            make_settings(_cfg("agent")),
            tools_cache=cache,
            os_backend=_Sandbox(),
        )

    assert not caplog.records, (
        f"лишний шум: {[r.message for r in caplog.records]}"
    )
