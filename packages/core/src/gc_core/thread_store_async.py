"""Асинхронный порт хранилища тредов: ядро ждёт хранилище нативно.

Зачем. Контракт хранилища (``ThreadStoreBase``) синхронный, и это правильно по умолчанию:
файлы и локальная база отвечают за микросекунды, оборачивать их не во что.
Но встраивающий с ``redis.asyncio`` или ``asyncpg`` синхронного клиента не
держит вовсе — а сегодня его реализацию ядро вызвать НЕ МОЖЕТ, и хуже того,
вызывает молча неправильно: двенадцать из шестнадцати обращений лежат в обычных
``def``, где корутина отбрасывается без ошибки и без лога. Падает лишь часть
путей, поэтому поломка выглядит как работающий агент с пустой трассой
субагентов, незагорающимися индикаторами и неверными токенами.

Форма решения — та же, что уже применена к ``FsBackend``: не расширять
синхронный контракт async-двойниками (это сломало бы каждую внешнюю
реализацию), а завести отдельный ``Protocol`` и одну фабрику-диспетчер. См.
``backends/async_protocol.aexec_port`` — здесь ровно тот же приём.

Внутренним контрактом ядра становится АСИНХРОННЫЙ порт: ядро всюду пишет
``await port.aload_meta(...)``, а синхронные хранилища заворачиваются в
адаптер. Так у async-пути не может быть «второго, забытого» вызывающего — он
единственный.

Почему адаптер зовёт синхронное хранилище ИНЛАЙН, а не через поток
-----------------------------------------------------------------

``asyncio.to_thread`` здесь выглядит очевидным и является ошибкой, проверенной
запуском:

* ``save_thread_meta`` пишет во ФИКСИРОВАННЫЙ путь ``{meta_path}.tmp``, а
  ``atomic_replace`` на POSIX пробрасывает ошибку сразу — два потока на одном
  треде дают ``FileNotFoundError``;
* дефолтный пул ``to_thread`` — общий (14 воркеров на этой машине), тот же,
  куда диспетчер уводит синхронные ЧУЖИЕ тулы; хранилище конкурировало бы с
  ними за места.

Для файлового хранилища выигрыш всё равно отсутствует: замерено 3.44 мс
максимального лага инлайн против 1.72 мс в потоке — оба на два порядка ниже
бюджета в 150 мс.

Настоящая опасность — не файлы, а СИНХРОННОЕ СЕТЕВОЕ хранилище (``redis-py``,
к которому тянутся первым). Отличить его от быстрого локального статически
нельзя, поэтому адаптер меряет первые вызовы и один раз предупреждает. Закон,
выведенный из кода и подтверждённый замером на приёмочном сценарии::

    максимальный лаг цикла ≈ 11 × RTT + 29 мс

Одиннадцать — потому что 25 обращений за прогон укладываются в три блока
подряд (11, 11 и 3) без возврата в цикл. Отсюда порог предупреждения: при
2 мс на вызов выходит ≈ 51 мс, треть бюджета.
"""
from __future__ import annotations

import inspect
import weakref
import time
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, runtime_checkable

from gc_core.thread_store_base import ASYNC_CONTRACT_METHODS, CONTRACT_METHODS
from gc_core.thread_store_types import (
    MessageKey,
    SubagentTraceRecord,
    ThreadState,
)

#: Дольше этого МЕДИАННЫЙ вызов хранилища держит цикл заметно: одиннадцать
#: подряд съедают треть бюджета приёмки. Порог предупреждения, не отказа —
#: медленное хранилище остаётся рабочим.
#:
#: Смотрим медиану окна, а не отдельный вызов, и это не перестраховка: на живом
#: стенде ``FileThreadStore`` даёт медиану 0,29 мс при отдельных всплесках до
#: 2,8 мс (запись меты, конкуренция за диск между агентами). Порог по одному
#: вызову ловил такой всплеск и советовал владельцу ОБЫЧНЫХ ФАЙЛОВ переписать
#: хранилище на асинхронное. Шумное предупреждение перестают читать.
SLOW_CALL_WARN_SEC = 0.002

