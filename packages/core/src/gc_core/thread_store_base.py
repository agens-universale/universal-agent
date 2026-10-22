"""Базовый класс хранилища тредов — рекомендуемый способ реализовать контракт.

    from gc_core.thread_store_base import ThreadStoreBase

    class PostgresThreadStore(ThreadStoreBase):
        def load_messages(self, system_id, thread_id): ...
        # ...остальные десять

Это ЕДИНСТВЕННОЕ объявление контракта. Рядом стоял ещё ``ThreadStore``
(``Protocol``) — те же одиннадцать методов второй копией, — и отдельный
кортеж имён третьей. Копии никто не сверял. Теперь список выводится отсюда же
(``CONTRACT_METHODS`` ниже), а протокол снят.

Почему именно ABC, хотя у протокола было два преимущества:

**Инварианты.** У хранилища есть поведение, одинаковое для всех
реализаций и не выводимое из сигнатур: штампы meta и правило «снятие последнего
флага убирает секцию ``runtime`` целиком». Пока их держать было негде, две
НАШИ СОБСТВЕННЫЕ реализации разъехались ровно здесь: ``FileThreadStore``
штамповал meta на каждом пути записи, а ``MemoryThreadStore`` — только в
``save_meta``. Обе проходили конформанс-набор, потому что и он проверял штампы
только у ``save_meta``. Стоило бы это чужому хранилищу дорого: без
``state_schema_version`` следующий прогон ОТКАЖЕТСЯ читать тред, и заметно это
лишь на ВТОРОМ прогоне — первый проходит нормально.

**Отказ на месте.** У протокола неполная реализация обнаруживается
поздно: ``athread_store`` ловит её при проводке агента, а раньше молчали все.
У ABC она не создастся вовсе: ``TypeError`` с перечнем нереализованных методов
прямо в конструкторе.

Наследование НЕ обязательно. ``athread_store`` по-прежнему принимает любое
хранилище, совпавшее по именам и сигнатурам, — структурная типизация никуда не
делась, и чужой объект из DI-контейнера или прокси наследовать нас не обязан.
База — короткий путь, а не пропуск на входе.

Асинхронный близнец — :class:`AsyncThreadStoreBase`: те же одиннадцать методов
с приставкой ``a``. Смешивать формы нельзя, об этом ``thread_store_async``.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from gc_core.thread_store_types import (
    MessageKey,
    SubagentTraceRecord,
    ThreadState,
    TraceKind,
)


class _ThreadStoreInvariants:
    """Поведение, общее для ЛЮБОГО хранилища. Не абстрактное — готовое.

    Вынесено отдельно от абстрактных методов, чтобы синхронная и асинхронная
    базы делили один экземпляр кода: инварианты у них одни и те же, разной
    может быть только форма вызова.
    """

    def stamp_meta(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Проставить штампы записи. Зовите на КАЖДОМ пути записи meta.

        Не только в ``save_meta``: ``patch_runtime_flag`` и
        ``update_runtime_meta`` тоже создают тред, если его ещё не было, и
        тред без ``state_schema_version`` следующий прогон читать откажется.

        ``created_at`` — момент рождения треда, он не меняется никогда;
        ``updated_at`` — момент этой записи. Копию не делаем: вызывающий и так
        передаёт свой словарь, а лишний ``deepcopy`` на горячем пути записи
        стоит дороже, чем стоит.
        """
        from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper

        stamped = ThreadMetaMapper.ensure_v2_meta(meta)
        stamped["updated_at"] = datetime.now(UTC).isoformat()
        if not stamped.get("created_at"):
            stamped["created_at"] = stamped["updated_at"]
        return stamped

    def apply_runtime_flag(
        self, meta: Dict[str, Any], flag: str, *, active: bool
    ) -> Dict[str, Any]:
        """Взвести или снять флаг в секции ``runtime``, соблюдая инвариант.

        Инвариант: снятие ПОСЛЕДНЕГО флага убирает секцию целиком. «Нет
        флагов» и «пустой словарь» обязаны читаться одинаково — иначе переезд
        на другое хранилище меняет форму meta, а её читают TUI и сервер.

        Имя флага валидируется здесь же: опечатка иначе прошла бы молча и
        завела бы флаг, которого никто не снимет.
        """
        from gc_core.persistence.meta_ownership import (
            resolve_runtime_flag_name,
        )

        flag = resolve_runtime_flag_name(flag)
        runtime = meta.setdefault("runtime", {})
        if active:
            runtime[flag] = True
        else:
            runtime.pop(flag, None)
        if not runtime:
            meta.pop("runtime", None)
        return meta

    def merge_runtime_meta(
        self, meta: Dict[str, Any], patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Долить произвольные значения в ``runtime``, не тронув остальное.

        Копируем ПАТЧ, а не meta: вызывающий может держать переданный словарь
        и менять его дальше, и тогда чужие правки протекли бы в хранилище.
        Остальные секции meta не трогаем — сюда пишут во время работы,
        параллельно с самим прогоном, и read-modify-write целого meta потерял
        бы то, что записал прогон.
        """
        runtime = meta.setdefault("runtime", {})
        runtime.update(copy.deepcopy(patch))
        if not runtime:
            meta.pop("runtime", None)
        return meta


class ThreadStoreBase(_ThreadStoreInvariants, ABC):
    """Одиннадцать методов контракта. Все СИНХРОННЫЕ.

    Асинхронную реализацию ядро вызвать не может: двенадцать из шестнадцати
    обращений лежат в обычных ``def``, и корутина там молча отбрасывается —
    без ошибки, без лога, с потерей записи. Асинхронный клиент
    (``redis.asyncio``, ``asyncpg``) — это :class:`AsyncThreadStoreBase`.

    Смысл каждого метода и восемь поведенческих свойств, которые легко
    нарушить, — в ``thread_store_base`` и в ``docs/sdk/thread-store-contract.md``.
    Проверить себя: ``gc_testkit.thread_store_conformance.assert_conforms``.

    **Необязательные способности** (в контракт не входят, объявляются по
    желанию, ядро ищет их через ``getattr``):

    * ``archive_and_replace(system_id, thread_id, messages)`` — отложить
      прежнюю историю рядом и заменить её. Не объявили — сессия сделает
      ``reset`` + ``append_messages``: то же замещение, но без архива. Так и
      задумано: «архив рядом» осмыслен для файлов, у базы это либо отдельная
      таблица, либо не нужно вовсе;
    * ``threads_dir`` — каталог с файлами, если он вообще есть. У хранилища
      без файлов его нет, и ядро на это рассчитывает.
    """

    def replace_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
    ) -> None:
        """Заместить историю треда, НЕ трогая meta и трассу субагентов.

        Зовётся компакцией у хранилищ, которые не объявляют
        ``archive_and_replace``. Раньше на этом месте стояла пара
        ``reset`` + ``append_messages``, и она делала лишнего:

        * ``reset`` по контракту стирает ВЕСЬ тред, включая
          ``subagent_trace`` — то есть каждая компакция теряла историю
          вызовов субагентов;
        * между ``reset`` и последующим ``save_meta`` треда не существовало
          вовсе: падение процесса в этом окне уносило ``created_at``, секцию
          ``tui`` и ``shared``.

        Умолчание ниже — то же самое замещение, но без потери трассы: она
        читается и возвращается на место. Хранилищу, которое умеет заместить
        историю одной операцией (SQL ``DELETE … INSERT`` в транзакции), стоит
        переопределить метод — это и надёжнее, и быстрее.
        """
        trace = self.read_subagent_trace(system_id, thread_id)
        meta = self.load_meta(system_id, thread_id)
        self.reset(system_id, thread_id)
        self.append_messages(system_id, thread_id, messages)
        if meta:
            self.save_meta(system_id, thread_id, meta)
        self._restore_subagent_trace(system_id, thread_id, trace)

    def _restore_subagent_trace(
        self, system_id: str, thread_id: str, records: Sequence[Dict[str, Any]]
    ) -> None:
        """Вернуть трассу на место после ``reset``. НЕ часть контракта.

        Умолчание идёт через ``append_subagent_trace`` и ТЕРЯЕТ ШТАМПЫ:
        писатель безусловно ставит свежее время и следующий номер. После
        каждой компакции у всей трассы дата равнялась моменту компакции, а
        нумерация начиналась заново — трасса переставала быть хронологией,
        ровно тем, ради чего её и спасали.

        Наши реализации это переопределяют и пишут записи как есть. Чужому
        хранилищу стоит поступить так же — либо переопределить сам
        ``replace_messages`` одной операцией (SQL ``DELETE … INSERT`` в
        транзакции), что и надёжнее, и быстрее.
        """
        for record in records:
            self.append_subagent_trace(
                system_id,
                thread_id,
                parent_call_id=record.get("parent_call_id", ""),
                child_run_id=record.get("child_run_id", ""),
                kind=record.get("kind", ""),
                payload=record.get("payload", {}) or {},
            )

    def update_meta(
        self,
        system_id: str,
        thread_id: str,
        merge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Прочитать мету, ``merge_fn(meta)``, записать; вернуть записанное.

        НЕ часть контракта. Умолчание — ``load_meta`` и ``save_meta`` двумя
        вызовами: запись другого писателя между ними теряется. Хранилищу с
        параллельными писателями меты (TUI рядом с исполнителем) стоит
        переопределить метод одним шагом под своим замком, как файловое.
        """
        meta = merge_fn(self.load_meta(system_id, thread_id) or {})
        self.save_meta(system_id, thread_id, meta)
        return meta

    def ensure_ready(self) -> None:
        """Подготовить хранилище к работе. По умолчанию — ничего не делать.

        Файловому нужно создать каталог; базе, скорее всего, ничего. Зовётся
        один раз при сборке сессии, поэтому дорогие миграции здесь неуместны.
        """
        return

    # --- чтение ------------------------------------------------------------

    @abstractmethod
    def load_messages(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        """История треда в порядке записи. Нет треда — пустой список, не ошибка."""

    @abstractmethod
    def load_thread_state(self, system_id: str, thread_id: str) -> ThreadState:
        """История ВМЕСТЕ со служебным: ключи идемпотентности и последний ``seq``.

        Третий элемент — НОМЕР последнего выданного ``seq``, а не количество
        записей: после ``discard_message`` они расходятся, а номер
        переиспользуется. Оба ``int``, подмену не поймает ни одна проверка
        типов — только этот текст и конформанс-набор.
        """

    @abstractmethod
    def load_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]:
        """Meta треда. Нет треда — пустой словарь."""

    # --- запись ------------------------------------------------------------

    @abstractmethod
    def append_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        known_keys: Optional[Set[MessageKey]] = None,
        start_seq: int = 0,
    ) -> int:
        """Дописать батч идемпотентно; вернуть номер последнего ``seq``.

        ``known_keys`` мутируется НА МЕСТЕ и только записанным: вызывающий
        держит тот же объект и по его приросту понимает, что легло. Защитная
        копия молча задваивает историю на следующем батче.
        """

    @abstractmethod
    def save_meta(
        self, system_id: str, thread_id: str, meta: Dict[str, Any]
    ) -> None:
        """Записать meta целиком. Штампы ставит ХРАНИЛИЩЕ — см. ``stamp_meta``."""

    @abstractmethod
    def patch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Точечно взвести или снять флаг в ``runtime`` — см. ``apply_runtime_flag``."""

    @abstractmethod
    def update_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        """Долить значения в ``runtime`` — см. ``merge_runtime_meta``."""

    @abstractmethod
    def discard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool:
        """Удалить сообщение по ``id``; ``False``, если такого не было."""

    @abstractmethod
    def reset(self, system_id: str, thread_id: str) -> List[str]:
        """Стереть тред; вернуть список того, что удалено (для лога)."""

    # --- трасса субагентов -------------------------------------------------

    @abstractmethod
    def append_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: TraceKind,
        payload: Dict[str, Any],
    ) -> SubagentTraceRecord:
        """Дописать запись трассы; ``ts`` и ``seq`` проставляет хранилище."""

    @abstractmethod
    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        """Трасса субагентов треда в порядке записи."""


class AsyncThreadStoreBase(_ThreadStoreInvariants, ABC):
    """Те же одиннадцать методов с приставкой ``a``, все корутинные.

    Для клиентов, у которых синхронной формы нет вовсе (``redis.asyncio``,
    ``asyncpg``). Ядро ждёт такое хранилище нативно; синхронное оно
    заворачивает в ``SyncThreadStoreAdapter`` само.

    **Смешивать формы нельзя.** Либо все одиннадцать синхронные, либо все
    одиннадцать корутинные: половинчатую реализацию ``athread_store``
    отвергает при создании агента — иначе часть записей ушла бы в базу, а
    часть потерялась молча, потому что синхронные вызывающие корутину просто
    отбрасывают.
    """

    async def areplace_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
    ) -> None:
        """См. :meth:`ThreadStoreBase.replace_messages`."""
        trace = await self.aread_subagent_trace(system_id, thread_id)
        meta = await self.aload_meta(system_id, thread_id)
        await self.areset(system_id, thread_id)
        await self.aappend_messages(system_id, thread_id, messages)
        if meta:
            await self.asave_meta(system_id, thread_id, meta)
        await self._arestore_subagent_trace(system_id, thread_id, trace)

    async def _arestore_subagent_trace(
        self, system_id: str, thread_id: str, records: Sequence[Dict[str, Any]]
    ) -> None:
        """См. :meth:`ThreadStoreBase._restore_subagent_trace`."""
        for record in records:
            await self.aappend_subagent_trace(
                system_id,
                thread_id,
                parent_call_id=record.get("parent_call_id", ""),
                child_run_id=record.get("child_run_id", ""),
                kind=record.get("kind", ""),
                payload=record.get("payload", {}) or {},
            )

    async def aupdate_meta(
        self,
        system_id: str,
        thread_id: str,
        merge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """См. :meth:`ThreadStoreBase.update_meta`."""
        meta = merge_fn(await self.aload_meta(system_id, thread_id) or {})
        await self.asave_meta(system_id, thread_id, meta)
        return meta

    async def aensure_ready(self) -> None:
        """См. :meth:`ThreadStoreBase.ensure_ready`."""
        return

    @abstractmethod
    async def aload_messages(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        """См. :meth:`ThreadStoreBase.load_messages`."""

    @abstractmethod
    async def aload_thread_state(
        self, system_id: str, thread_id: str
    ) -> ThreadState:
        """См. :meth:`ThreadStoreBase.load_thread_state`."""

    @abstractmethod
    async def aload_meta(
        self, system_id: str, thread_id: str
    ) -> Dict[str, Any]:
        """См. :meth:`ThreadStoreBase.load_meta`."""

    @abstractmethod
    async def aappend_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        known_keys: Optional[Set[MessageKey]] = None,
        start_seq: int = 0,
    ) -> int:
        """См. :meth:`ThreadStoreBase.append_messages`."""

    @abstractmethod
    async def asave_meta(
        self, system_id: str, thread_id: str, meta: Dict[str, Any]
    ) -> None:
        """См. :meth:`ThreadStoreBase.save_meta`."""

    @abstractmethod
    async def apatch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """См. :meth:`ThreadStoreBase.patch_runtime_flag`."""

    @abstractmethod
    async def aupdate_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        """См. :meth:`ThreadStoreBase.update_runtime_meta`."""

    @abstractmethod
    async def adiscard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool:
        """См. :meth:`ThreadStoreBase.discard_message`."""

    @abstractmethod
    async def areset(self, system_id: str, thread_id: str) -> List[str]:
        """См. :meth:`ThreadStoreBase.reset`."""

    @abstractmethod
    async def aappend_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: TraceKind,
        payload: Dict[str, Any],
    ) -> SubagentTraceRecord:
        """См. :meth:`ThreadStoreBase.append_subagent_trace`."""

    @abstractmethod
    async def aread_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        """См. :meth:`ThreadStoreBase.read_subagent_trace`."""


def _abstract_names(cls: type) -> tuple:
    """Абстрактные методы класса В ПОРЯДКЕ ОБЪЯВЛЕНИЯ.

    Не ``__abstractmethods__``: это frozenset, и порядок в нём случайный — а
    имена попадают в сообщения об ошибках, где скачущий порядок мешает
    сравнивать два запуска глазами. ``__dict__`` порядок определения хранит.
    """
    return tuple(
        name
        for name, attr in cls.__dict__.items()
        if getattr(attr, "__isabstractmethod__", False)
    )


#: Исполняемый контракт хранилища: по этим именам ``athread_store`` решает,
#: годится ли переданный объект.
#:
#: ВЫВОДИТСЯ из самого ABC, а не пишется рядом. Раньше те же одиннадцать имён
#: были записаны трижды — в ``Protocol``, в ABC и в отдельном кортеже, — и
#: ничто не сторожило их согласие: метод, добавленный в ABC и забытый в
#: кортеже, ядро начало бы звать у хранилищ, которые о нём не знают, а
#: диспетчер пропустил бы их, сверяясь со старым списком.
#:
#: Проверка ОСТАЁТСЯ структурной: диспетчер смотрит на имена, а не на
#: наследование. Хранилище, которое наследовать не может (прокси на
#: ``__getattr__``, объект из чужого DI-контейнера), принимается как прежде —
#: см. ``test_isinstance_is_not_a_gate``.
CONTRACT_METHODS = _abstract_names(ThreadStoreBase)

#: Те же операции с приставкой ``a``. Тоже выводятся, а не дублируются.
ASYNC_CONTRACT_METHODS = _abstract_names(AsyncThreadStoreBase)


__all__ = [
    "ASYNC_CONTRACT_METHODS",
    "CONTRACT_METHODS",
    "AsyncThreadStoreBase",
    "ThreadStoreBase",
]
