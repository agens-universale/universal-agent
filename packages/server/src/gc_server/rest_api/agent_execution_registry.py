import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _take_tail(executor: Any) -> list:
    """Хвост прогона: отдано исполнителю, а до агента не дошло.

    Подробности — `AgentExecutor.take_undelivered_messages`. Исполнитель без
    этого метода хвоста не отдаёт — доигрывать нечего.
    """
    take = getattr(executor, "take_undelivered_messages", None)
    return list(take()) if take is not None else []


def _make_registry_key(system: str, task_id: str) -> str:
    sys_norm = (system or "").strip().lower()
    task_norm = (task_id or "").strip()
    if not sys_norm:
        raise ValueError("system должен быть непустой строкой")
    if not task_norm:
        raise ValueError("task_id должен быть непустой строкой")
    return f"{sys_norm}:{task_norm}"


@dataclass
class _Reservation:
    """Бронь ключа задачи на время сборки контекста."""

    #: Сборка закончилась — любым исходом.
    #:
    #: `asyncio.Event`, а не общий `Future`: ждущих бывает несколько, и
    #: отмена одного из них (клиент отвалился, сервер гасят, вышел потолок
    #: ожидания) отменила бы общий `Future` и разбудила остальных исключением,
    #: а у `Event` каждый ждёт на своём. Исход сборки ждущим не нужен — они
    #: смотрят реестр заново; а `Event.set()` повторяем и не требует забрать
    #: результат. Создаётся, ждётся и выставляется на loop сервера: все
    #: эндпоинты асинхронные.
    done: asyncio.Event
    #: Когда бронь взята. Прогона во время сборки ещё нет, и статус «строится»
    #: отдаёт это время в `started_at` и `last_activity_at` — иначе оба были
    #: null, и зависшая сборка для сторожа по активности не старела никогда.
    taken_at: float


@dataclass
class ActiveRun:
    key: str
    system: str
    task_id: str
    executor: Any
    #: Прогон агента. С переездом ядра на asyncio это ``asyncio.Task`` на loop
    #: сервера, а не поток: uvicorn и так асинхронный, и держать поток на
    #: каждый висящий запрос было нужно только потому, что ядро блокировало.
    #: Поле оставлено под прежним именем — его читают снаружи как «то, чем
    #: идёт прогон».
    thread: Any
    started_at: float
    finished_at: Optional[float]
    thread_id: Optional[str]
    last_activity_at: float
    done: bool = False
    result: Any = None
    error: Optional[BaseException] = None
    #: Прогон СНЯЛИ, а не он упал: остановка uvicorn, явная отмена таски.
    #: Отдельно от ``error``, иначе клиент не отличит отказ агента от штатного
    #: завершения сервера — раньше ``except BaseException`` записывал отмену
    #: в ``error`` и репортил прогон упавшим.
    cancelled: bool = False
    #: Прогон остановили через `stop`. Флаг реестра, а не только исполнителя:
    #: остановленный прогон не доигрывает хвост, а `stop_run` между ходами
    #: попадает в state уже закончившегося хода и сам ничего не остановит.
    stop_requested: bool = False


def _finish_turn_locked(
    run_ref: ActiveRun | None, result: Any, tail: list
) -> bool:
    """Обновить исход хода под блокировкой реестра; вернуть необходимость повтора."""
    stopped = run_ref is not None and run_ref.stop_requested
    replay = bool(tail) and not stopped
    if run_ref is not None:
        # Конец хода — активность прогона, а при хвосте
        # это ещё и начало следующего. Иначе на всё
        # время хвостового хода сторож по активности
        # видел время последнего вебхука.
        run_ref.last_activity_at = time.time()
        if not replay:
            run_ref.done = True
            # Итог прогона задачи — ответ последнего хода:
            # хвостовой ход идёт по тому же треду и
            # отвечает с учётом всего, что было до него.
            run_ref.result = result
            run_ref.finished_at = run_ref.last_activity_at
    return replay


