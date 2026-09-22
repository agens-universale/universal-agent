"""Негодное хранилище тредов отвергается ДО дискавери MCP.

Проверка стояла в `_finish_run_context`, то есть после `abuild_tools_data`: к
моменту отказа stdio-сессии уже подняты под токеном владельца и создан логгер
прогона с открытым файлом. `AsyncAgent.create` на исключении закрывает только
пул httpx — всё остальное доживало до конца процесса.
"""
import ast
import pathlib

import pytest
from gc_core import run_context


class _BadStore:
    """Объект, который не является ни синхронным, ни асинхронным хранилищем."""


async def test_bad_store_fails_before_mcp_discovery(monkeypatch, tmp_path):
    called: list = []

    async def _tools_should_not_run(*args, **kwargs):
        called.append(True)
        raise AssertionError("дискавери не должна начаться на негодном хранилище")

    monkeypatch.setattr(run_context, "abuild_tools_data", _tools_should_not_run)

    with pytest.raises(Exception):
        await run_context.acreate_run_context(
            {"logs_dump_dir": str(tmp_path), "save_logs": False},
            thread_store=_BadStore(),
        )

    assert called == [], "контекст дошёл до дискавери MCP с негодным хранилищем"


def test_check_precedes_the_build_in_source():
    """Структурно: проверка стоит выше сборки тулов в самом коде.

    Поведенческий тест выше поймает регрессию только через подмену; этот
    краснеет на самой перестановке строк.
    """
    tree = ast.parse(pathlib.Path(run_context.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "acreate_run_context")

    check_at = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "athread_store"]
    build_at = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "abuild_tools_data"]

    assert check_at, "проверка хранилища исчезла из acreate_run_context"
    assert build_at, "проверка устарела: сборка тулов больше не здесь"
    assert min(check_at) < min(build_at), (
        f"проверка хранилища на строке {min(check_at)} идёт после дискавери "
        f"({min(build_at)}) — сессии и логгер уже подняты"
    )
