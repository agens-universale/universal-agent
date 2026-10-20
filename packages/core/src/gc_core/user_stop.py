"""Стоп хода снимает то, чего ход ждёт сейчас, и пишет, что снято.

Флаг ``stop_requested`` по-прежнему кончает ход между шагами. Пока модель
стримит или тул не вернулся, одного флага мало: ожидание регистрируется
здесь, и ``request`` с того же жеста его обрывает.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Callable, Iterator, Optional

#: Код возврата прерванной команды, если процесс не оставил свой.
INTERRUPTED_CODE = -15

_STOP: contextvars.ContextVar[Optional[UserStop]] = contextvars.ContextVar(
    "gc_user_stop", default=None
)
_CALL: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "gc_user_stop_call", default=("", "")
)

_SETTLE_SEC = 8.0


class UserStopInterrupt(Exception):
    """Ожидание оборвано стопом. Не транспортная ошибка и не повод повторять запрос."""

    def __init__(self, *, kind: str, started: bool = True) -> None:
        self.kind = kind
        self.started = started
        super().__init__(kind)


class _Arm:
    __slots__ = (
        "backend",
        "call_id",
        "fired",
        "kind",
        "mcp_server",
        "observed",
        "release",
        "tool",
        "unkillable",
    )

    def __init__(
        self,
        *,
        kind: str,
        tool: str,
        call_id: str,
        backend: str,
        mcp_server: str,
        release: Optional[Callable[[], Any]],
        unkillable: bool,
    ) -> None:
        self.kind = kind
        self.tool = tool
        self.call_id = call_id
        self.backend = backend
        self.mcp_server = mcp_server
        self.release = release
        self.unkillable = unkillable
        self.fired = False
        self.observed = asyncio.Event()


class UserStop:
    """Одна текущая работа хода. ``request`` снимает её из любого потока."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stack: list[_Arm] = []
        self._requested = False
        self._settled = threading.Event()
        self._settled.set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._logger: Any = None
        self._inflight: Optional[asyncio.Task] = None

    def prepare_run(
        self, loop: asyncio.AbstractEventLoop, run_logger: Any
    ) -> None:
        """Новый ход: прошлый стоп не переносится, loop — тот, где живёт прогон."""
        with self._lock:
            self._requested = False
            self._stack.clear()
            self._settled.set()
            self._loop = loop
            self._logger = run_logger

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self, run_logger: Any = None) -> None:
        """Снять текущую работу. Снимать нечего — события в логе нет."""
        log = self._logger if run_logger is None else run_logger
        with self._lock:
            self._requested = True
            arm = self._stack[-1] if self._stack else None
            if arm is None or arm.fired:
                return
            arm.fired = True
            loop = self._loop
            self._settled.clear()
        if loop is None or loop.is_closed():
            self._finish_without_loop(arm, log)
            return
        loop.call_soon_threadsafe(self._schedule, arm, log)

    def wait_if_other_thread(self, timeout: float = _SETTLE_SEC) -> None:
        """Подождать снятие, если мы не на потоке loop: иначе loop встанет насовсем."""
        loop = self._loop
        if (
            loop is not None
            and getattr(loop, "_thread_id", None) == threading.get_ident()
        ):
            return
        self._settled.wait(timeout)

    async def await_settled(self, timeout: float = _SETTLE_SEC) -> None:
        """Дождаться снятия, отдавая loop. Уже снято — возврат сразу."""
        deadline = time.monotonic() + timeout
        while not self._settled.is_set():
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.01)

    def notify_observed(self) -> None:
        """Ждущий на другом потоке увидел стоп и выходит. С loop — напрямую."""
        with self._lock:
            arm = self._stack[-1] if self._stack else None
            loop = self._loop
        if arm is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            arm.observed.set()
            return
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(arm.observed.set)

    @asynccontextmanager
    async def arm(
        self,
        *,
        kind: str,
        release: Optional[Callable[[], Any]] = None,
        unkillable: bool = False,
        tool: str = "",
        call_id: str = "",
        backend: str = "",
        mcp_server: str = "",
    ) -> AsyncIterator[_Arm]:
        """Пока блок открыт, ``request`` снимает именно эту работу.

        Стоп уже запрошен на входе — работа не началась, в лог ничего не пишем.
        """
        if not tool and not call_id:
            tool, call_id = _CALL.get()
        arm = _Arm(
            kind=kind,
            tool=tool or "",
            call_id=call_id or "",
            backend=backend or "",
            mcp_server=mcp_server or "",
            release=release,
            unkillable=unkillable,
        )
        loop = asyncio.get_running_loop()
        self.bind(loop)
        with self._lock:
            already = self._requested
            if not already:
                self._stack.append(arm)
        if already:
            raise UserStopInterrupt(kind=kind, started=False)
        try:
            yield arm
        finally:
            with self._lock:
                try:
                    self._stack.remove(arm)
                except ValueError:
                    pass

    def _schedule(self, arm: _Arm, run_logger: Any) -> None:
        self._inflight = asyncio.create_task(self._fire(arm, run_logger))

    async def _fire(self, arm: _Arm, run_logger: Any) -> None:
        try:
            _log_stop(run_logger, arm)
            if arm.release is not None:
                result = arm.release()
                if hasattr(result, "__await__"):
                    await result
            else:
                arm.observed.set()
        finally:
            arm.observed.set()
            self._settled.set()

    def _finish_without_loop(self, arm: _Arm, run_logger: Any) -> None:
        _log_stop(run_logger, arm)
        arm.observed.set()
        self._settled.set()


