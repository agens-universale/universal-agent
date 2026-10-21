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

#: Потолок ответа «работа снята»: допустимые 20 с удалённого шелла.
#: Уложились — успех, зависший release — ошибка.
_SETTLE_SEC = 20.0


class _StopWaitOnly:
    """``release`` оборвал ожидание, до самой работы сигнал отмены не дошёл."""


#: Вернуть из ``release``, если ждать перестали, а снять вызов нельзя.
#: В лог тогда пишется ``user_stop_unkillable``, не ``user_stop_killed``.
STOP_WAIT_ONLY = _StopWaitOnly()


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
        "fire",
        "fired",
        "kind",
        "left",
        "log_event",
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
        log_event: bool = True,
    ) -> None:
        self.kind = kind
        self.tool = tool
        self.call_id = call_id
        self.backend = backend
        self.mcp_server = mcp_server
        self.release = release
        self.unkillable = unkillable
        self.log_event = log_event
        self.fired = False
        self.observed = asyncio.Event()
        #: Исходное ожидание вышло из ``arm``.
        self.left = asyncio.Event()
        self.fire: Optional[asyncio.Task] = None


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

    async def await_settled(self, timeout: Optional[float] = None) -> None:
        """Дождаться снятия, отдавая loop. Уже снято — возврат сразу.

        Потолок — ``TimeoutError``, не тихий успех: ответ «снято» значит,
        что работа снята.
        """
        if timeout is None:
            timeout = _SETTLE_SEC
        deadline = time.monotonic() + timeout
        while not self._settled.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError("user stop did not settle")
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
        log_event: bool = True,
    ) -> AsyncIterator[_Arm]:
        """Пока блок открыт, ``request`` снимает именно эту работу.

        Стоп уже запрошен на входе — работа не началась, в лог ничего не пишем.
        ``log_event=False`` — жест принят, но работа не снята и в лог не пишется.
        Так Harbor дожидается уже идущего ``env.exec`` и не отдаёт отмену тулу.
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
            log_event=log_event,
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
            # Сначала «вышли», потом ждём ``_fire``: на ошибке release он
            # стоит на ``left`` и без этого шага не отпустит ``_settled``.
            arm.left.set()
            with self._lock:
                try:
                    self._stack.remove(arm)
                except ValueError:
                    pass
            if arm.fired and arm.fire is None:
                await asyncio.sleep(0)
            if arm.fire is not None:
                await _await_fire(arm.fire)

    def _schedule(self, arm: _Arm, run_logger: Any) -> None:
        task = asyncio.create_task(self._fire(arm, run_logger))
        arm.fire = task
        self._inflight = task
        task.add_done_callback(_retrieve_task_error)

    async def _fire(self, arm: _Arm, run_logger: Any) -> None:
        """Снять работу. ``killed`` — только после успешного ``release``.

        Ошибка ``release`` процессом не считается: в лог идёт другое событие,
        ``_settled`` открывается, когда исходное ожидание само вышло.
        """
        try:
            if arm.release is not None:
                result = arm.release()
                if hasattr(result, "__await__"):
                    result = await result
                if result is STOP_WAIT_ONLY:
                    arm.unkillable = True
            else:
                arm.observed.set()
            _log_stop(run_logger, arm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_release_failed(run_logger, arm, exc)
            await arm.left.wait()
        finally:
            arm.observed.set()
            self._settled.set()

    def _finish_without_loop(self, arm: _Arm, run_logger: Any) -> None:
        _log_stop(run_logger, arm)
        arm.observed.set()
        self._settled.set()


def _retrieve_task_error(task: asyncio.Task) -> None:
    """Забрать исключение задачи, которую никто больше не ждёт."""
    if task.cancelled():
        return
    task.exception()


async def _await_fire(task: asyncio.Task) -> None:
    """Дождаться ``_fire`` и забрать его исключение.

    Отмена самого ``arm`` пробрасывается дальше. Ошибка release уже записана
    в лог внутри ``_fire``.
    """
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        return


def _log_release_failed(
    run_logger: Any, arm: _Arm, exc: BaseException
) -> None:
    """``release`` не снял работу. Это не ``user_stop_killed``."""
    if run_logger is None:
        return
    run_logger.info(
        "user stop release failed kind=%s tool=%s error=%s",
        arm.kind,
        arm.tool,
        type(exc).__name__,
        extra={
            "event": "user_stop_release_failed",
            "kind": arm.kind,
            "tool": arm.tool,
            "call_id": arm.call_id,
            "backend": arm.backend,
            "mcp_server": arm.mcp_server,
            "error": type(exc).__name__,
        },
    )


def _log_stop(run_logger: Any, arm: _Arm) -> None:
    """Одна запись. Тело запроса, аргументы и вывод команды сюда не входят."""
    if run_logger is None or not arm.log_event:
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


async def await_cancelled(task: asyncio.Task) -> None:
    """Отменить задачу, которую ход не принял, и дождаться её выхода.

    Пока между постановкой в очередь и ``arm`` нет ожидания, тело ещё не
    шагнуло: отмена не даёт ему начаться. Исключение забирается здесь.
    Отмена самого вызывающего пробрасывается дальше.
    """
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        return


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
    "await_cancelled",
    "bind_tool_call",
    "bind_user_stop",
    "current_tool_call",
    "current_user_stop",
    "interrupted_code",
]
