import asyncio
import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


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


class AgentRunRegistry:
    """
    Реестр активных запусков агента (в памяти процесса).

    Ключ: (system, task_id) -> тред с `AgentExecutor.arun()`.
    Если запуск уже идёт — можно докидывать сообщения в процесс через `add_user_message()`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, ActiveRun] = {}
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
            key=lambda kv: (kv[1].finished_at or 0.0),
        )
        overflow = len(items) - self._completed_max
        for i in range(max(0, overflow)):
            self._completed.pop(items[i][0], None)

    def get_status(self, *, system: str, task_id: str) -> Optional[ActiveRun]:
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
            if run is not None:
                return run
            self._cleanup_completed_locked()
            return self._completed.get(key)

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
            existing = self._runs.get(key)
            if existing is not None and not existing.done:
                return existing

            executor = executor_factory(ctx)
            started_at = time.time()
            run_ref: ActiveRun | None = None

            async def _runner() -> None:
                nonlocal run_ref
                try:
                    result = await executor.arun(
                        [initial_user_message],
                        thread_id=thread_id,
                        system=system,
                        verbose=verbose,
                    )
                    with self._lock:
                        if run_ref is not None:
                            run_ref.done = True
                            run_ref.result = result
                            run_ref.last_activity_at = time.time()
                            run_ref.finished_at = time.time()
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
                    raise
                except BaseException as e:  # noqa: BLE001
                    with self._lock:
                        if run_ref is not None:
                            run_ref.done = True
                            run_ref.error = e
                            run_ref.last_activity_at = time.time()
                            run_ref.finished_at = time.time()
                finally:
                    with self._lock:
                        current = self._runs.get(key)
                        if current is run_ref:
                            del self._runs[key]
                        if run_ref is not None:
                            self._completed[key] = run_ref
                            self._cleanup_completed_locked()

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
                return False
            run.last_activity_at = time.time()

        run.executor.add_message(make_message(content))
        return True

    def stop(self, *, system: str, task_id: str) -> bool:
        key = _make_registry_key(system, task_id)
        with self._lock:
            run = self._runs.get(key)
            if run is None or run.done:
                return False
        run.executor.stop_run()
        return True


agent_run_registry = AgentRunRegistry()