def _log_stop(run_logger: Any, arm: _Arm) -> None:
    """Одна запись. Тело запроса, аргументы и вывод команды сюда не входят."""
    if run_logger is None:
        return
    if arm.unkillable:
        run_logger.info(
            "user stop: command cannot be killed backend=%s tool=%s",
            arm.backend,
            arm.tool,
            extra={
                "event": "user_stop_unkillable",
                "backend": arm.backend,
                "tool": arm.tool,
                "call_id": arm.call_id,
            },
        )
        return
    run_logger.info(
        "user stop killed kind=%s tool=%s",
        arm.kind,
        arm.tool,
        extra={
            "event": "user_stop_killed",
            "kind": arm.kind,
            "tool": arm.tool,
            "call_id": arm.call_id,
            "backend": arm.backend,
            "mcp_server": arm.mcp_server,
        },
    )


def current_user_stop() -> Optional[UserStop]:
    return _STOP.get()


@contextmanager
def bind_user_stop(stop: UserStop) -> Iterator[None]:
    token = _STOP.set(stop)
    try:
        yield
    finally:
        _STOP.reset(token)


@contextmanager
def bind_tool_call(tool: str, call_id: str) -> Iterator[None]:
    token = _CALL.set((str(tool or ""), str(call_id or "")))
    try:
        yield
    finally:
        _CALL.reset(token)


def current_tool_call() -> tuple[str, str]:
    return _CALL.get()


async def await_agent_stop(agent: Any) -> None:
    """Стоп агента и ожидание, пока текущая работа снята."""
    stop = getattr(agent, "astop", None)
    if stop is not None:
        await stop()
        return
    plain = getattr(agent, "stop", None)
    if plain is not None:
        plain()


def interrupted_code(return_code: Any) -> int:
    """Прерванный код: успешный выход и «кода нет» стопом не остаются."""
    if return_code in (None, 0):
        return INTERRUPTED_CODE
    try:
        return int(return_code)
    except (TypeError, ValueError):
        return INTERRUPTED_CODE


__all__ = [
    "INTERRUPTED_CODE",
    "UserStop",
    "UserStopInterrupt",
    "await_agent_stop",
    "bind_tool_call",
    "bind_user_stop",
    "current_tool_call",
    "current_user_stop",
    "interrupted_code",
]
