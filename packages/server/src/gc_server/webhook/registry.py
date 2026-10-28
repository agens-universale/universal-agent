"""Свои прогоны вебхука в памяти процесса: брони сборки контекста и живые
прогоны (остановка, снятие при выходе).

Сообщения задачи здесь не копятся: вебхук кладёт их во входящий поток треда,
ход забирает их сам (в начале, опросом и рукопожатием). Статус задачи — в
хранилище (``webhook/status.py``); реестр только сообщает поверхности о
начале и конце прогона.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from gc_core.agent_executor import InboxDrained
from gc_core.threads.errors import ThreadBusy

logger = logging.getLogger(__name__)

#: Чем кончился прогон: ``completed`` | ``failed`` | ``cancelled`` |
#: ``handed_off`` (тред взял другой инстанс — он и доведёт задачу).
Ending = str


def _make_registry_key(system: str, task_id: str) -> str:
    sys_norm = (system or "").strip().lower()
    task_norm = (task_id or "").strip()
    if not sys_norm:
        raise ValueError("system должен быть непустой строкой")
    if not task_norm:
        raise ValueError("task_id должен быть непустой строкой")
    return f"{sys_norm}:{task_norm}"


@dataclass
class ActiveRun:
    """Свой идущий прогон задачи."""

    key: str
    system: str
    task_id: str
    executor: Any
    started_at: float
    #: Прогон: ``asyncio.Task`` на loop сервера.
    task: asyncio.Task | None = None
    ending: Ending = ""
    result: Any = None
    error: BaseException | None = None
    #: Что поверхность хочет помнить о прогоне (прежний статус задачи).
    notes: dict = field(default_factory=dict)
    #: Прогон снят остановкой сервера — прерывание, а не отмена.
    shutdown: bool = False


Hook = Callable[[ActiveRun], Awaitable[None]]


def _stopped(executor: Any) -> bool:
    """Ход остановили (отмена через поток control или ``stop``): итог —
    cancelled, а не completed."""
    get_state = getattr(executor, "get_state", None)
    state = get_state() if get_state is not None else None
    return bool(getattr(state, "stop_requested", False))


class AgentRunRegistry:
    """Брони сборки контекста и свои живые прогоны по ключу задачи."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, ActiveRun] = {}
        #: Ключ → когда взята бронь: контекст задачи строится этим процессом.
        self._reservations: dict[str, float] = {}
        #: Прогоны, снятые из реестра, чей итог ещё пишется: ход окончен,
        #: аренда отпущена, но задача ещё не брошенная.
        self._finishing: dict[str, ActiveRun] = {}

    def building_since(self, *, system: str, task_id: str) -> float | None:
        """С каких пор этот процесс строит контекст задачи (или ``None``)."""
        key = _make_registry_key(system, task_id)
        with self._lock:
            return self._reservations.get(key)

    def is_running(self, *, system: str, task_id: str) -> bool:
        return self.run_of(system=system, task_id=task_id) is not None

    def is_finishing(self, *, system: str, task_id: str) -> bool:
        """Свой прогон задачи кончился, но его итог ещё пишется."""
        key = _make_registry_key(system, task_id)
        with self._lock:
            return key in self._finishing

    def run_of(self, *, system: str, task_id: str) -> ActiveRun | None:
        key = _make_registry_key(system, task_id)
        with self._lock:
            return self._runs.get(key)

    def reserve(self, *, system: str, task_id: str) -> bool:
        """Занять ключ задачи до сборки контекста; ``False`` — его уже ведёт
        или строит этот процесс. Взявший снимает бронь
        ``release_reservation`` при любом исходе сборки."""
        key = _make_registry_key(system, task_id)
        with self._lock:
            if key in self._runs or key in self._reservations:
                return False
            self._reservations[key] = time.time()
            return True

    def release_reservation(self, *, system: str, task_id: str) -> None:
        key = _make_registry_key(system, task_id)
        with self._lock:
            self._reservations.pop(key, None)

    async def astart(
        self,
        *,
        system: str,
        task_id: str,
        executor: Any,
        ctx: Any,
        on_finish: Hook,
        notes: dict | None = None,
        verbose: bool | None = None,
    ) -> ActiveRun:
        """Запустить прогон задачи: ход из входящего потока треда."""
        key = _make_registry_key(system, task_id)
        run = ActiveRun(
            key=key,
            system=system,
            task_id=task_id,
            executor=executor,
            started_at=time.time(),
            notes=dict(notes or {}),
        )
        with self._lock:
            self._runs[key] = run
        run.task = asyncio.get_running_loop().create_task(
            self._arun(run, ctx, on_finish, verbose), name=f"agent-run:{key}"
        )
        return run

    async def _arun(
        self, run: ActiveRun, ctx: Any, on_finish: Hook, verbose: Any
    ) -> None:
        try:
            await self._aplay(run, verbose)
        except asyncio.CancelledError:
            # Снятие — не отказ: остановка сервера (прерывание) или отмена.
            run.ending = "interrupted" if run.shutdown else "cancelled"
            raise
        except (ThreadBusy, InboxDrained):
            # Тред взял другой инстанс (или он уже всё забрал): доведёт он.
            run.ending = "handed_off"
        except Exception as exc:
            run.ending, run.error = "failed", exc
        finally:
            with self._lock:
                self._runs.pop(run.key, None)
                self._finishing[run.key] = run
            try:
                await self._afinish(run, on_finish)
            finally:
                with self._lock:
                    self._finishing.pop(run.key, None)
            # Логгер прогона `logging` держит до конца процесса: без этого
            # каждый запрос оставлял по логгеру с открытым файлом.
            from gc_core.utils.logging_module import release_run_logger

            release_run_logger(getattr(ctx, "logger", None))

    async def _aplay(self, run: ActiveRun, verbose: Any) -> None:
        """Ходы, пока во входящем потоке есть недоставленное.

        Исполнитель доигрывает поток сам (рукопожатие). Остаётся окно между
        его последней проверкой и снятием прогона из реестра: вебхук в это
        окно видит прогон живым и отвечает ``queued``. Поэтому после снятия —
        ещё одна проверка потока."""
        while True:
            run.result = await run.executor.arun(
                [], thread_id=run.task_id, system=run.system, verbose=verbose
            )
            run.ending = "cancelled" if _stopped(run.executor) else "completed"
            with self._lock:
                self._runs.pop(run.key, None)
                self._finishing[run.key] = run
            if run.ending == "cancelled":
                return  # остановленный прогон хвост не доигрывает
            if not await run.executor.ainbox_left():
                return
            with self._lock:
                if run.key in self._runs or run.key in self._reservations:
                    return  # следующий прогон этого процесса уже стартует
                self._finishing.pop(run.key, None)
                self._runs[run.key] = run

    @staticmethod
    async def _afinish(run: ActiveRun, on_finish: Hook) -> None:
        try:
            await on_finish(run)
        except Exception:
            logger.warning(
                "webhook run status write failed key=%s",
                run.key,
                exc_info=True,
            )

    def live_tasks(self) -> list[asyncio.Task]:
        """Задачи своих прогонов — и идущих, и дописывающих итог (их
        дожидаются до закрытия хранилища)."""
        with self._lock:
            runs = [*self._runs.values(), *self._finishing.values()]
        return [run.task for run in runs if run.task]

    def cancel_all(self) -> list[asyncio.Task]:
        """Прервать идущие прогоны (остановка сервера): статус не меняется,
        отметка хода остаётся — задачу продолжит восстановление.
        Возвращает снятые задачи — их дожидаются до закрытия хранилища."""
        with self._lock:
            running = list(self._runs.values())
            for run in running:
                run.shutdown = True
        for run in running:
            if run.task:
                run.task.cancel()
        # Итог, который уже пишется, не прерывается — его дожидаются.
        return self.live_tasks()

    async def stop(self, *, system: str, task_id: str) -> bool:
        """Снять свой прогон. True — когда текущая работа хода снята.

        Звать из корутины цикла прогона: ожидание отдаёт цикл.
        Потолок — ``TimeoutError``, не True.
        """
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
        if run is None:
            return False
        run.executor.stop_run()
        gate = getattr(run.executor, "user_stop", None)
        if gate is not None:
            await gate.await_settled()
        return True
