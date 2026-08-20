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


class SyncExecAdapter:
    """Асинхронный порт поверх синхронного бэкенда.

    Совместимость, а не производительность: чужой ``FsBackend`` продолжает
    работать без единой правки у его автора. Поток здесь честно блокируется —
    но это поток из пула, а не event loop приложения.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def backend(self) -> Any:
        return self._backend

    async def aexec(self, command: str, **kwargs: Any) -> ExecResult:
        return await asyncio.to_thread(lambda: self._backend.exec(command, **kwargs))

    async def aexec_argv(self, argv: list, **kwargs: Any) -> ExecResult:
        return await asyncio.to_thread(lambda: self._backend.exec_argv(argv, **kwargs))

    async def arun_python(self, code: str, **kwargs: Any) -> ExecResult:
        return await asyncio.to_thread(lambda: self._backend.run_python(code, **kwargs))


def aexec_port(backend: Any) -> AsyncExecPort:
    """Асинхронный порт для бэкенда: свой, если он есть, иначе адаптер."""
    if all(callable(getattr(backend, name, None))
           for name in ("aexec", "aexec_argv", "arun_python")):
        return backend
    return SyncExecAdapter(backend)


__all__ = ["AsyncExecPort", "SyncExecAdapter", "aexec_port"]