class AgentRunRegistry:
    """
    Реестр активных запусков агента (в памяти процесса).

    Ключ: (system, task_id) -> тред с `AgentExecutor.arun()`.
    Если запуск уже идёт — можно докидывать сообщения в процесс через `add_user_message()`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, ActiveRun] = {}
        #: Задачи, у которых контекст ещё строится: ключ → бронь. Бронь держит
        #: ключ занятым с ПЕРВОГО запроса. Сообщений она не копит: вебхук,
        #: упёршийся в бронь, ждёт конца сборки и проходит путь заново (см.
        #: `await_build`). Прежняя бронь со списком сообщений при отказе сборки
        #: откладывала их до следующего вебхука задачи — без срока, без потолка
        #: и без гарантии, что такой вебхук вообще будет.
        self._reservations: dict[str, _Reservation] = {}
        self._completed: dict[str, ActiveRun] = {}
        self._completed_ttl_s = 15 * 60
        self._completed_max = 500

    def _cleanup_completed_locked(self) -> None:
        now = time.time()
        expired_keys: list[str] = []
        for key, run in self._completed.items():
            if run.finished_at is None:
                continue
            if now - run.finished_at > self._completed_ttl_s:
                expired_keys.append(key)
        for key in expired_keys:
            self._completed.pop(key, None)

        if len(self._completed) <= self._completed_max:
            return
        # Удаляем самые старые завершённые.
        items = sorted(
            self._completed.items(),
            key=lambda kv: kv[1].finished_at or 0.0,
        )
        overflow = len(items) - self._completed_max
        for i in range(max(0, overflow)):
            self._completed.pop(items[i][0], None)

    def _get_status_locked(self, key: str) -> Optional[ActiveRun]:
        run = self._runs.get(key)
        if run is not None:
            return run
        self._cleanup_completed_locked()
        return self._completed.get(key)

    def get_status(self, *, system: str, task_id: str) -> Optional[ActiveRun]:
        key = _make_registry_key(system, task_id)
        with self._lock:
            return self._get_status_locked(key)

    def get_status_snapshot(
        self, *, system: str, task_id: str
    ) -> tuple[Optional[float], Optional[ActiveRun]]:
        """Статус задачи для клиента одним снимком: (с каких пор строится контекст, прогон).

        Первое — время брони, если контекст задачи сейчас строится, иначе None.

        Бронь проверяется ПЕРВОЙ и под тем же замком, что и записи прогонов.
        Бронь выдаётся ровно тогда, когда прошлый прогон закончился, а его
        запись живёт в `_completed` ещё 15 минут: спроси эндпоинт сначала
        прогон, а бронь потом — клиент, которому только что ответили `queued`,
        получал `completed` и `result` ПРОШЛОГО прогона. Две отдельные
        проверки к тому же видели бы реестр в разные моменты.
        """
        key = _make_registry_key(system, task_id)
        with self._lock:
            reservation = self._reservations.get(key)
            if reservation is not None:
                return reservation.taken_at, None
            return None, self._get_status_locked(key)

    def reserve(self, *, system: str, task_id: str) -> bool:
        """Занять ключ задачи ДО сборки контекста. False — ключ уже занят.

        Сборка идёт через `await` (дискавери MCP по сети), и между проверкой
        «идёт ли прогон» и стартом было окно: второй вебхук на ту же задачу
        видел ключ свободным, строил ВТОРОЙ контекст, оба запроса отвечали
        `started`, а сообщение второго до агента не доходило. Контекст
        проигравшего оставался висеть — с логгером и MCP-сессиями, которые
        некому закрыть.

        Взявший бронь обязан снять её `release_reservation` при любом исходе
        сборки: этим он будит тех, кто ждёт её в `await_build`.
        """
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
            if (run is not None and not run.done) or key in self._reservations:
                return False
            self._reservations[key] = _Reservation(
                done=asyncio.Event(), taken_at=time.time()
            )
            return True

    def release_reservation(self, *, system: str, task_id: str) -> None:
        """Снять бронь и разбудить ждущих — при ЛЮБОМ исходе сборки.

        Снимает только владелец брони, один раз, из `finally` своей сборки:
        новую бронь по ключу можно взять лишь после этого, так что чужую здесь
        снять нельзя. Синхронно и без `await` намеренно: `finally` исполняется
        и под раскруткой `CancelledError`, где ожидание съелось бы вместе с
        отменой, а ждущие остались бы спать навсегда.
        """
        key = _make_registry_key(system, task_id)
        with self._lock:
            reservation = self._reservations.pop(key, None)
        if reservation is not None:
            reservation.done.set()

    async def await_build(self, *, system: str, task_id: str) -> None:
        """Дождаться конца чужой сборки контекста по задаче — любого исхода.

        После пробуждения вызывающий проходит путь заново: прогон стартовал —
        его сообщение уходит в прогон (`queued`); сборка упала — вызывающий
        берёт бронь и строит сам. Ждущие просыпаются в порядке ожидания — в
        нём же их сообщения и уходят агенту, после стартового сообщения
        строившего. Брони нет — сборка уже закончилась, ждать нечего.

        Потолка здесь нет: он у эндпоинта, чей HTTP-запрос открыт всё время
        ожидания. Отмена ожидания — по потолку или по отвалу клиента — брони
        не трогает: строитель её не заметит.
        """
        key = _make_registry_key(system, task_id)
        with self._lock:
            reservation = self._reservations.get(key)
        if reservation is not None:
            await reservation.done.wait()

    def _complete_run_locked(
        self, key: str, run_ref: ActiveRun | None
    ) -> None:
        current = self._runs.get(key)
        if current is run_ref:
            del self._runs[key]
        if run_ref is not None:
            self._completed[key] = run_ref
            self._cleanup_completed_locked()

    async def astart_if_absent(
        self,
        *,
        system: str,
        task_id: str,
        executor_factory: Any,
        ctx: Any,
        initial_user_message: str,
        thread_id: Optional[str] = None,
        verbose: Optional[bool] = None,
    ) -> ActiveRun:
        key = _make_registry_key(system, task_id)
        with self._lock:
            self._cleanup_completed_locked()
            # Бронь здесь не трогаем: её снимает владелец в `finally` сборки,
            # и ждущие просыпаются уже при зарегистрированном прогоне — их
            # сообщения уходят в него через `add_user_message`.
            existing = self._runs.get(key)
            if existing is not None and not existing.done:
                return existing

            executor = executor_factory(ctx)
            started_at = time.time()
            run_ref: ActiveRun | None = None

            async def _runner() -> None:
                nonlocal run_ref
                messages: list = [initial_user_message]
                run_kwargs: dict = {}
                # Хвост, взятый у исполнителя и ещё не доставленный: его несёт
                # следующий ход. Исполнитель его уже не вернёт — для него это
                # аргументы `arun`, а не очередь. Умрёт ход, который его несёт, —
                # даже в сетапе, до цикла, — хвост потерян, и считать его надо
                # здесь. Доставлен он, когда этот ход закончился: тогда место
                # занимает следующий взятый хвост.
                in_flight: list = []
                # Хвост, который доставить уже некуда, и чем кончился прогон.
                dropped: list = []
                ending = ""
                try:
                    while True:
                        result = await executor.arun(
                            messages,
                            thread_id=thread_id,
                            system=system,
                            verbose=verbose,
                            **run_kwargs,
                        )
                        # Хвост — легшее в очередь после последнего забора её
                        # циклом: пока прогон сохранял тред и сносил MCP, он был
                        # «жив», и вебхуки получали `queued`. Забрать хвост и
                        # объявить прогон законченным — ОДИН шаг под замком и
                        # без единого `await`: `add_user_message` смотрит `done`
                        # под тем же замком, так что сообщение либо попадёт в
                        # хвост, либо увидит `done` и уйдёт новым прогоном.
                        with self._lock:
                            tail = _take_tail(executor)
                            # С этой строки хвост — только здесь.
                            in_flight = tail
                            replay = _finish_turn_locked(run_ref, result, tail)
                            if not replay:
                                dropped, ending = tail, "stopped"
                        if not replay:
                            break
                        # Доигрываем хвост отдельным ходом того же исполнителя
                        # по тому же треду: на следующий старт сервер строит
                        # НОВЫЙ исполнитель, и оставленное в этом пропало бы.
                        from gc_core.agent_executor import (
                            undelivered_as_arun_args,
                        )

                        messages, run_kwargs = undelivered_as_arun_args(tail)
                except asyncio.CancelledError:
                    # Отмена — не отказ прогона. Так выглядит остановка uvicorn
                    # и явная отмена таски; помечать это как failed значит
                    # врать клиенту и мешать отличить падение от штатного
                    # завершения сервера. Дальше отмену пробрасываем: гасить её
                    # здесь — значит соврать уже планировщику.
                    with self._lock:
                        if run_ref is not None:
                            run_ref.done = True
                            run_ref.cancelled = True
                            run_ref.last_activity_at = time.time()
                            run_ref.finished_at = time.time()
                        dropped = [*in_flight, *_take_tail(executor)]
                        ending = "cancelled"
                    raise
                except BaseException as e:
                    with self._lock:
                        if run_ref is not None:
                            run_ref.done = True
                            run_ref.error = e
                            run_ref.last_activity_at = time.time()
                            run_ref.finished_at = time.time()
                        dropped = [*in_flight, *_take_tail(executor)]
                        ending = "failed"
                finally:
                    with self._lock:
                        self._complete_run_locked(key, run_ref)
                    if dropped:
                        # Хвост остановленного, упавшего или снятого прогона не
                        # доигрываем: это отменило бы остановку или спрятало
                        # отказ за статусом следующего хода. Потеря таких
                        # сообщений — часть исхода прогона, но не молчаливая.
                        # В счёт входит и батч хвоста, с которым умер ход.
                        logger.warning(
                            "run_tail_dropped system=%s task_id=%s ending=%s count=%d",
                            system,
                            task_id,
                            ending,
                            len(dropped),
                            extra={
                                "event": "run_tail_dropped",
                                "system": system,
                                "task_id": task_id,
                                "ending": ending,
                                "count": len(dropped),
                            },
                        )
                    # Логгер прогона `logging` держит до конца процесса, а
                    # сервер ведёт прогоны без счёта: без этого каждый запрос
                    # оставлял по логгеру с открытым файлом.
                    from gc_core.utils.logging_module import release_run_logger

                    release_run_logger(getattr(ctx, "logger", None))

            # Таска, а не поток: эндпоинт уже на loop, а ядро больше не блокирует.
            t = asyncio.get_running_loop().create_task(
                _runner(), name=f"agent-run:{key}"
            )
            run_ref = ActiveRun(
                key=key,
                system=system,
                task_id=task_id,
                executor=executor,
                thread=t,
                started_at=started_at,
                finished_at=None,
                thread_id=thread_id,
                last_activity_at=started_at,
            )
            self._runs[key] = run_ref
            return run_ref

    def add_user_message(
        self,
        *,
        system: str,
        task_id: str,
        make_message: Any,
        content: str,
    ) -> bool:
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
            if run is None or run.done:
                # Прогона нет — даже если контекст задачи сейчас строится:
                # такое сообщение ждёт конца сборки у эндпоинта (`await_build`),
                # а не копится здесь.
                return False
            run.last_activity_at = time.time()
            # Кладём под замком: прогон объявляется законченным под ним же и
            # только при пустом хвосте. Сообщение либо ляжет в очередь до этой
            # проверки и будет доиграно, либо увидит `done` — третьего нет.
            run.executor.add_message(make_message(content))
        return True

    def stop(self, *, system: str, task_id: str) -> bool:
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
            if run is None or run.done:
                return False
            run.stop_requested = True
        run.executor.stop_run()
        gate = getattr(run.executor, "user_stop", None)
        if gate is not None:
            # С другого потока ждём снятие. С потока loop ждать нельзя:
            # снятие исполняется на нём же.
            gate.wait_if_other_thread()
        return True


agent_run_registry = AgentRunRegistry()