#: Сколько первых вызовов мерить, прежде чем судить по медиане. Дальше замер
#: выключается: он сам стоит денег на горячем шве (``persist_batch`` — раз за
#: итерацию цикла и раз за тул-колл).
#:
#: Восемь, а не больше: окно должно закрыться в ЛЮБОМ реальном прогоне. Обычный
#: делает 25 обращений, но короткий — меньше, и при широком окне он не
#: предупредил бы никогда. Восьми хватает на устойчивую медиану.
SLOW_CALL_PROBE_COUNT = 8


class ThreadStoreContractError(TypeError):
    """Объект не годится ни как ``ThreadStore``, ни как ``AsyncThreadStore``."""


@runtime_checkable
class AsyncThreadStore(Protocol):
    """Хранилище треда для асинхронного клиента.

    Те же одиннадцать операций, что у :class:`~gc_core.thread_store_base.ThreadStoreBase`,
    с теми же свойствами и той же семантикой — отличается только приставка
    ``a`` и корутинность. Свойства читать там: идемпотентность
    ``aappend_messages``, мутация ``known_keys`` на месте, ``max_seq`` как
    НОМЕР, пустая секция ``runtime`` убирается целиком, шесть полей записи
    трассы, ``id`` на чтении.

    Проверить свою реализацию:
    ``gc_testkit.thread_store_conformance.assert_conforms_async``.
    """

    async def aload_messages(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]: ...

    async def aload_thread_state(
        self, system_id: str, thread_id: str
    ) -> ThreadState: ...

    async def aload_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]: ...

    async def aappend_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        known_keys: Optional[Set[MessageKey]] = None,
        start_seq: int = 0,
    ) -> int: ...

    async def asave_meta(
        self, system_id: str, thread_id: str, meta: Dict[str, Any]
    ) -> None: ...

    async def apatch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None: ...

    async def aupdate_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None: ...

    async def adiscard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool: ...

    async def areset(self, system_id: str, thread_id: str) -> List[str]: ...

    async def aappend_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> SubagentTraceRecord: ...

    async def aread_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]: ...


