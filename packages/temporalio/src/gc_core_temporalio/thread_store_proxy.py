"""Async ThreadStore facade; every I/O operation uses the supplied transport.

ThreadSession, compaction and sidecar writers use their normal store contract.
The proxy contains no history/meta policy and never opens a local file.
"""

from copy import deepcopy
from dataclasses import replace

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
)
from gc_core.thread_store_types import ThreadState
from gc_core.utils.generate_message_id import generate_msg_id_sha256

from gc_core_temporalio.message_refs import message_hash
from gc_core_temporalio.sizes import payload_size
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    ThreadStoreCall,
)

# Only these operations acknowledge metadata without changing thread history.
_METADATA_OPERATIONS = frozenset(
    {
        "save_meta",
        "patch_runtime_flag",
        "reset_run_flags",
        "update_runtime_meta",
    }
)


def _history_fence(value):
    return (value.generation, value.owner_id, value.owner_epoch, value.max_seq)


#: Бюджет журнала намерений (``user_intent_log``) в meta треда — в байтах
#: истории Temporal. Meta целиком уходит в каждую запись и квитанцию, а журнал
#: хранит текст каждого сообщения пользователя: без бюджета долгий тред или
#: несколько больших сообщений упирались в лимит записи (2 МБ).
INTENT_LOG_BUDGET = 256 * 1024
#: Длиннее — запись журнала укорачивается: полный текст сообщения есть в треде.
INTENT_RECORD_MAX_CHARS = 4_000


def _bounded_meta(meta):
    """Meta для записи: журнал намерений в пределах бюджета (локальный режим
    хранит его целиком; у Temporal предел записи)."""
    log = meta.get("user_intent_log") if isinstance(meta, dict) else None
    if not isinstance(log, list) or not log:
        return meta
    records = []
    for record in log:
        content = record.get("content") if isinstance(record, dict) else None
        if isinstance(content, str) and len(content) > INTENT_RECORD_MAX_CHARS:
            record = {
                **record,
                "content": content[:INTENT_RECORD_MAX_CHARS]
                + " …[обрезано; полный текст — в треде]",
            }
        records.append(record)
    # Старые записи уходят первыми: свежие намерения важнее для сжатия.
    while len(records) > 1 and payload_size(records) > INTENT_LOG_BUDGET:
        records.pop(0)
    return {**meta, "user_intent_log": records}


def _hashable(value):
    """JSON-массив обратно в кортеж: ключ идемпотентности — элемент множества."""
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


