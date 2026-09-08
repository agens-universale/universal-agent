"""Портал: приватный event loop в фоновом демон-потоке.

Несущая деталь обратной совместимости при переезде ядра на asyncio
(docs/plans/2026-08-20-async-core.md, Задача 2). Реализация у каждой операции
одна — асинхронная; синхронные точки входа (``AgentExecutor.run``,
``run_agent``, ``request_backend_*``) превращаются в три строки::

    def run(self, *args, **kwargs):
        return get_portal().run(self.arun(*args, **kwargs))

Так синхронный API сохраняет сигнатуру и блокирующую семантику, а второй
реализации не заводится.

Портал ленивый: пока никто не позвал синхронный API, потока нет. Поток
демонский — не задерживает выход процесса.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, Optional


class SyncApiInsideEventLoopError(RuntimeError):
    """Синхронный API ядра позвали из работающего event loop.

    Заблокировать вызывающий поток здесь значило бы остановить чужой loop
    целиком — вместе со всем, что на нём живёт. Молчать об этом нельзя, поэтому
    вместо тихой блокировки — явная ошибка с указанием на асинхронный двойник.
    """


class Portal:
    """Фоновый event loop, на котором исполняются корутины ядра."""

    def __init__(self, name: str = "gc-core-portal") -> None:
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _target() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    # SystemExit и KeyboardInterrupt из корутины asyncio
                    # пробивает наружу: future результат получает (вызывающий
                    # видит исключение и считает, что всё в порядке), а
                    # run_forever на этом заканчивается и поток умирает.
                    # Портал обязан это заметить сам: иначе _loop остаётся
                    # ссылкой на мёртвый loop, и следующий синхронный вызов
                    # встаёт в очередь, которую некому разобрать, — процесс
                    # виснет навсегда, без исключения и без строки в лог.
                    with self._lock:
                        if self._loop is loop:
                            self._loop = None
                            self._thread = None

            thread = threading.Thread(target=_target, name=self._name, daemon=True)
            thread.start()
            # Ждём, пока loop реально закрутится: run_coroutine_threadsafe на
            # ещё не запущенном loop ставит задачу в очередь, которую некому
            # разобрать, и вызывающий повис бы на .result().
            ready.wait()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Исполнить корутину на портальном loop, заблокировав вызывающий поток."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Закрываем корутину явно: без этого интерпретатор доругается
            # «coroutine was never awaited» уже в другом месте, и причину
            # ошибки в трейсбеке будет не видно.
            coro.close()
            raise SyncApiInsideEventLoopError(
                "Синхронный API gc_core вызван из работающего event loop. "
                "Используйте асинхронный двойник (arun / arun_agent / arequest_*) "
                "или вынесите вызов в asyncio.to_thread()."
            )
        loop = self._ensure_loop()
        return _unwrap(asyncio.run_coroutine_threadsafe(_carry(coro), loop).result())

    def close(self, timeout: float = 5.0) -> None:
        """Остановить loop и дождаться потока. Идемпотентно."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)
        loop.close()


async def _carry(coro: Coroutine[Any, Any, Any]) -> tuple[BaseException | None, Any]:
    """Донести до вызывающего ЛЮБОЙ исход корутины, включая BaseException.

    Без этой обёртки `SystemExit` (и `KeyboardInterrupt`) из корутины вешает
    процесс намертво. asyncio обрабатывает их особо: `Task.__step` ставит
    исключение на задачу и пробрасывает его дальше — сквозь `run_forever`,
    убивая поток портала. Перенос результата в `concurrent.futures.Future`
    сделан через `call_soon_threadsafe`, то есть должен исполниться на том же
    loop, — а разбирать очередь уже некому. Вызывающий остаётся на `.result()`
    навсегда: ни исключения, ни таймаута, ни строки в лог.

    В синхронном ядре `sys.exit()` внутри тула просто разворачивал стек. Здесь
    исход корутины становится ЗНАЧЕНИЕМ, поэтому портальный loop доживает до
    следующего вызова, а исключение поднимается у вызывающего — как раньше.
    """
    try:
        return None, await coro
    except BaseException as exc:  # noqa: BLE001 — переносим как есть, включая SystemExit
        return exc, None


def _unwrap(carried: tuple[BaseException | None, Any]) -> Any:
    exc, value = carried
    if exc is not None:
        raise exc
    return value


_portal: Optional[Portal] = None
_portal_lock = threading.Lock()


def get_portal() -> Portal:
    """Процессный портал. Создаётся при первом обращении, не на импорте."""
    global _portal
    with _portal_lock:
        if _portal is None:
            _portal = Portal()
        return _portal


def shutdown_portal(timeout: float = 5.0) -> None:
    """Погасить процессный портал (тесты, штатное завершение приложения)."""
    global _portal
    with _portal_lock:
        p, _portal = _portal, None
    if p is not None:
        p.close(timeout=timeout)


__all__ = ["Portal", "SyncApiInsideEventLoopError", "get_portal", "shutdown_portal"]