class SyncThreadStoreAdapter:
    """Асинхронный порт поверх синхронного ``ThreadStore``.

    Совместимость, а не производительность: ``FileThreadStore`` и любая чужая
    синхронная реализация продолжают работать без единой правки у автора.
    Синхронный метод зовётся ИНЛАЙН — почему не через поток, разобрано в
    докстроке модуля.

    Первые :data:`SLOW_CALL_PROBE_COUNT` вызовов замеряются: синхронное СЕТЕВОЕ
    хранилище статически не отличить от быстрого локального, а разница между
    ними — 29 мс и 276 мс лага цикла.
    """

    def __init__(self, store: Any, *, warn: Optional[Any] = None) -> None:
        self._store = store
        self._probes_left = SLOW_CALL_PROBE_COUNT
        self._warned = False
        self._warn = warn
        self._samples: list = []

    def set_warn(self, warn: Any) -> None:
        """Подставить приёмник предупреждений уже выданному адаптеру.

        Адаптер на хранилище один и живёт дольше вызова, а приёмник у первого
        вызывающего часто пуст: контекст с логгером приходит позже. Без этого
        предупреждение о медленном хранилище терялось ровно там, где его есть
        кому показать.
        """
        self._warn = warn

    @property
    def store(self) -> Any:
        """Обёрнутое синхронное хранилище."""
        return self._store

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._store, name)
        if self._probes_left <= 0:
            return method(*args, **kwargs)

        self._probes_left -= 1
        started = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            self._samples.append((time.perf_counter() - started, name))
            if self._probes_left == 0 and not self._warned:
                self._warned = True
                self._judge_samples()

    def _judge_samples(self) -> None:
        """Медленно ли хранилище В ЦЕЛОМ — по медиане окна, а не по всплеску."""
        import statistics

        if not self._samples:
            return
        median = statistics.median(spent for spent, _ in self._samples)
        if median <= SLOW_CALL_WARN_SEC:
            return
        worst_name = max(self._samples)[1]
        self._report_slow(worst_name, median)

    def _report_slow(self, name: str, median: float) -> None:
        text = (
            f"thread_store_blocking_loop store={type(self._store).__name__} "
            f"slowest_method={name} median_ms={median * 1000:.1f} "
            f"threshold_ms={SLOW_CALL_WARN_SEC * 1000:.0f} "
            f"samples={len(self._samples)} "
            "hint=implement_AsyncThreadStore_for_network_backed_storage"
        )
        if self._warn is not None:
            self._warn(text)
            return
        import logging

        logging.getLogger("gc_core.thread_store").warning(text)

    # --- чтение ------------------------------------------------------------

    async def aload_messages(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        return self._call("load_messages", system_id, thread_id)

    async def aload_thread_state(
        self, system_id: str, thread_id: str
    ) -> ThreadState:
        return self._call("load_thread_state", system_id, thread_id)

    async def aload_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]:
        return self._call("load_meta", system_id, thread_id)

    # --- запись ------------------------------------------------------------

    async def aappend_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        known_keys: Optional[Set[MessageKey]] = None,
        start_seq: int = 0,
    ) -> int:
        return self._call(
            "append_messages", system_id, thread_id, messages,
            known_keys=known_keys, start_seq=start_seq,
        )

    async def asave_meta(
        self, system_id: str, thread_id: str, meta: Dict[str, Any]
    ) -> None:
        self._call("save_meta", system_id, thread_id, meta)

    async def apatch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._call(
            "patch_runtime_flag", system_id, thread_id, flag,
            active=active, session_meta=session_meta,
        )

    async def aupdate_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        self._call("update_runtime_meta", system_id, thread_id, patch)

    async def adiscard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool:
        return self._call("discard_message", system_id, thread_id, message_id)

    async def areset(self, system_id: str, thread_id: str) -> List[str]:
        return self._call("reset", system_id, thread_id)

    # --- трассы субагентов -------------------------------------------------

    async def aappend_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> SubagentTraceRecord:
        return self._call(
            "append_subagent_trace", system_id, thread_id,
            parent_call_id=parent_call_id, child_run_id=child_run_id,
            kind=kind, payload=payload,
        )

    async def aread_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        return self._call("read_subagent_trace", system_id, thread_id)


def is_async_thread_store(store: Any) -> bool:
    """Реализует ли объект асинхронный контракт целиком.

    Требуются ВСЕ одиннадцать корутинных методов. Половинчатая реализация —
    самый неприятный случай: часть записей уходит в базу, часть теряется молча,
    и тред расщепляется без единого признака ошибки.
    """
    return all(
        inspect.iscoroutinefunction(getattr(store, name, None))
        for name in ASYNC_CONTRACT_METHODS
    )