class TemporalThreadStore:
    def __init__(
        self,
        store_id,
        call,
        *,
        owner_id=None,
        owner_epoch=None,
        generation=0,
        capabilities=(),
        threads_dir="",
    ):
        self.store_id = store_id
        self._call = call
        self.owner_id = owner_id
        self.owner_epoch = owner_epoch
        self._generation = generation
        self._capabilities = frozenset(capabilities)
        self.threads_dir = threads_dir
        # Per-thread (base revision, latest confirmed value). The interval above
        # base contains only contiguous own metadata receipts with one fence.
        self._metadata_acks = {}
        #: (система, тред) → хэши сообщений последнего чтения треда.
        self.message_hashes = {}
        #: Потолок одной операции: вход activity — запись истории Temporal
        #: (лимит 2 МБ). Отложенные шаги хода (persist: turn) ядро пишет
        #: частями по нему.
        self.max_operation_bytes = PAGE_BYTES

    async def aread_snapshot(
        self, system_id, thread_id, *, include_state=True
    ):
        if include_state:
            snapshot = await self._read_paged(system_id, thread_id)
        else:
            reply = await self._call(
                "agent.thread.load",
                ThreadStoreCall(
                    self.store_id, system_id, thread_id, include_state=False
                ),
            )
            snapshot = reply.snapshot
        if snapshot is None:
            raise ExecutionFailure("Thread store did not return a snapshot")
        if snapshot.state is not None:
            # Эти сообщения есть в треде: activity модели получает их
            # ссылками, воркер найдёт их в хранилище (``message_refs``).
            self.message_hashes[(system_id, thread_id)] = {
                message_hash(message) for message in snapshot.state.messages
            }
        self._generation = snapshot.generation
        key = (system_id, thread_id)
        previous = self._metadata_acks.get(key)
        if previous is None or (
            previous[1].revision != snapshot.revision
            or _history_fence(previous[1]) != _history_fence(snapshot)
            or previous[1].meta != snapshot.meta
        ):
            self._metadata_acks[key] = (
                snapshot.revision,
                replace(
                    snapshot,
                    state=None,
                    meta=deepcopy(snapshot.meta),
                ),
            )
        return snapshot

    async def _read_paged(self, system_id, thread_id, *, attempts=3):
        """Снимок с состоянием страницами по ``PAGE_BYTES``.

        Каждая страница — отдельная activity: результат одной activity — одна
        запись истории Temporal, и тред любой длины не упирается в лимит
        2 МБ на запись. Тред изменился между страницами — чтение заново.
        """
        for attempt in range(attempts):
            first = await self._call(
                "agent.thread.load",
                ThreadStoreCall(
                    self.store_id, system_id, thread_id, page_bytes=PAGE_BYTES
                ),
            )
            snapshot, page = first.snapshot, first.page
            if snapshot is None or page is None:
                return snapshot
            messages, keys = list(page.messages), list(page.keys)
            cursor = page.next_cursor
            try:
                while cursor is not None:
                    reply = await self._call(
                        "agent.thread.load",
                        ThreadStoreCall(
                            self.store_id,
                            system_id,
                            thread_id,
                            page_bytes=PAGE_BYTES,
                            cursor=cursor,
                            expect=[snapshot.revision, snapshot.generation],
                        ),
                    )
                    messages += reply.page.messages
                    keys += reply.page.keys
                    cursor = reply.page.next_cursor
            except ThreadStoreConflict:
                if attempt + 1 == attempts:
                    raise
                continue
            # Ключи приходят JSON-массивами: множество контракта ядра.
            snapshot.state = ThreadState(
                messages, {_hashable(key) for key in keys}, page.max_seq
            )
            return snapshot

    async def acommit_operation(self, system_id, thread_id, operation):
        key = (system_id, thread_id)
        payload = operation.payload
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
            # Все записи треда идут сюда (ядро зовёт и напрямую): журнал
            # намерений в meta — в пределах бюджета записи истории.
            operation = replace(
                operation,
                payload={**payload, "meta": _bounded_meta(payload["meta"])},
            )
        try:
            reply = await self._call(
                "agent.thread.commit",
                ThreadStoreCall(
                    self.store_id, system_id, thread_id, operation
                ),
            )
        except BaseException:
            # No acknowledged outcome: never bridge this gap on a later call.
            self._metadata_acks.pop(key, None)
            raise
        receipt = reply.receipt
        if (
            receipt is None
            or receipt.operation_id != operation.operation_id
            or receipt.kind != operation.kind
        ):
            self._metadata_acks.pop(key, None)
            raise ExecutionFailure(
                "Thread store did not acknowledge operation"
            )
        self._generation = receipt.generation
        previous = self._metadata_acks.get(key)
        base = receipt.revision
        if (
            previous is not None
            and operation.kind in _METADATA_OPERATIONS
            and receipt.revision == previous[1].revision + 1
            and _history_fence(receipt) == _history_fence(previous[1])
            and receipt.written == 0
            and not receipt.added_known_keys
        ):
            base = previous[0]
        self._metadata_acks[key] = (base, deepcopy(receipt))
        messages = (
            payload.get("messages") if isinstance(payload, dict) else None
        )
        if isinstance(messages, list):
            # Записанные сообщения тоже есть в треде: повтор запроса модели
            # после промаха кэша воркера передаст их ссылками.
            self.message_hashes.setdefault(key, set()).update(
                message_hash(message) for message in messages
            )
        return receipt

    def metadata_ack_since(self, system_id, thread_id, revision):
        """Return an own metadata receipt only across a proven contiguous span.

        A fresh read or a history mutation can establish a new baseline, but
        cannot justify adopting it from an older session snapshot.
        """
        interval = self._metadata_acks.get((system_id, thread_id))
        if interval is None:
            return None
        base, latest = interval
        if base <= revision < latest.revision:
            return deepcopy(latest)
        return None

    async def _mutate(self, system_id, thread_id, kind, payload):
        return await self.acommit_operation(
            system_id,
            thread_id,
            ThreadStoreOperation(
                operation_id=generate_msg_id_sha256("store"),
                kind=kind,
                payload=deepcopy(payload),
                generation=self._generation,
                owner_id=self.owner_id,
                owner_epoch=self.owner_epoch,
            ),
        )

    async def aload_thread_state(self, system_id, thread_id):
        snapshot = await self.aread_snapshot(system_id, thread_id)
        if snapshot.state is None:
            raise ExecutionFailure("Thread store did not return thread state")
        return snapshot.state

    async def aload_messages(self, system_id, thread_id):
        return (await self.aload_thread_state(system_id, thread_id)).messages

    async def aload_meta(self, system_id, thread_id):
        snapshot = await self.aread_snapshot(
            system_id, thread_id, include_state=False
        )
        return snapshot.meta

    async def aappend_messages(
        self,
        system_id,
        thread_id,
        messages,
        *,
        known_keys=None,
        start_seq=0,
    ):
        receipt = await self._mutate(
            system_id,
            thread_id,
            "append_messages",
            {
                "messages": list(messages),
                "known_keys": (
                    sorted(known_keys) if known_keys is not None else None
                ),
                "start_seq": start_seq,
            },
        )
        if known_keys is not None:
            known_keys.update(receipt.added_known_keys)
        return receipt.written

    async def asave_meta(self, system_id, thread_id, meta):
        await self._mutate(system_id, thread_id, "save_meta", {"meta": meta})

    async def apatch_runtime_flag(
        self,
        system_id,
        thread_id,
        flag,
        *,
        active,
        session_meta=None,
    ):
        receipt = await self._mutate(
            system_id,
            thread_id,
            "patch_runtime_flag",
            {"flag": flag, "active": active},
        )
        if session_meta is not None:
            for section in ("runtime", "tui"):
                if section in receipt.meta:
                    session_meta[section] = deepcopy(receipt.meta[section])
                else:
                    session_meta.pop(section, None)

    @property
    def areset_run_flags(self):
        if "reset_run_flags" not in self._capabilities:
            return None
        return self._areset_run_flags

    async def _areset_run_flags(
        self, system_id, thread_id, *, session_meta=None
    ):
        runtime = (session_meta or {}).get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        receipt = await self._mutate(
            system_id,
            thread_id,
            "reset_run_flags",
            {"stale_summarizing": bool(runtime.get("summarizing"))},
        )
        if session_meta is not None:
            for section in ("runtime", "tui"):
                if section in receipt.meta:
                    session_meta[section] = deepcopy(receipt.meta[section])
                else:
                    session_meta.pop(section, None)

    async def aupdate_runtime_meta(self, system_id, thread_id, patch):
        await self._mutate(
            system_id, thread_id, "update_runtime_meta", {"patch": patch}
        )

    async def adiscard_message(self, system_id, thread_id, message_id):
        receipt = await self._mutate(
            system_id, thread_id, "discard_message", {"message_id": message_id}
        )
        return bool(receipt.result)

    async def areset(self, system_id, thread_id):
        receipt = await self._mutate(system_id, thread_id, "reset", {})
        return receipt.result

    async def aappend_subagent_trace(
        self,
        system_id,
        thread_id,
        *,
        parent_call_id,
        child_run_id,
        kind,
        payload,
    ):
        receipt = await self._mutate(
            system_id,
            thread_id,
            "append_subagent_trace",
            {
                "parent_call_id": parent_call_id,
                "child_run_id": child_run_id,
                "kind": kind,
                "payload": payload,
            },
        )
        return receipt.result

    async def aread_subagent_trace(self, system_id, thread_id):
        """Трасса субагента страницами по ``PAGE_BYTES`` (как тред)."""
        records, cursor = [], [0]
        while cursor is not None:
            reply = await self._call(
                "agent.thread.read_trace",
                ThreadStoreCall(
                    self.store_id,
                    system_id,
                    thread_id,
                    page_bytes=PAGE_BYTES,
                    cursor=cursor,
                ),
            )
            if reply.trace is None:
                raise ExecutionFailure("Thread store did not return a trace")
            records += reply.trace
            cursor = None if reply.trace_next is None else [reply.trace_next]
        return records

    @property
    def areplace_messages(self):
        if "replace_messages" not in self._capabilities:
            return None
        return self._areplace_messages

    async def _areplace_messages(self, system_id, thread_id, messages):
        await self._mutate(
            system_id,
            thread_id,
            "replace_messages",
            {"messages": list(messages)},
        )

    @property
    def aarchive_and_replace(self):
        if "archive_and_replace" not in self._capabilities:
            return None
        return self._aarchive_and_replace

    async def _aarchive_and_replace(self, system_id, thread_id, messages):
        receipt = await self._mutate(
            system_id,
            thread_id,
            "archive_and_replace",
            {"messages": list(messages)},
        )
        return receipt.archive
