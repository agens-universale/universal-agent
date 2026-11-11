"""Один долгоживущий цикл агента на всё приложение TUI.

Раньше каждый ход, сборка контекста и ``/compact`` поднимали свой
``asyncio.run`` в свежем потоке. Задачи, заведённые ходом, умирали с концом
хода, а пул httpx закрывался после каждого (TLS-рукопожатие на каждое
сообщение). Фоновым субагентам нужен цикл, который переживает ход, — он
здесь: свой поток ``gc-ua-agent-loop``, ``run_forever`` до выхода.

Цикл — не цикл Textual, и это по-прежнему несущее: синхронный ``FsBackend``
мостящих бэкендов блокирует поток, и на цикле UI это был бы фриз или дедлок.

Потоки (жёсткое правило). Поток UI синхронно цикл не ждёт: из обработчиков —
``await asyncio.wrap_future(loop.submit(coro))``. Синхронный `run` — только с
потоков, где своего цикла нет (воркер раннера, тесты); с UI и с самого цикла
он бросает ``RuntimeError``.

Пул httpx (``async_http`` ключует клиентов по ``id(loop)``) живёт, пока жив
цикл, и закрывается ровно в `shutdown`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
import time
from typing import Any, Callable, Coroutine

from gc_tui.services.stdin_bridge import redirect_stdin

THREAD_NAME = "gc-ua-agent-loop"

_log = logging.getLogger(__name__)

_OF_LOCK = threading.Lock()

#: Шаг сторожа занятости цикла.
_WATCH_TICK_SEC = 1.0


class AgentLoop:
    """Цикл агента в своём daemon-потоке; запускается лениво (`start`)."""

    #: Сторож: цикл, не отвечавший дольше, виден в логе (см. `_check_lag`).
    busy_warn_sec: float = 10.0

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        #: Мост stdin потока цикла: чужой ``input()`` в корутине читает эту
        #: очередь, а не терминал Textual. Будит её `cancel_ask_human` раннера.
        self.stdin_queue: queue.Queue[str] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Реентерабельный: `submit` и `call_soon` зовут `start` под ним.
        self._lock = threading.RLock()
        self._closed = False
        #: Срок уборки выхода (``time.monotonic``); ставит `_close`.
        self._deadline = 0.0
        self._clock: Callable[[], float] = time.monotonic

    def start(self) -> None:
        """Поднять поток цикла. Идемпотентно; после `shutdown` — ошибка."""
        with self._lock:
            if self._closed:
                raise RuntimeError("цикл агента уже остановлен")
            if self.thread is not None:
                return
            self._loop = asyncio.new_event_loop()
            self.thread = threading.Thread(
                target=self._serve,
                args=(self._loop,),
                name=THREAD_NAME,
                daemon=True,
            )
            self.thread.start()

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        """Корутина на цикл; возвращает ``concurrent.futures.Future``.

        Проверка закрытия и постановка — под одним замком с `shutdown`:
        корутина либо встанет в цикл раньше его остановки (и будет
        отменена с остальными задачами), либо получит отказ сразу. Иначе
        отданная в окно закрытия корутина вешала бы `run` навсегда.
        """
        with self._lock:
            try:
                self.start()
            except RuntimeError:
                coro.close()
                raise
            return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Coroutine, timeout: float | None = None) -> Any:
        """Выполнить корутину на цикле и дождаться результата.

        Только с потока без своего цикла: ожидание с UI заморозило бы окно, а
        с самого цикла — дедлок.
        """
        if self.in_loop_thread() or thread_has_running_loop():
            coro.close()
            raise RuntimeError(
                "AgentLoop.run: синхронное ожидание цикла агента с потока, "
                "где идёт цикл (UI или сам цикл агента); "
                "используйте await asyncio.wrap_future(loop.submit(...))"
            )
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Вызвать ``fn`` на цикле (потокобезопасно, без ожидания)."""
        with self._lock:
            self.start()
            self._loop.call_soon_threadsafe(fn)

    def in_loop_thread(self) -> bool:
        return self.thread is not None and (
            threading.current_thread() is self.thread
        )

    def shutdown(self, timeout: float = 5.0) -> None:
        """Выход: stop → в потоке цикла отмена задач и ``aclose_clients`` →
        join.

        Ограничено ``timeout`` целиком. Задачи, пережившие остановку (ход,
        глухой к стопу), отменяются в потоке цикла раньше закрытия клиентов:
        пул, закрытый под живой задачей, её уборка открыла бы заново. Не
        дождались — поток daemon и процесс не держит. Только с потока без своего цикла, как
        и `run`: с UI — через ``asyncio.to_thread``.

        Идемпотентно и под гонку: закрывает только первый вызов (тот, что
        взвёл ``_closed``); остальные лишь ждут поток в пределах своего
        ``timeout`` — второй ``aclose`` на закрывающемся цикле бросил бы
        «Event loop is closed».
        """
        if self.in_loop_thread() or thread_has_running_loop():
            raise RuntimeError(
                "AgentLoop.shutdown: синхронное ожидание цикла агента с "
                "потока, где идёт цикл (UI или сам цикл агента)"
            )
        deadline = time.monotonic() + timeout
        with self._lock:
            first = not self._closed
            self._closed = True
            loop, thread = self._loop, self.thread
        if thread is None or not thread.is_alive():
            return
        if first:
            self._close(loop, deadline)
        thread.join(max(0.0, deadline - time.monotonic()))

    def _close(self, loop: asyncio.AbstractEventLoop, deadline: float) -> None:
        """Первый `shutdown`: остановить цикл; уборку делает его поток."""
        self._deadline = deadline
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:  # цикл уже закрыт (поток вышел сам)
            pass

    def _serve(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        with redirect_stdin(self.stdin_queue):
            loop.call_soon(self._watch, self._clock())
            try:
                loop.run_forever()
            finally:
                self._cancel_leftovers(loop)
                self._aclose_clients(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

    def _budget(self) -> float:
        return max(self._deadline - time.monotonic(), 0.1)

    def _cancel_leftovers(self, loop: asyncio.AbstractEventLoop) -> None:
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            loop.run_until_complete(
                asyncio.wait(tasks, timeout=self._budget())
            )

    def _aclose_clients(self, loop: asyncio.AbstractEventLoop) -> None:
        from gc_core.utils import async_http

        try:
            loop.run_until_complete(
                asyncio.wait_for(async_http.aclose_clients(), self._budget())
            )
        except Exception:
            _log.warning("agent_loop_aclose_clients_failed", exc_info=True)

    def _watch(self, expected: float) -> None:
        self._check_lag(expected)
        step = _WATCH_TICK_SEC
        self._loop.call_later(step, self._watch, self._clock() + step)

    def _check_lag(self, expected: float) -> None:
        """Сторож: тик опоздал больше ``busy_warn_sec`` — цикл был занят
        (синхронный вызов на потоке цикла держит и ходы, и детей).

        Ограничение: занятость видна только задним числом — тик опаздывает
        и срабатывает, когда цикл освободился. Вечную блокировку (цикл так и
        не вернулся) сторож не сообщит: его тик просто не наступит."""
        lag = self._clock() - expected
        if lag > self.busy_warn_sec:
            _log.warning(
                "цикл агента занят > %g с (опоздание %.1f с)",
                self.busy_warn_sec,
                lag,
                extra={"event": "agent_loop_busy", "lag_sec": lag},
            )


def thread_has_running_loop() -> bool:
    """Идёт ли в текущем потоке цикл asyncio (UI, цикл агента)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def agent_loop_of(app: Any) -> AgentLoop:
    """Цикл агента приложения; создаётся лениво, под замком."""
    with _OF_LOCK:
        loop = getattr(app, "_agent_loop", None)
        if loop is None:
            loop = AgentLoop()
            app._agent_loop = loop
        return loop
