"""Порт ``ThreadSession`` поверх контракта хранилища тредов v2.

``ThreadSession`` и её вызывающие (исполнитель, координатор сводки, ручное
сжатие) работают со старым асинхронным портом: ``aload_meta``,
``aappend_messages``, ``asave_meta``, ``apatch_runtime_flag`` и так далее.
Этот порт переводит их в операции v2 — так поведение цикла агента остаётся
прежним, а пишет он в любое хранилище нового контракта. Этап 3 заменит
сессию фасадом и уберёт порт.

Аренда берётся лениво, на первой записи, и продлевается, когда до истечения
остаётся меньше половины срока; бессрочная (``ttl=None`` — владелец workflow
Temporal) берётся один раз. Ревизия перечитывается прямо перед каждой
записью: гонку двух писателей решает повтор, как и раньше.

Код порта исполняется и в workflow Temporal: идентификаторы операций и время
— из среды исполнения (``generate_msg_id_sha256``, ``utc_now``), там они
детерминированы, а повтор activity приходит с тем же ``op_id``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gc_core.execution.environment import utc_now
from gc_core.execution.message_keys import make_message_key
from gc_core.threads.errors import OwnershipError, ThreadConflict
from gc_core.threads.instance import instance_id
from gc_core.threads.meta_map import meta_from_record, parts_from_meta
from gc_core.threads.models import (
    Compaction,
    Lease,
    LeaseRequest,
    Receipt,
    SideChange,
    ThreadChange,
    ThreadKey,
)
from gc_core.threads.store import ThreadStore
from gc_core.utils.generate_message_id import generate_msg_id_sha256

_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
_RETRIES = 3
#: Срок аренды сессии: процесс, упавший посреди хода, держит тред не дольше.
LEASE_TTL = timedelta(seconds=60)


def _op_id() -> str:
    return generate_msg_id_sha256("session")


class SessionPort:
    """Старый асинхронный порт сессии; хранилище — ``ThreadStore`` v2."""

    def __init__(
        self,
        store: ThreadStore,
        *,
        owner: str | None = None,
        ttl: timedelta | None = LEASE_TTL,
    ) -> None:
        self._store = store
        self._owner = owner or instance_id()
        self._ttl = ttl
        self._leases: dict[ThreadKey, Lease] = {}
        #: Ревизия из последней квитанции или чтения: перечитывать тред
        #: перед каждой записью дорого, а чужую запись поймает конфликт.
        self._revisions: dict[ThreadKey, int] = {}

    @property
    def threads_dir(self) -> str:
        return getattr(self._store, "threads_dir", "") or ""

    # --- аренда и запись -------------------------------------------------------

    def adopt_lease(self, key: ThreadKey, lease: Lease) -> None:
        """Аренда, уже взятая вызывающим (workflow взял тред в начале
        сессии): первая запись не идёт за ней в хранилище."""
        self._leases[key] = lease

    async def _lease(self, key: ThreadKey) -> Lease:
        lease = self._leases.get(key)
        if lease is not None and (
            lease.expires_at is None
            or lease.expires_at - utc_now() > self._ttl / 2
        ):
            return lease
        record = await self._store.claim(
            key, LeaseRequest(owner=self._owner, ttl=self._ttl)
        )
        self._leases[key] = record.lease
        return record.lease

    async def _commit(self, key: ThreadKey, **fields: Any) -> Receipt:
        for attempt in range(_RETRIES):
            lease = await self._lease(key)
            change = ThreadChange(
                op_id=_op_id(),
                expected_revision=await self._revision(key),
                lease=lease,
                **fields,
            )
            try:
                receipt = await self._store.commit(key, change)
            except ThreadConflict:
                self._revisions.pop(key, None)
                continue
            except OwnershipError:
                # Аренду сняли (сброс истории) или она истекла — взять заново.
                self._leases.pop(key, None)
                self._revisions.pop(key, None)
                if attempt == _RETRIES - 1:
                    raise
                continue
            self._revisions[key] = receipt.revision
            if receipt.lease is None:
                self._leases.pop(key, None)
            return receipt
        raise ThreadConflict(
            f"тред {key.system}/{key.thread} менялся под записью"
        )

    async def _revision(self, key: ThreadKey) -> int:
        if key not in self._revisions:
            record = await self._store.get_thread(key)
            self._revisions[key] = record.revision if record else 0
        return self._revisions[key]

    async def _side(self, key: ThreadKey, **fields: Any) -> Receipt:
        return await self._store.append_side(
            key, SideChange(op_id=_op_id(), **fields)
        )

    # --- чтение ----------------------------------------------------------------

    async def aload_meta(self, system_id: str, thread_id: str) -> dict:
        key = _key(system_id, thread_id)
        record = await self._store.get_thread(key)
        if record is None:
            return {}
        self._revisions[key] = record.revision
        return meta_from_record(record)

    async def aload_thread_state(self, system_id: str, thread_id: str):
        from gc_core.thread_store_types import ThreadState

        key = _key(system_id, thread_id)
        entries = await self._store.read_stream(key, _HISTORY)
        history_keys = getattr(self._store, "history_keys", None)
        keys = history_keys(key) if callable(history_keys) else None
        if keys is not None:
            messages = [entry.data for entry in entries]
        else:
            # Хранилище ключей истории не ведёт (``None`` — так отвечает и
            # прокси Temporal поверх такого хранилища).
            messages, keys = _with_ids(key, [entry.data for entry in entries])
        record = await self._store.get_thread(key)
        max_seq = record.max_seq.get(_HISTORY, 0) if record else 0
        return ThreadState(messages, keys, max_seq)

    async def aload_messages(self, system_id: str, thread_id: str) -> list:
        return (await self.aload_thread_state(system_id, thread_id)).messages

    async def aread_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> list:
        entries = await self._store.read_stream(
            _key(system_id, thread_id), _SUBAGENTS
        )
        return [{"ts": e.ts, "seq": e.seq, **e.data} for e in entries]

    # --- история ---------------------------------------------------------------

    async def aappend_messages(
        self,
        system_id: str,
        thread_id: str,
        messages,
        *,
        known_keys: set | None = None,
        start_seq: int = 0,
    ) -> int:
        del start_seq  # номера выдаёт хранилище
        if known_keys is None:
            # Без ключей вызывающего — сверка с тем, что уже сохранено:
            # повтор батча не должен задвоить историю.
            state = await self.aload_thread_state(system_id, thread_id)
            known = set(state.known_keys)
        else:
            known = known_keys
        fresh, batch = [], set()
        for message in messages:
            key = make_message_key(message)
            if key in known or key in batch:
                continue
            fresh.append(message)
            batch.add(key)
        if fresh:
            await self._commit(
                _key(system_id, thread_id), append_messages=fresh
            )
        # Ключи пополняются на месте и только записанным: по их приросту
        # вызывающий понимает, что легло.
        known.update(batch)
        return len(fresh)

    async def adiscard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool:
        key = _key(system_id, thread_id)
        entries = await self._store.read_stream(key, _HISTORY)
        if not any(e.data.get("id") == message_id for e in entries):
            return False
        await self._commit(key, discard_message_ids=[message_id])
        return True

    async def areset(self, system_id: str, thread_id: str) -> list:
        key = _key(system_id, thread_id)
        await self._store.delete(key, op_id=_op_id())
        self._leases.pop(key, None)
        self._revisions.pop(key, None)
        return []

    async def aarchive_and_replace(
        self, system_id: str, thread_id: str, messages
    ) -> dict:
        from gc_core.threads import file_layout

        key = _key(system_id, thread_id)
        receipt = await self._commit(
            key, replace_history=Compaction(messages=list(messages))
        )
        await self._keep_tui_section(key)
        data = meta = None
        if self.threads_dir and receipt.archive_id:
            data, meta = file_layout.archive_paths(
                self.threads_dir, key.system, key.thread, receipt.archive_id
            )
        return {
            "archive_suffix": receipt.archive_id,
            "archive_data_path": data,
            "archive_meta_path": meta,
        }

    async def areplace_messages(
        self, system_id: str, thread_id: str, messages
    ) -> None:
        await self._commit(
            _key(system_id, thread_id),
            replace_history=Compaction(messages=list(messages), archive=False),
        )

    async def _keep_tui_section(self, key: ThreadKey) -> None:
        # Сжатие develop всегда пишет секцию ``tui`` — пустую, если её не было.
        record = await self._store.get_thread(key)
        if record is not None and "tui" not in record.frontend:
            await self._side(key, set_frontend=("tui", {}))

    # --- meta ------------------------------------------------------------------

    async def asave_meta(
        self, system_id: str, thread_id: str, meta: dict
    ) -> None:
        parts = parts_from_meta(meta)
        await self._commit(
            _key(system_id, thread_id),
            set_state=parts.state,
            set_usage=parts.usage,
            set_info=parts.info,
            set_runtime=parts.runtime,
            set_extra=parts.extra,
        )

    async def apersist_step(
        self, system_id: str, thread_id: str, messages, meta: dict
    ) -> None:
        """Шаг сессии — новые сообщения и meta одной записью."""
        parts = parts_from_meta(meta)
        await self._commit(
            _key(system_id, thread_id),
            append_messages=list(messages),
            set_state=parts.state,
            set_usage=parts.usage,
            set_info=parts.info,
            set_runtime=parts.runtime,
            set_extra=parts.extra,
        )

    async def _arewrite_runtime(self, system_id, thread_id, change) -> None:
        """Перечитать meta, применить правило develop, записать ``runtime``."""
        meta = await self.aload_meta(system_id, thread_id)
        change(meta)
        await self._side(
            _key(system_id, thread_id),
            set_runtime=parts_from_meta(meta).runtime,
        )

    async def apatch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: dict | None = None,
    ) -> None:
        from gc_core.thread_store import apply_runtime_flag_to_meta

        await self._arewrite_runtime(
            system_id,
            thread_id,
            lambda meta: apply_runtime_flag_to_meta(
                meta, flag, active=active, session_meta=session_meta
            ),
        )

    async def aupdate_runtime_meta(
        self, system_id: str, thread_id: str, patch: dict
    ) -> None:
        from gc_core.persistence.meta_ownership import merge_runtime_into_meta

        await self._arewrite_runtime(
            system_id,
            thread_id,
            lambda meta: merge_runtime_into_meta(meta, patch),
        )

    async def areset_run_flags(
        self,
        system_id: str,
        thread_id: str,
        *,
        stale_summarizing: bool = False,
        session_meta: dict | None = None,
    ) -> None:
        from gc_core.thread_store import reset_run_flags_in_meta

        await self._arewrite_runtime(
            system_id,
            thread_id,
            lambda meta: reset_run_flags_in_meta(
                meta,
                stale_summarizing=stale_summarizing,
                session_meta=session_meta,
            ),
        )

    # --- трасса субагентов ---------------------------------------------------------

    async def aappend_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: str,
        payload: dict,
    ) -> dict:
        key = _key(system_id, thread_id)
        data = {
            "parent_call_id": parent_call_id,
            "child_run_id": child_run_id,
            "kind": kind,
            "payload": payload,
        }
        receipt = await self._side(key, append=[(_SUBAGENTS, [data])])
        seq = receipt.max_seq.get(_SUBAGENTS, 1)
        written = await self._store.read_stream(
            key, _SUBAGENTS, after_seq=seq - 1, limit=1
        )
        entry = written[0]
        return {"ts": entry.ts, "seq": entry.seq, **entry.data}


def _with_ids(key: ThreadKey, messages: list) -> tuple[list, set]:
    """У каждого прочитанного сообщения есть ``id``: у записи без него он
    синтезируется из её содержимого — один и тот же на каждом чтении."""
    import hashlib
    import json

    keys, out = set(), []
    for message in messages:
        keys.add(make_message_key(message))
        if not (isinstance(message.get("id"), str) and message["id"]):
            body = json.dumps(message, ensure_ascii=False, sort_keys=True)
            identity = f"{key.system}:{key.thread}:{body}"
            digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
            message = {**message, "id": f"m_{digest}"}
            keys.add(make_message_key(message))
        out.append(message)
    return out, keys


def _key(system_id: str, thread_id: str) -> ThreadKey:
    return ThreadKey(system=system_id, thread=thread_id)


#: Порт на одно хранилище один — как у адаптера синхронных хранилищ.
_PORT_ATTR = "_gc_session_port"


def attach_session_port(store: ThreadStore, port: SessionPort) -> None:
    """Порт сессии, заданный владельцем хранилища (workflow Temporal: свой
    владелец аренды, бессрочно), — его и отдаст ``session_port``."""
    setattr(store, _PORT_ATTR, port)


def session_port(store: ThreadStore) -> SessionPort:
    """Порт сессии для хранилища v2 (один на объект хранилища)."""
    port = getattr(store, _PORT_ATTR, None)
    if port is None:
        port = SessionPort(store)
        try:
            setattr(store, _PORT_ATTR, port)
        except AttributeError:  # __slots__ — просто без кэша
            pass
    return port
