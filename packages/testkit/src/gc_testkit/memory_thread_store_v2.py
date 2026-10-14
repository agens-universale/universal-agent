"""Хранилище тредов v2 в памяти — образец реализации контракта и стенд тестов.

Самая короткая честная реализация ``gc_core.threads.store.ThreadStore``: всё,
что контракт обещает (квитанции, ревизия, аренда с фенсингом, перенумерация
истории, алиасы, архивы), здесь сделано явно и без оптимизаций. Автору своего
хранилища удобно читать её рядом с конформанс-набором.

Внутри методов нет ``await`` между проверкой и записью, поэтому каждая
операция атомарна на loop без блокировок.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from gc_core.threads.errors import (
    OperationMismatch,
    OwnershipError,
    ThreadConflict,
)
from gc_core.threads.models import (
    ArchiveInfo,
    BlobRef,
    JsonObject,
    Lease,
    LeaseRequest,
    Page,
    Receipt,
    RuntimeStatus,
    SessionState,
    SideChange,
    StreamEntry,
    ThreadChange,
    ThreadInfo,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
    Usage,
    change_digest,
    check_stream_name,
)
from gc_core.threads.store import ThreadStore

_HISTORY = "messages"
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"}


@dataclass
class _Thread:
    created_at: str
    generation: int = 0
    info: ThreadInfo = field(default_factory=ThreadInfo)
    state: SessionState = field(default_factory=SessionState)
    usage: Usage = field(default_factory=Usage)
    runtime: RuntimeStatus = field(default_factory=RuntimeStatus)
    frontend: dict[str, JsonObject] = field(default_factory=dict)
    streams: dict[str, list[StreamEntry]] = field(default_factory=dict)
    archives: list[ArchiveInfo] = field(default_factory=list)
    archived: dict[str, list[StreamEntry]] = field(default_factory=dict)
    revision: int = 0
    history_version: int = 0
    lease: Lease | None = None
    epoch: int = 0
    updated_at: str = ""
    #: Последний выданный seq каждого потока: удалённый хвост не освобождает
    #: номер, иначе читатель с курсора пропустил бы следующую запись.
    high: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Stored:
    digest: str
    receipt: Receipt
    at: datetime


def _instant(stamp: str | None) -> datetime:
    """Момент времени из ISO-8601 (``Z`` или смещение); пусто — самое раннее."""
    if not stamp:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(stamp)


def _after_cursor(record: ThreadRecord, cursor: str) -> bool:
    """Идёт ли запись после курсора в порядке (updated_at ↓, system, thread)."""
    updated, system, thread = cursor.split("|", 2)
    mine, theirs = _instant(record.info.updated_at), _instant(updated)
    if mine != theirs:
        return mine < theirs
    return (record.key.system, record.key.thread) > (system, thread)


class MemoryThreadStore(ThreadStore):
    """``clock`` — часы хранилища: по ним истекает аренда и стареют квитанции."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._threads: dict[ThreadKey, _Thread] = {}
        self._receipts: dict[tuple[ThreadKey, str], _Stored] = {}
        self._aliases: dict[str, ThreadKey] = {}
        self._blobs: dict[tuple[ThreadKey, str], bytes] = {}
        #: Удалённые треды: (generation, epoch) — фенсинг переживает delete.
        self._tombstones: dict[ThreadKey, tuple[int, int]] = {}

    # --- внутреннее ----------------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    def _expired(self, lease: Lease) -> bool:
        return (
            lease.expires_at is not None and lease.expires_at <= self._clock()
        )

    def _thread(self, key: ThreadKey) -> _Thread:
        thread = self._threads.get(key)
        if thread is None:
            now = self._now()
            generation, epoch = self._tombstones.get(key, (0, 0))
            thread = _Thread(
                created_at=now,
                updated_at=now,
                generation=generation,
                epoch=epoch,
            )
            self._threads[key] = thread
        return thread

    def _record(self, key: ThreadKey, thread: _Thread) -> ThreadRecord:
        history = thread.streams.get(_HISTORY, [])
        info = thread.info.model_copy(
            update={
                "created_at": thread.created_at,
                "updated_at": thread.updated_at,
                "messages_count": len(history),
            }
        )
        return ThreadRecord(
            key=key,
            info=info,
            state=thread.state.model_copy(deep=True),
            usage=thread.usage.model_copy(deep=True),
            runtime=thread.runtime.model_copy(deep=True),
            frontend=copy.deepcopy(thread.frontend),
            archives=list(thread.archives),
            revision=thread.revision,
            generation=thread.generation,
            history_version=thread.history_version,
            max_seq={n: seq for n, seq in thread.high.items() if seq},
            lease=thread.lease,
        )

    def _receipt(self, key: ThreadKey, thread: _Thread, op_id: str, **extra):
        return Receipt(
            op_id=op_id,
            revision=thread.revision,
            generation=thread.generation,
            history_version=thread.history_version,
            max_seq=self._record(key, thread).max_seq,
            **extra,
        )

    def _replayed(self, key: ThreadKey, change) -> Receipt | None:
        stored = self._receipts.get((key, change.op_id))
        if stored is None:
            return None
        if stored.digest != change_digest(change):
            raise OperationMismatch(
                f"op_id {change.op_id!r} уже применён с другим содержимым"
            )
        return stored.receipt

    def _remember(self, key: ThreadKey, change, receipt: Receipt) -> Receipt:
        self._receipts[(key, change.op_id)] = _Stored(
            change_digest(change), receipt, self._clock()
        )
        return receipt

    def _check_lease(self, thread: _Thread | None, lease: Lease) -> None:
        current = thread.lease if thread is not None else None
        if current is None:
            raise OwnershipError("запись без claim")
        if (current.owner, current.epoch) != (lease.owner, lease.epoch):
            raise OwnershipError("аренда сменилась: запись по чужой эпохе")
        if self._expired(current):
            raise OwnershipError("аренда истекла")

    def _append(self, thread: _Thread, stream: str, items) -> None:
        entries = thread.streams.setdefault(stream, [])
        seq, now = thread.high.get(stream, 0), self._now()
        for item in items:
            seq += 1
            entries.append(
                StreamEntry(seq=seq, ts=now, data=copy.deepcopy(item))
            )
        thread.high[stream] = seq

    def _renumber(self, thread: _Thread, items) -> None:
        thread.streams[_HISTORY] = []
        thread.high[_HISTORY] = 0
        self._append(thread, _HISTORY, items)
        thread.history_version += 1

    def _archive(self, thread: _Thread) -> str:
        archive_id = f"a{len(thread.archives) + 1}"
        entries = list(thread.streams.get(_HISTORY, []))
        thread.archived[archive_id] = entries
        thread.archives.append(
            ArchiveInfo(
                id=archive_id,
                created_at=self._now(),
                messages_count=len(entries),
            )
        )
        return archive_id

    def _replace_history(self, thread: _Thread, change: ThreadChange):
        compaction = change.replace_history
        if compaction is None:
            return None
        archive_id = self._archive(thread) if compaction.archive else None
        self._renumber(thread, compaction.messages)
        return archive_id

    def _reset_history(self, thread: _Thread) -> None:
        self._renumber(thread, [])
        thread.generation += 1
        thread.lease = None

    def _discard(self, thread: _Thread, ids: list[str]) -> None:
        if ids:
            thread.streams[_HISTORY] = [
                e
                for e in thread.streams.get(_HISTORY, [])
                if e.data.get("id") not in ids
            ]

    def _apply_sets(self, thread: _Thread, change: ThreadChange) -> None:
        if change.set_state is not None:
            thread.state = change.set_state.model_copy(deep=True)
        if change.set_usage is not None:
            thread.usage = change.set_usage.model_copy(deep=True)
        if change.set_info is not None:
            thread.info = change.set_info.model_copy(deep=True)

    def _apply(self, key: ThreadKey, thread: _Thread, change: ThreadChange):
        if change.reset_history:
            self._reset_history(thread)
        archive_id = self._replace_history(thread, change)
        self._discard(thread, change.discard_message_ids)
        self._append(thread, _HISTORY, change.append_messages)
        self._apply_sets(thread, change)
        self._alias(key, change.add_aliases)
        return archive_id

    def _alias(self, key: ThreadKey, aliases: list[str]) -> None:
        for alias in aliases:
            self._aliases[alias] = key

    def _check_aliases(self, key: ThreadKey, aliases: list[str]) -> None:
        for alias in aliases:
            owner = self._aliases.get(alias)
            if owner is not None and owner != key:
                raise ValueError(
                    f"алиас {alias!r} принадлежит треду "
                    f"{owner.system}/{owner.thread}"
                )

    # --- чтение --------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        thread = self._threads.get(key)
        return None if thread is None else self._record(key, thread)

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        records = [self._record(k, t) for k, t in self._threads.items()]
        records = [r for r in records if self._matches(r, query)]
        records.sort(key=lambda r: (r.key.system, r.key.thread))
        records.sort(key=lambda r: _instant(r.info.updated_at), reverse=True)
        if query.cursor:
            records = [r for r in records if _after_cursor(r, query.cursor)]
        page = records[: query.limit]
        cursor = None
        if len(records) > query.limit:
            last = page[-1]
            cursor = (
                f"{last.info.updated_at}|{last.key.system}|{last.key.thread}"
            )
        return Page[ThreadRecord](items=page, next_cursor=cursor)

    @staticmethod
    def _matches(record: ThreadRecord, query: ThreadQuery) -> bool:
        updated = _instant(record.info.updated_at)
        after, before = query.updated_after, query.updated_before
        checks = (
            query.system is None or record.key.system == query.system,
            query.workspace is None
            or record.info.workspace == query.workspace,
            after is None or updated > _instant(after),
            before is None or updated < _instant(before),
        )
        return all(checks)

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        thread = self._threads.get(key)
        entries = thread.streams.get(stream, []) if thread else []
        fresh = [e.model_copy(deep=True) for e in entries if e.seq > after_seq]
        return fresh if limit is None else fresh[:limit]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        thread = self._threads.get(key)
        entries = thread.archived.get(archive_id, []) if thread else []
        return [e.model_copy(deep=True) for e in entries]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        return self._aliases.get(alias)

    # --- запись --------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        replayed = self._replayed(key, change)
        if replayed is not None:
            return replayed
        thread = self._threads.get(key)
        self._check_lease(thread, change.lease)
        if change.expected_revision != thread.revision:
            raise ThreadConflict(
                f"ревизия {thread.revision}, ожидалась {change.expected_revision}"
            )
        self._check_aliases(key, change.add_aliases)
        archive_id = self._apply(key, thread, change)
        thread.revision += 1
        thread.updated_at = self._now()
        receipt = self._receipt(
            key, thread, change.op_id, archive_id=archive_id
        )
        return self._remember(key, change, receipt)

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        replayed = self._replayed(key, change)
        if replayed is not None:
            return replayed
        for stream, _items in change.append:
            if check_stream_name(stream) == _HISTORY:
                raise ValueError("историю треда пишет только commit")
        self._check_aliases(key, change.add_aliases)
        thread = self._thread(key)
        if change.set_runtime is not None:
            thread.runtime = change.set_runtime
        if change.set_frontend is not None:
            name, state = change.set_frontend
            thread.frontend[name] = copy.deepcopy(state)
        for stream, items in change.append:
            self._append(thread, stream, items)
        self._alias(key, change.add_aliases)
        receipt = self._receipt(key, thread, change.op_id)
        return self._remember(key, change, receipt)

    # --- аренда и жизненный цикл -----------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        thread = self._thread(key)
        current = thread.lease
        expires = None if lease.ttl is None else self._clock() + lease.ttl
        if current is not None and not self._expired(current):
            if current.owner != lease.owner:
                raise OwnershipError(f"тред ведёт {current.owner!r}")
            epoch = current.epoch
        else:
            thread.epoch += 1
            epoch = thread.epoch
        thread.lease = Lease(
            owner=lease.owner, epoch=epoch, expires_at=expires
        )
        return self._record(key, thread)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        thread = self._threads.get(key)
        current = thread.lease if thread else None
        if current and (current.owner, current.epoch) == (
            lease.owner,
            lease.epoch,
        ):
            thread.lease = None

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        thread = self._threads.pop(key, None)
        if thread is None:
            return
        self._tombstones[key] = (thread.generation + 1, thread.epoch)
        self._aliases = {a: k for a, k in self._aliases.items() if k != key}
        self._blobs = {b: d for b, d in self._blobs.items() if b[0] != key}
        self._receipts = {
            r: s for r, s in self._receipts.items() if r[0] != key
        }

    # --- вложения и обслуживание ---------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = digest + _EXTENSIONS.get(mime, ".bin")
        self._blobs[(key, name)] = data
        location = f"thread-file://{key.system}/{key.thread}/{name}"
        return BlobRef(key=key, name=name, mime=mime, location=location)

    async def get_blob(self, ref: BlobRef) -> bytes:
        try:
            return self._blobs[(ref.key, ref.name)]
        except KeyError:
            raise FileNotFoundError(ref.location) from None

    async def prune_receipts(self, older_than: timedelta) -> int:
        cutoff = self._clock() - older_than
        stale = [r for r, s in self._receipts.items() if s.at < cutoff]
        for receipt in stale:
            del self._receipts[receipt]
        return len(stale)
