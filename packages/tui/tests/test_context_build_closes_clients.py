"""Сборка executor в TUI закрывает httpx-клиенты своего loop.

`_build_real_executor` заводит собственный `asyncio.run`, а реестр клиентов
`async_http` ключуется по loop. Ход и `/compact` закрывают пул в `finally` и
пишут об этом прямо в комментариях, а сборка — нет: клиент легаси http/sse MCP
оставался в реестре с открытыми сокетами после смерти loop, и так на каждой
пересборке контекста (смена конфига сессии, `/profiles`).

Проверка структурная: поведенческая поймала бы утечку только на легаси-MCP с
реальными сокетами, а эта краснеет на самой правке.
"""
import ast
import pathlib

from gc_testkit.paths import TUI_PKG

_SRC = TUI_PKG / "app.py"


def _build_executor_node() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_real_executor"
    )


def test_context_build_closes_its_clients():
    node = _build_executor_node()

    closes = [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "aclose_clients"
    ]
    assert closes, (
        "сборка контекста в TUI не закрывает пул httpx своего loop — "
        "клиент легаси http/sse MCP переживёт loop с открытыми сокетами"
    )

    in_finally = [
        n for t in ast.walk(node) if isinstance(t, ast.Try)
        for stmt in t.finalbody
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "aclose_clients"
    ]
    assert in_finally, (
        "закрытие пула не в `finally` — отказ сборки (нет сети, битый конфиг) "
        "оставит клиентов позади"
    )
