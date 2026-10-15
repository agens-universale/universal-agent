"""Контракт хранилища тредов v2 — то, что реализует автор хранилища.

Ядро и фронтенды сюда не ходят: они работают через фасад ``Threads`` /
``Thread``, а хранилище видят только фабрика и параметры SDK. Всё
производное — правила слияния состояния, повторы при конфликте, продление
аренды — живёт в фасаде, поэтому методов здесь немного и каждый — одна
операция хранилища.

**Правило записи.** Всё, что агент или фронтенд пишет о себе, идёт через
хранилища (тред — сюда, ресурсы агента и память проекта — в ``AgentStore``).
Мимо них пишутся только правки workspace тулами (через ``os_backend``) и
поимённый список исключений: конфиги, логи, состояние установки.

**Порядок проверок ``commit`` — в одной транзакции:**

1. Есть квитанция этого ``op_id``: то же содержимое (``change_digest``) —
   вернуть её и ничего не писать; другое — ``OperationMismatch``.
2. ``lease`` не совпадает с текущей арендой (владелец и эпоха) или истекла по
   часам хранилища — ``OwnershipError``. ``commit`` без ``claim`` — тоже.
3. ``expected_revision`` не равна текущей ревизии — ``ThreadConflict``.
4. Применить изменения, увеличить ``revision``, сохранить и вернуть квитанцию.
   Квитанция ``commit`` несёт аренду после записи (``reset_history`` её
   снимает — ``None``): по ней пишущий знает, что аренда при нём, и не
   берёт её заново перед следующей записью.

``append_side`` проверяет только ``op_id`` и ревизию не меняет: статус хода,
состояние фронтендов и журналы рядом с историей не конфликтуют с ходом.
Исключение — тред durable-исполнителя (бессрочная аренда): запись хода
(статус, потоки, алиасы) проходит только с его арендой в
``SideChange.lease`` (``rules.check_side_writer``), иначе ``OwnershipError``.
Для нового ключа он создаёт тред без аренды (сообщение во ``inbox`` может
прийти раньше, чем начался ход).

**Аренда.** ``claim`` проходит, если аренды нет, она того же владельца и жива
(продление, эпоха та же) или истекла (эпоха +1 — даже для того же владельца:
для фенсинга это смена). Истечение считают часы хранилища. ``release`` чужой
или уже сменённой аренды ничего не делает. Последняя выданная эпоха переживает
``release``: следующий ``claim`` выдаёт следующую.

**Применение ``commit``** — в порядке: ``reset_history``, ``replace_history``,
``discard_message_ids``, ``append_messages``, ``set_*``, ``add_aliases``.
``set_state`` / ``set_usage`` / ``set_info`` и ``set_runtime`` **заменяют**
значение целиком (слияние — забота фасада); ``set_frontend`` заменяет
состояние одного фронтенда. ``discard_message_ids`` сверяется с
``data["id"]`` записей истории.

``reset_history`` очищает только поток ``messages`` (архивы, остальные потоки,
состояние не трогает), увеличивает ``generation`` и ``history_version`` и
**снимает аренду**: писать дальше можно после нового ``claim``.

**Время.** ``created_at`` ставится при создании треда, ``updated_at`` — при
каждом ``commit``; ``append_side``, ``claim`` и ``release`` его не двигают
(иначе список тредов прыгал бы от каждого тика статуса). Формат — ISO-8601 со
смещением (``2026-10-14T12:00:00.000000+00:00``); границы ``ThreadQuery``
сравниваются как моменты времени, а не как строки.

**Идентификаторы.**

* ``op_id`` уникален в пределах треда и общий у ``commit`` и
  ``append_side``: тот же ``op_id`` с другим содержимым — ``OperationMismatch``.
  Квитанции удаляются вместе с тредом; повтор ``delete`` ничего не делает.
* Алиас глобален: алиас, принадлежащий другому треду, — ``ValueError`` до
  любой записи.
* Эпоха аренды переживает ``delete``: тред, созданный заново под тем же
  ключом, выдаёт эпоху больше прежней, иначе запись по аренде удалённого
  треда прошла бы фенсинг.

**Отказы.** Нет треда — ``None`` или ``[]``, а не исключение. Недоступность
самого хранилища — исключения драйвера как есть.

**Потоки.** ``seq`` строго растёт и **не переиспользуется**: удалённый хвост
не освобождает номер, иначе читатель с курсором пропустил бы следующую
запись. Единственное исключение — ``replace_history`` и ``reset_history``:
они нумеруют историю заново с 1 и увеличивают ``history_version``, по нему
читатель с курсором узнаёт о замене. Данные изолированы: изменение словаря
после записи или после чтения не меняет хранимое.

Методы асинхронные и без приставки ``a``: это публичная поверхность для чужих
авторов, и она читается как обычная асинхронная библиотека (исключение из
правила ``docs/async.md`` записано там же).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import timedelta

from gc_core.threads.models import (
    BlobRef,
    Lease,
    LeaseRequest,
    Page,
    Receipt,
    SideChange,
    StreamEntry,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
)


class ThreadStore(ABC):
    """Хранилище тредов. Реализация — одна на базу; проверка —
    ``gc_testkit.thread_store_conformance_v2.assert_conforms``."""

    # --- чтение -----------------------------------------------------------

    @abstractmethod
    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        """Тред без содержимого потоков; нет треда — ``None``."""

    @abstractmethod
    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        """Треды по фильтру, ``updated_at`` по убыванию, страницами."""

    @abstractmethod
    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        """Записи потока с ``seq > after_seq`` в порядке записи."""

    @abstractmethod
    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        """История, ушедшая в архив при ``replace_history``."""

    @abstractmethod
    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        """Тред по внешнему идентификатору (например ``response_id``)."""

    # --- запись -----------------------------------------------------------

    @abstractmethod
    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        """История и состояние — атомарно; порядок проверок — в докстринге модуля."""

    @abstractmethod
    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        """Статус, состояние фронтенда, журналы — без ревизии треда."""

    # --- аренда и жизненный цикл -------------------------------------------

    @abstractmethod
    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        """Взять или продлить аренду; нового треда — создать запись."""

    @abstractmethod
    async def release(self, key: ThreadKey, lease: Lease) -> None:
        """Отдать свою аренду; чужую или сменённую — не трогать."""

    @abstractmethod
    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        """Удалить тред целиком: потоки, архивы, блобы, алиасы, квитанции."""

    # --- вложения и обслуживание ---------------------------------------------

    @abstractmethod
    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        """Сохранить вложение треда; ссылка для сообщения — ``ref.location``."""

    @abstractmethod
    async def get_blob(self, ref: BlobRef) -> bytes:
        """Байты вложения; нет — ``FileNotFoundError``."""

    @abstractmethod
    async def prune_receipts(self, older_than: timedelta) -> int:
        """Удалить квитанции старше порога; вернуть, сколько удалено."""

    # --- необязательное: push ------------------------------------------------

    async def watch(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int,
        generation: int | None = None,
        history_version: int | None = None,
        poll_interval: float = 0.5,
    ) -> AsyncIterator[StreamEntry]:
        """Новые записи потока по мере появления; остановка — отменой задачи.

        Курсор читателя — ``(generation, history_version, after_seq)``: если
        тред удалён и создан заново (другое поколение) или история заменена
        (другая версия), выдача начинается с начала новой истории.
        ``None`` — значение на момент старта.

        По умолчанию — опрос. Хранилище с push (LISTEN/NOTIFY, Redis streams)
        переопределяет.
        """
        cursor, mark = after_seq, None
        if generation is not None or history_version is not None:
            mark = (generation, history_version)
        while True:
            record = await self.get_thread(key)
            current = (
                (record.generation, record.history_version)
                if record is not None
                else None
            )
            if (
                mark is not None
                and current != mark
                and _changed(mark, current)
            ):
                cursor = 0
            mark = current
            for entry in await self.read_stream(key, stream, after_seq=cursor):
                cursor = entry.seq
                yield entry
            await asyncio.sleep(poll_interval)


def _changed(mark: tuple, current: tuple | None) -> bool:
    """Сменилось ли поколение или версия истории (``None`` в курсоре — «не
    задано вызывающим», не сравнивается)."""
    if current is None:
        return True
    return any(
        wanted is not None and wanted != now
        for wanted, now in zip(mark, current, strict=True)
    )


#: Абстрактные методы контракта — то, что обязана реализовать база.
CONTRACT_METHODS: tuple[str, ...] = tuple(
    sorted(ThreadStore.__abstractmethods__)
)
