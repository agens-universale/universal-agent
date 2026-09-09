"""Асинхронный порт исполнения команд (async-переезд, Задача 18).

``FsBackend`` — синхронный ABC с 30 абстрактными методами, и его реализуют не
только мы: встраивающие пишут свои бэкенды (песочницы, контейнеры, удалённые
хосты). Добавить туда `async`-двойники всех методов значило бы сломать каждую
такую реализацию — то есть ровно то, чего переезд обязан избежать.

Поэтому асинхронным сделан только запуск процессов: единственная операция
бэкенда, которая держит поток МИНУТАМИ. Всё остальное (stat, чтение, обход) —
короткий локальный I/O, и его синхронность loop не мешает.

Бэкенд, у которого есть ``aexec``, используется напрямую. Любой другой —
включая чужие реализации — заворачивается в ``SyncExecAdapter``: тот же
результат, поток вместо корутины.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional, Protocol, runtime_checkable

from gc_core.backends.protocol import ExecResult


@runtime_checkable
class AsyncExecPort(Protocol):
    """Что нужно циклу от бэкенда, чтобы запускать команды не блокируя loop."""

    async def aexec(self, command: str, *, cwd: Optional[str] = None,
                    timeout: Optional[int] = None,
                    env: Optional[dict] = None) -> ExecResult: ...

    async def aexec_argv(self, argv: list, *, stdin: Optional[str] = None,
                         cwd: Optional[str] = None,
                         timeout: Optional[int] = None,
                         env: Optional[dict] = None) -> ExecResult: ...

    async def arun_python(self, code: str, *, cwd: Optional[str] = None,
                          timeout: Optional[int] = None,
                          env: Optional[dict] = None) -> ExecResult: ...


#: Слоты для чужих синхронных бэкендов — СВОИ, не общие с MCP.
#:
#: Пул MCP брать нельзя: команда агента идёт минутами, а слот занят всё это
#: время. Одна долгая `execute_command` на чужом бэкенде выбрала бы слоты,
#: которыми пользуются MCP-дискавери и вызовы тулов СОСЕДНИХ агентов — та же
#: ошибка, которую уже исправляли для тел гвардов.
_EXEC_SLOTS = threading.BoundedSemaphore(32)


class SyncExecAdapter:
    """Асинхронный порт поверх синхронного бэкенда.

    Совместимость, а не производительность: чужой ``FsBackend`` продолжает
    работать без единой правки у его автора. Поток здесь честно блокируется —
    но это поток из пула, а не event loop приложения.

    Поток БРОШЕННЫЙ, а не из дефолтного пула. Разница видна ровно на стопе:
    прервать чужой синхронный ``exec`` нечем, команда доработает до своего
    таймаута в любом случае. Но с ``asyncio.to_thread`` за это платил ещё и
    вызывающий — отмена не возвращала управление, потому что ``asyncio.run``
    на выходе ждёт ``shutdown_default_executor``, то есть весь оставшийся
    таймаут команды. Здесь отмена возвращается сразу, а поток дорабатывает
    сам по себе; ровно тот же договор, что у MCP-обменов.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def backend(self) -> Any:
        return self._backend

    async def aexec(self, command: str, **kwargs: Any) -> ExecResult:
        return await self._off_loop(self._backend.exec, command, **kwargs)

    async def aexec_argv(self, argv: list, **kwargs: Any) -> ExecResult:
        return await self._off_loop(self._backend.exec_argv, argv, **kwargs)

    async def arun_python(self, code: str, **kwargs: Any) -> ExecResult:
        return await self._off_loop(self._backend.run_python, code, **kwargs)

    @staticmethod
    async def _off_loop(fn: Any, *args: Any, **kwargs: Any) -> ExecResult:
        from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

        return await run_abandonable_in(
            _EXEC_SLOTS, fn, *args, _thread_name="sync-exec", **kwargs
        )


def aexec_port(backend: Any) -> AsyncExecPort:
    """Асинхронный порт для бэкенда: свой, если он есть, иначе адаптер."""
    if all(callable(getattr(backend, name, None))
           for name in ("aexec", "aexec_argv", "arun_python")):
        return backend
    return SyncExecAdapter(backend)


__all__ = ["AsyncExecPort", "SyncExecAdapter", "aexec_port"]
