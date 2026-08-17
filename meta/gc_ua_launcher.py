"""Точки входа полного профиля `gc-ua`.

Мета-дистрибутив не содержит кода агента — только этот лаунчер. Он существует
по двум причинам. Во-первых, hatchling отказывается собирать колесо без единого
файла. Во-вторых, команды обязан объявлять каждый профиль: `uv tool install`
экспортирует в PATH скрипты только запрошенного дистрибутива.

Импорты внутри функций: модули ядра приезжают дистрибутивом gc-cli, то есть
на момент импорта этого файла они уже доступны, но держать их на уровне модуля
незачем — лаунчер должен оставаться пустым по стоимости запуска.
"""
from __future__ import annotations


def cli() -> int:
    from cli import main
    return main()


def tui() -> int:
    from tui_entry import main
    return main()


def serve() -> int:
    from serve_entry import main
    return main()


def mcp() -> int:
    from mcp_runtime.cli import main
    return main()


def mcp_install() -> int:
    from mcp_runtime.install_cli import main
    return main()