#: Адаптеры, уже выданные на конкретное хранилище.
#:
#: Ключ — сам объект store, слабой ссылкой: адаптер не должен продлевать ему
#: жизнь. Кэш нужен из-за окна проб: адаптер меряет первые
#: ``SLOW_CALL_PROBE_COUNT`` обращений и один раз предупреждает о медленном
#: сетевом хранилище. Пока адаптер создавался НА КАЖДЫЙ ``afrom_config`` и на
#: каждую запись трассы субагента, окно не закрывалось никогда: обещанное
#: «предупреждение один раз» превращалось в «раз за прогон», а медиана
#: считалась по write-heavy прологу, где порог в 2 мс перешагивает и локальный
#: файл.
_ADAPTERS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def _adapt_sync_store(store: Any, warn: Optional[Any]) -> AsyncThreadStore:
    """Обернуть полностью синхронное хранилище; смешанное — отказ."""
    half_async = [
        name for name in CONTRACT_METHODS
        if inspect.iscoroutinefunction(getattr(store, name, None))
    ]
    if half_async:
        raise ThreadStoreContractError(
            f"{type(store).__name__}: часть методов объявлена async def "
            f"под СИНХРОННЫМИ именами: {', '.join(half_async)}.\n"
            "  Смешивать нельзя: синхронные вызывающие отбросят корутину без "
            "ошибки,\n  и часть записей треда потеряется молча.\n"
            "  Либо все методы синхронные (ThreadStoreBase), либо все "
            "асинхронные с приставкой a (AsyncThreadStore)."
        )
    adapter = SyncThreadStoreAdapter(store, warn=warn)
    try:
        _ADAPTERS[store] = adapter
    except TypeError:
        pass                          # без weakref — просто без кэша
    return adapter


def _contract_error(store: Any, missing: list[str]) -> ThreadStoreContractError:
    half_async_named = [
        name for name in ASYNC_CONTRACT_METHODS
        if inspect.iscoroutinefunction(getattr(store, name, None))
    ]
    if half_async_named:
        detail = (
            f"\n  Асинхронных методов найдено: {len(half_async_named)} из "
            f"{len(ASYNC_CONTRACT_METHODS)} — реализация неполна.\n"
            f"  Не хватает: "
            f"{', '.join(n for n in ASYNC_CONTRACT_METHODS if n not in half_async_named)}"
        )
    else:
        detail = f"\n  Не хватает синхронных методов: {', '.join(missing)}"
    return ThreadStoreContractError(
        f"{type(store).__name__} не годится как хранилище тредов.{detail}\n"
        "  Нужен либо ThreadStoreBase (одиннадцать синхронных методов), либо "
        "AsyncThreadStore\n  (те же одиннадцать с приставкой a, все корутинные). "
        "Проверить себя:\n"
        "  gc_testkit.thread_store_conformance.assert_conforms / assert_conforms_async"
    )


def athread_store(store: Any, *, warn: Optional[Any] = None) -> AsyncThreadStore:
    """Асинхронный порт для хранилища: свой, если он есть, иначе адаптер.

    Единственная точка, где решается форма чужого объекта. Отказ здесь громкий
    и адресный — он приходит В МОМЕНТ ПРОВОДКИ, а не на 630-й строке чужого
    файла, куда ведёт нынешний ``TypeError: cannot unpack non-iterable
    coroutine object``.

    Адаптер на одно хранилище ОДИН — см. ``_ADAPTERS``.
    """
    if is_async_thread_store(store):
        return store

    try:
        cached = _ADAPTERS.get(store)
    except TypeError:
        cached = None                     # не-хешируемое хранилище: без кэша
    if cached is not None:
        # Кэш отдаёт ТОТ ЖЕ адаптер, но приёмник предупреждений мог смениться:
        # первый вызов часто идёт без него, а следующий приходит из контекста
        # с логгером. Молча отдавая старый адаптер, мы теряли бы предупреждение
        # о медленном хранилище именно там, где его есть кому показать.
        if warn is not None:
            cached.set_warn(warn)
        return cached

    from gc_core.thread_store_base import CONTRACT_METHODS

    missing = [
        name for name in CONTRACT_METHODS
        if not callable(getattr(store, name, None))
    ]
    if not missing:
        return _adapt_sync_store(store, warn)

    raise _contract_error(store, missing)


__all__ = [
    "ASYNC_CONTRACT_METHODS",
    "AsyncThreadStore",
    "SLOW_CALL_PROBE_COUNT",
    "SLOW_CALL_WARN_SEC",
    "SyncThreadStoreAdapter",
    "ThreadStoreContractError",
    "athread_store",
    "is_async_thread_store",
]
