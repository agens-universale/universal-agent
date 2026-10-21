"""Фасад ``Threads`` — то, через что фронтенды читают и пишут треды.

Фронтенды (TUI, CLI, сервер, gc-chat, gc-acp) не ходят ни в файлы тредов,
ни в контракт хранилища напрямую: всё производное — постраничный обход,
курсор хвоста, слияние состояния фронтенда, аренда для записи истории —
здесь, в одной реализации поверх любого ``ThreadStore``.

Синхронным вызывающим (опрос TUI, рабочие потоки) — :func:`run_sync`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from gc_core.plan_store import normalize_plan_block
from gc_core.threads.history import messages_and_keys
from gc_core.threads.instance import session_owner
from gc_core.threads.meta_map import meta_from_record
from gc_core.threads.models import (
    CONTROL,
    INBOX,
    JsonObject,
    Receipt,
    SideChange,
    StreamEntry,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
)
from gc_core.threads.session_writer import SessionWriter
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
    истории (по умолчанию — своя сессия, ``session_owner``). Аренда берётся
    на запись и сразу отпускается: следующий ход не ждёт её срока, а тред,
    занятый ходом, даёт ``ThreadBusy``."""

    def __init__(self, store: ThreadStore, *, owner: str | None = None):
        self.store = store
        self._owner = owner or session_owner()

    async def _owned(self, key: ThreadKey, work):
        """Запись под своей арендой: взять тред, ``work(writer)``, отпустить.
        Тред, занятый ходом другого владельца, — ``ThreadBusy``."""
        writer = SessionWriter(self.store, key, owner=self._owner)
        await writer.aclaim()
        try:
            return await work(writer)
        finally:
            await writer.arelease()

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

    async def busy_owner(self, key: ThreadKey) -> str | None:
        """Кто ведёт тред сейчас (живая аренда) или ``None`` — тред свободен."""
        from gc_core.execution.environment import utc_now
        from gc_core.threads.rules import lease_expired

        record = await self.store.get_thread(key)
        lease = record.lease if record is not None else None
        if lease is None or lease_expired(lease, utc_now()):
            return None
        return lease.owner

    async def resolve(self, alias: str) -> ThreadKey | None:
        return await self.store.resolve_alias(alias)

    async def snapshot(
        self, key: ThreadKey, streams: Sequence[str]
    ) -> TailBatch | None:
        """Все записи потоков и курсор сразу за ними — начало показа треда.
        Треда нет — ``None``."""
        record = await self.store.get_thread(key)
        if record is None:
            return None
        start = Cursor(record.generation, record.history_version)
        return await self.tail(key, streams, start)

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

    async def submit(
        self,
        key: ThreadKey,
        message: JsonObject,
        *,
        op_id: str | None = None,
        source: str = "api",
    ) -> Receipt:
        """Сообщение пользователя во входящий поток треда — без аренды:
        идущий ход заберёт его опросом, следующий — в начале. ``id`` нет —
        выдаётся здесь, чтобы повторное чтение потока давало то же сообщение.
        ``max_seq["inbox"]`` квитанции — номер сообщения в потоке."""
        entry = {**message, "source": source}
        entry.setdefault("role", "user")
        if not entry.get("id"):
            entry["id"] = f"m_{uuid.uuid4().hex}"
        return await self.store.append_side(
            key,
            SideChange(
                op_id=op_id or _op_id(INBOX), append=[(INBOX, [entry])]
            ),
        )

    async def request_cancel(
        self, key: ThreadKey, *, op_id: str | None = None
    ) -> Receipt:
        """Отменить идущий ход треда с любого процесса: запись в поток
        ``control``. Ход увидит её опросом и остановится на следующем шаге;
        следующих ходов она не касается."""
        from gc_core.execution.environment import utc_now

        entry = {"type": "cancel", "at": utc_now().isoformat()}
        return await self.store.append_side(
            key,
            SideChange(
                op_id=op_id or _op_id(CONTROL), append=[(CONTROL, [entry])]
            ),
        )

    async def append_messages(self, key: ThreadKey, messages) -> int:
        """Дописать сообщения в историю (аренда и повторы — как у сессии);
        уже сохранённые не повторяются. Возвращает, сколько записано."""
        from gc_core.execution.message_keys import make_message_key

        async def work(writer: SessionWriter) -> int:
            entries = await self.store.read_stream(key, _HISTORY)
            _saved, known = messages_and_keys(self.store, key, entries)
            fresh, seen = [], set(known)
            for message in messages:
                message_key = make_message_key(message)
                if message_key not in seen:
                    fresh.append(message)
                    seen.add(message_key)
            if fresh:
                await writer.acommit(append_messages=fresh)
            return len(fresh)

        return await self._owned(key, work)

    async def discard_message(
        self,
        key: ThreadKey,
        message_id: str,
        *,
        recount: Callable[[list[JsonObject]], JsonObject] | None = None,
    ) -> bool:
        """Удалить сообщение. ``recount(история) -> {"live_…": …}`` —
        пересчитать объём живой истории в ``runtime`` (остальное в нём —
        как было)."""

        async def work(writer: SessionWriter) -> bool:
            entries = await self.store.read_stream(key, _HISTORY)
            if not any(e.data.get("id") == message_id for e in entries):
                return False
            await writer.acommit(discard_message_ids=[message_id])
            return True

        removed = await self._owned(key, work)
        record = await self.store.get_thread(key) if removed else None
        if record is not None and recount is not None:
            messages = [entry.data for entry in await self.history(key)]
            runtime = record.runtime.model_copy(update=recount(messages))
            await self.store.append_side(
                key, SideChange(op_id=_op_id("runtime"), set_runtime=runtime)
            )
        return removed

    async def clear_plan(self, key: ThreadKey) -> bool:
        """Убрать план сессии по решению человека (``/plan clear``); True —
        план был.

        Единственная запись фронтенда в ``shared`` — осознанное исключение
        из правила «shared пишет исполнитель»: команда приходит, когда
        прогона нет. Запись — под арендой треда, как запись истории: тред,
        занятый ходом другого владельца, даёт ``OwnershipError``, а не гонку.
        Вместе с планом взводится признак для модели — следующий прогон
        один раз сообщит ей, что плана больше нет; файл плана для человека
        (возможность файлового хранилища) удаляется.
        """

        async def has_plan() -> bool:
            record = await self.store.get_thread(key)
            return record is not None and bool(
                normalize_plan_block(record.state.plan)
            )

        # Плана нет — ответ без аренды (тред может вести чужой ход).
        if not await has_plan():
            return False

        async def work(writer: SessionWriter) -> bool:
            record = await self.store.get_thread(key)
            if record is None or not normalize_plan_block(record.state.plan):
                return False
            state = record.state.model_copy(
                update={"plan": None, "plan_removed_notice": True}
            )
            state.__pydantic_fields_set__.discard("plan")
            await writer.acommit(set_state=state)
            return True

        if not await self._owned(key, work):
            return False
        drop = getattr(self.store, "delete_plan_file", None)
        if callable(drop):
            await drop(key)
        return True

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
