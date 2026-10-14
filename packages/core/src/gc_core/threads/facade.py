"""Фасад ``Threads`` — то, через что фронтенды читают и пишут треды.

Фронтенды (TUI, CLI, сервер, gc-chat, gc-acp) не ходят ни в файлы тредов,
ни в контракт хранилища напрямую: всё производное — постраничный обход,
курсор хвоста, слияние состояния фронтенда, аренда для записи истории —
здесь, в одной реализации поверх любого ``ThreadStore``.

Синхронным вызывающим (опрос TUI, рабочие потоки) — :func:`run_sync`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from gc_core.threads.meta_map import meta_from_record
from gc_core.threads.models import (
    JsonObject,
    Receipt,
    RuntimeStatus,
    SideChange,
    StreamEntry,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
)
from gc_core.threads.session_port import SessionPort
from gc_core.threads.store import ThreadStore

_HISTORY = "messages"
_PAGE = 1000


@dataclass(frozen=True)
class Cursor:
    """Позиция читателя хвоста: поколение треда, версия истории и последний
    прочитанный seq каждого потока."""

    generation: int
    history_version: int
    seq: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TailBatch:
    """Новые записи с прошлого чтения. ``reset`` — история заменена или тред
    создан заново: читатель начинает свой показ с начала."""

    entries: list[tuple[str, StreamEntry]]
    cursor: Cursor
    reset: bool = False


def _op_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Threads:
    """Треды одного хранилища. ``owner`` — владелец аренды для записи
    истории (по умолчанию — имя машины, как у сессии агента)."""

    def __init__(self, store: ThreadStore, *, owner: str | None = None):
        self.store = store
        self._port = SessionPort(store, owner=owner)

    @classmethod
    def from_settings(cls, settings: Any) -> Threads:
        """Файловое хранилище по каталогу тредов из настроек."""
        from gc_core.thread_paths import resolve_threads_dir
        from gc_core.threads.file_store import FileThreadStore

        return cls(FileThreadStore(resolve_threads_dir(settings)))

    # --- чтение -----------------------------------------------------------

    async def get(self, key: ThreadKey) -> ThreadRecord | None:
        return await self.store.get_thread(key)

    @staticmethod
    def legacy_meta(record: ThreadRecord) -> JsonObject:
        """Meta в форме develop — для вывода целиком (``/status``, ``/logs``)."""
        return meta_from_record(record)

    async def iter_threads(
        self,
        *,
        system: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> AsyncIterator[ThreadRecord]:
        """Все треды по фильтру, ``updated_at`` по убыванию, постранично."""
        cursor = None
        while True:
            page = await self.store.list_threads(
                ThreadQuery(
                    system=system,
                    updated_before=updated_before,
                    updated_after=updated_after,
                    cursor=cursor,
                    limit=_PAGE,
                )
            )
            for record in page.items:
                yield record
            cursor = page.next_cursor
            if cursor is None:
                return

    async def history(self, key: ThreadKey) -> list[StreamEntry]:
        return await self.store.read_stream(key, _HISTORY)

    async def parts(self, key: ThreadKey) -> list[list[StreamEntry]]:
        """История по частям между сжатиями: архивы по порядку, затем живая."""
        record = await self.store.get_thread(key)
        if record is None:
            return []
        parts = [
            await self.store.read_archive(key, archive.id)
            for archive in record.archives
        ]
        parts.append(await self.store.read_stream(key, _HISTORY))
        return parts

    async def subagent_events(self, key: ThreadKey) -> list[StreamEntry]:
        return await self.store.read_stream(key, "subagent_events")

    async def edits(
        self, key: ThreadKey, *, after_seq: int = 0
    ) -> list[StreamEntry]:
        return await self.store.read_stream(key, "edits", after_seq=after_seq)

    async def read_frontend(
        self, key: ThreadKey, stream: str
    ) -> list[StreamEntry]:
        """Поток фронтенда ``<фронтенд>.<имя>`` целиком."""
        return await self.store.read_stream(key, stream)

    async def resolve(self, alias: str) -> ThreadKey | None:
        return await self.store.resolve_alias(alias)

    async def tail(
        self,
        key: ThreadKey,
        streams: Sequence[str],
        cursor: Cursor | None,
    ) -> TailBatch:
        """Новые записи потоков с прошлого курсора; ``cursor=None`` — «с этого
        момента» (записи не отдаются, только курсор)."""
        record = await self.store.get_thread(key)
        if record is None:
            empty = Cursor(generation=-1, history_version=-1)
            return TailBatch([], empty, reset=cursor is not None)
        if cursor is None:
            now = {s: record.max_seq.get(s, 0) for s in streams}
            return TailBatch(
                [],
                Cursor(record.generation, record.history_version, now),
            )
        new_generation = cursor.generation != record.generation
        new_history = cursor.history_version != record.history_version
        entries: list[tuple[str, StreamEntry]] = []
        seq: dict[str, int] = {}
        for stream in streams:
            restart = new_generation or (new_history and stream == _HISTORY)
            after = 0 if restart else cursor.seq.get(stream, 0)
            fresh = await self.store.read_stream(key, stream, after_seq=after)
            entries.extend((stream, entry) for entry in fresh)
            seq[stream] = fresh[-1].seq if fresh else after
        return TailBatch(
            entries,
            Cursor(record.generation, record.history_version, seq),
            reset=new_generation or new_history,
        )

    # --- запись -----------------------------------------------------------

    async def update_frontend(
        self, key: ThreadKey, name: str, patch: JsonObject
    ) -> Receipt:
        """Слить ключи в состояние фронтенда (атомарно в хранилище)."""
        return await self.store.append_side(
            key,
            SideChange(op_id=_op_id(name), patch_frontend=(name, patch)),
        )

    async def append_frontend(
        self,
        key: ThreadKey,
        stream: str,
        items: list[JsonObject],
        *,
        aliases: Sequence[str] = (),
        patch: tuple[str, JsonObject] | None = None,
    ) -> Receipt:
        """Дописать в поток фронтенда; алиасы треда и слияние ``patch`` в
        состояние фронтенда — той же записью."""
        return await self.store.append_side(
            key,
            SideChange(
                op_id=_op_id("stream"),
                append=[(stream, items)],
                add_aliases=list(aliases),
                patch_frontend=patch,
            ),
        )

    async def append_messages(self, key: ThreadKey, messages) -> int:
        """Дописать сообщения в историю (аренда и повторы — как у сессии);
        уже сохранённые не повторяются. Возвращает, сколько записано."""
        return await self._port.aappend_messages(
            key.system, key.thread, list(messages)
        )

    async def discard_message(
        self,
        key: ThreadKey,
        message_id: str,
        *,
        runtime: RuntimeStatus | None = None,
    ) -> bool:
        """Удалить сообщение; ``runtime`` — пересчитанный объём истории."""
        removed = await self._port.adiscard_message(
            key.system, key.thread, message_id
        )
        if removed and runtime is not None:
            await self.store.append_side(
                key, SideChange(op_id=_op_id("runtime"), set_runtime=runtime)
            )
        return removed

    async def delete(self, key: ThreadKey) -> None:
        await self.store.delete(key, op_id=_op_id("delete"))

    async def prune(self, older_than: timedelta) -> int:
        return await self.store.prune_receipts(older_than)


def run_sync(awaitable):
    """Выполнить корутину фасада синхронно — без event loop.

    Для синхронных вызывающих (опрос TUI, рабочие потоки) с локальным
    хранилищем: файловое и in-memory ничего внешнего не ждут, и корутина
    доходит до конца за один шаг. Хранилище, которое ждёт сеть, здесь
    недопустимо — громкий отказ, а не тихое зависание.
    """
    coroutine = awaitable.__await__()
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    coroutine.close()
    raise RuntimeError(
        "run_sync: хранилище ждёт внешнее (сеть, таймер) — синхронный мост "
        "только для локального хранилища; зовите фасад из event loop"
    )
