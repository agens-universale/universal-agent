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


Hook = Callable[[ActiveRun], Awaitable[None]]


class AgentRunRegistry:
    """Брони сборки контекста и свои живые прогоны по ключу задачи."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, ActiveRun] = {}
        #: Ключ → когда взята бронь: контекст задачи строится этим процессом.
        self._reservations: dict[str, float] = {}

    def building_since(self, *, system: str, task_id: str) -> float | None:
        """С каких пор этот процесс строит контекст задачи (или ``None``)."""
        key = _make_registry_key(system, task_id)
        with self._lock:
            return self._reservations.get(key)

    def is_running(self, *, system: str, task_id: str) -> bool:
        key = _make_registry_key(system, task_id)
        with self._lock:
            return key in self._runs

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
        on_start: Hook,
        on_finish: Hook,
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
        )
        with self._lock:
            self._runs[key] = run
        await on_start(run)
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
            # Снятие — не отказ: остановка сервера, явная отмена.
            run.ending = "cancelled"
            raise
        except (ThreadBusy, InboxDrained):
            # Тред взял другой инстанс (или он уже всё забрал): доведёт он.
            run.ending = "handed_off"
        except Exception as exc:
            run.ending, run.error = "failed", exc
        finally:
            with self._lock:
                self._runs.pop(run.key, None)
            await self._afinish(run, on_finish)
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
            run.ending = "completed"
            with self._lock:
                self._runs.pop(run.key, None)
            if not await run.executor.ainbox_left():
                return
            with self._lock:
                if run.key in self._runs or run.key in self._reservations:
                    return  # следующий прогон этого процесса уже стартует
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
        with self._lock:
            return [run.task for run in self._runs.values() if run.task]

    def cancel_all(self) -> list[asyncio.Task]:
        """Снять идущие прогоны (остановка сервера): статус — cancelled.
        Возвращает снятые задачи — их дожидаются до закрытия хранилища."""
        tasks = self.live_tasks()
        for task in tasks:
            task.cancel()
        return tasks

    def stop(self, *, system: str, task_id: str) -> bool:
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
        if run is None:
            return False
        run.executor.stop_run()
        return True
