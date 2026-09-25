"""Живой нативный поиск хоста (`find | xargs | grep`) — одна проба на процесс.

Общий для наборов: тесты, утверждающие `search_backend == "grep"`, без рабочего
рецепта не «падают», а не применимы. На Windows `find` из Git Bash есть, а GNU
grep нет (cygwin grep падает на `-i -F`), и движок честно уходит в Python.
"""

from __future__ import annotations

import asyncio
import functools

import pytest


@functools.cache
def host_search_caps() -> tuple[bool, bool]:
    """(`has_grep`, `has_find`) хоста — ОДИН прогрев, потом кэш.

    Прежняя версия читала свойства напрямую и глотала исключение в ``False``.
    После того как пробы стали корутинными (свойство отвечает только из кэша
    и до прогрева отказывает), это молча превратило все живые тесты нативного
    grep в пропуски, а сводка осталась зелёной.

    Свой ``asyncio.run``: функция зовётся на сборе тестов, где loop'а нет.
    """
    from gc_core.backends.local import LocalFsBackend

    backend = LocalFsBackend()
    try:
        asyncio.run(backend.awarm_capabilities())
        return bool(backend.has_grep), bool(backend.has_find)
    except Exception:
        # Отказ ЗДЕСЬ — это «на хосте нет grep/find», и пропуск честен.
        return False, False


requires_grep = pytest.mark.skipif(
    not host_search_caps()[0], reason="на хосте нет рабочего рецепта grep"
)
requires_native_search = pytest.mark.skipif(
    not all(host_search_caps()),
    reason="на хосте нет рабочего find | xargs | grep — нативный поиск недоступен",
)
