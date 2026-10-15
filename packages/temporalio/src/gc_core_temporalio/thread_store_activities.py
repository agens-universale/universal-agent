"""Activities хранилища тредов v2: workflow → хранилище воркера.

Каждая activity — одна операция контракта ``ThreadStore`` v2 над хранилищем
``store_id`` воркера (файловое, SQLite, Postgres, своё). Политики сессии
здесь нет — только провод: куски больших ответов, разница записи треда,
сборка состояния из разницы (``thread_store_contracts``).

Кэш страниц ``fetch_url`` (``state.fetch_url_cache``) под Temporal живёт в
памяти воркера (``activities.FetchPageCache``) и в историю не идёт: ответы
отдают его пустым, а ``commit`` с пустым кэшем сохраняет кэш хранилища (его
вёл локальный режим).
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import timedelta
from typing import Any

from gc_core.execution.contracts import ExecutionFailure
from gc_core.threads.errors import (
    OperationMismatch,
    OwnershipError,
    ThreadConflict,
)
from gc_core.threads.models import (
    Lease,
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadRecord,
)
from gc_core.threads.store import ThreadStore
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from gc_core_temporalio.meta_delta import (
    MetaDeltaError,
    apply,
    diff,
    meta_hash,
    normalize,
)
from gc_core_temporalio.sizes import payload_size
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    BaseMismatch,
    ThreadCall,
    ThreadChanged,
    ThreadReply,
    expected_record,
    strip_fetch_cache,
)

#: Ответов целиком в кэше воркера для следующих кусков.
_TEXT_CACHE_SIZE = 16
#: Записей тредов, которые видел workflow, — база разницы ответа ``get``.
_RECORD_CACHE_SIZE = 64
#: Разница больше — запись целиком (кусками).
_DELTA_BYTES = PAGE_BYTES // 2
_HISTORY = "messages"
#: Отказы контракта и хода — доменные: повтор activity их не исправит.
_DOMAIN_ERRORS = (
    ExecutionFailure,
    ThreadConflict,
    OwnershipError,
    OperationMismatch,
    BaseMismatch,
    ThreadChanged,
    ValueError,
)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _text_chunk(text: str, start: int, budget: int):
    """Кусок JSON-текста с ``start`` не больше ``budget`` байт в истории
    (кавычки и ``\\`` в строке экранируются ещё раз). → (кусок, следующая
    позиция или ``None``)."""
    length = max(budget - 2, 1)
    chunk = text[start : start + length]
    while len(chunk) > 1 and payload_size(chunk) > budget:
        length = max(length // 2, 1)
        chunk = text[start : start + length]
    end = start + len(chunk)
    return chunk, end if end < len(text) else None


def record_json(record: ThreadRecord) -> dict:
    """Запись треда для workflow: только заданные поля (по ним строится
    meta), без кэша ``fetch_url``."""
    data = normalize(record.model_dump(mode="json", exclude_unset=True))
    if "state" in data:
        data["state"] = strip_fetch_cache(data["state"])
    return data


def state_json(record: ThreadRecord) -> dict:
    """Состояние сессии записи — база разницы ``set_state``."""
    return strip_fetch_cache(
        normalize(record.state.model_dump(mode="json", exclude_unset=True))
    )


def _keep_fetch_cache(
    change: ThreadChange, record: ThreadRecord | None
) -> ThreadChange:
    """Пустой кэш ``fetch_url`` в ``set_state`` не стирает кэш хранилища."""
    if change.set_state is None or record is None:
        return change
    cache = record.state.fetch_url_cache
    if not cache or change.set_state.fetch_url_cache:
        return change
    state = change.set_state.model_copy(update={"fetch_url_cache": cache})
    return change.model_copy(update={"set_state": state})


def _domain_error(exc: Exception) -> ApplicationError:
    return ApplicationError(
        str(exc), type=type(exc).__name__, non_retryable=True
    )


class ThreadStoreActivities:
    """Activities ``universal_agent.thread.*`` над хранилищами воркера."""

    def __init__(self, stores: dict[str, ThreadStore]):
        for name, store in stores.items():
            if not isinstance(store, ThreadStore):
                raise ValueError(
                    f"ThreadStore {name!r}: нужен ThreadStore v2 "
                    "(gc_core.threads.store), а не "
                    f"{type(store).__name__}"
                )
        self.raw = dict(stores)
        #: (хранилище, операция, система, тред, аргументы) → (версия, текст).
        self._texts: OrderedDict = OrderedDict()
        #: (хранилище, система, тред, версия) → запись, которую видел workflow.
        self._records: OrderedDict = OrderedDict()

    def _store(self, call: ThreadCall) -> ThreadStore:
        try:
            return self.raw[call.store_id]
        except KeyError:
            raise ApplicationError(
                f"Unknown configured ThreadStore {call.store_id!r}",
                type="ExecutionFailure",
                non_retryable=True,
            ) from None

    @staticmethod
    def _key(call: ThreadCall) -> ThreadKey:
        return ThreadKey(system=call.system, thread=call.thread)

    async def _run(self, operation, call: ThreadCall) -> ThreadReply:
        try:
            # Алиас глобален: у вызова resolve_alias треда нет.
            key = self._key(call) if call.thread else None
            return await operation(self._store(call), key, call)
        except _DOMAIN_ERRORS as exc:
            raise _domain_error(exc) from exc

    # --- куски больших ответов ---------------------------------------------

    def _remember(self, cache: OrderedDict, key, value, size: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)

    async def _chunked(
        self, call: ThreadCall, op: str, produce
    ) -> ThreadReply:
        """Ответ ``produce()`` кусками; следующий кусок — той же версии
        (из кэша или пересчитанный), иначе ``ThreadChanged``."""
        args = dict(call.args)
        offset = args.pop("offset", 0)
        wanted = args.pop("version", None)
        slot = (call.store_id, op, call.system, call.thread, _json_text(args))
        cached = self._texts.get(slot)
        if offset and cached is not None and cached[0] == wanted:
            version, text = cached
        else:
            text = _json_text(await produce())
            version = _version(text)
            self._remember(
                self._texts, slot, (version, text), _TEXT_CACHE_SIZE
            )
            if offset and version != wanted:
                raise ThreadChanged("ответ изменился между кусками")
        chunk, nxt = _text_chunk(text, offset, PAGE_BYTES)
        return ThreadReply(chunk=chunk, next=nxt, version=version)

    # --- чтение ------------------------------------------------------------

    @activity.defn(name="universal_agent.thread.get")
    async def get(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._get, call)

    async def _get(self, store, key, call):
        if call.args.get("offset"):
            return await self._chunked(
                call, "get", lambda: self._record(store, key)
            )
        record = await store.get_thread(key)
        if record is None:
            return ThreadReply(value=None)
        data = record_json(record)
        version = meta_hash(data)
        known = call.args.get("known")
        if known == version:
            return ThreadReply(unchanged=True, version=version)
        slot = (call.store_id, call.system, call.thread)
        base = self._records.get((*slot, known)) if known else None
        self._remember(
            self._records, (*slot, version), data, _RECORD_CACHE_SIZE
        )
        if base is not None:
            delta = diff(base, data)
            if payload_size(delta) <= _DELTA_BYTES:
                return ThreadReply(delta=delta, version=version)

        async def whole():
            return data

        return await self._chunked(
            ThreadCall(call.store_id, call.system, call.thread, {}),
            "get",
            whole,
        )

    async def _record(self, store, key):
        record = await store.get_thread(key)
        return None if record is None else record_json(record)

    @activity.defn(name="universal_agent.thread.read_stream")
    async def read_stream(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._read_stream, call)

    async def _read_stream(self, store, key, call):
        args = call.args

        async def produce():
            entries = await store.read_stream(
                key,
                args["stream"],
                after_seq=args.get("after_seq", 0),
                limit=args.get("limit"),
            )
            keys = None
            history_keys = getattr(store, "history_keys", None)
            if args.get("keys") and callable(history_keys):
                keys = sorted(history_keys(key))
            return {
                "entries": [e.model_dump(mode="json") for e in entries],
                "keys": keys,
            }

        return await self._chunked(call, "read_stream", produce)

    @activity.defn(name="universal_agent.thread.read_archive")
    async def read_archive(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._read_archive, call)

    async def _read_archive(self, store, key, call):
        async def produce():
            entries = await store.read_archive(key, call.args["archive_id"])
            return {"entries": [e.model_dump(mode="json") for e in entries]}

        return await self._chunked(call, "read_archive", produce)

    @activity.defn(name="universal_agent.thread.resolve_alias")
    async def resolve_alias(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._resolve_alias, call)

    async def _resolve_alias(self, store, _key, call):
        found = await store.resolve_alias(call.args["alias"])
        return ThreadReply(value=None if found is None else found.model_dump())

    # --- запись ------------------------------------------------------------

    @activity.defn(name="universal_agent.thread.commit")
    async def commit(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._commit, call)

    async def _commit(self, store, key, call):
        data = dict(call.args["change"])
        record = await store.get_thread(key)
        state = call.args.get("state")
        if state is not None:
            data["set_state"] = _state_from(state, record, data)
        change = _keep_fetch_cache(ThreadChange.model_validate(data), record)
        receipt = await store.commit(key, change)
        reply = ThreadReply(value=receipt.model_dump(mode="json"))
        known = call.args.get("known")
        base = self._records.get(
            (call.store_id, call.system, call.thread, known)
        )
        if base is not None:
            await self._after_commit(
                store, key, call, reply, expected_record(base, data)
            )
        return reply

    async def _after_commit(self, store, key, call, reply, expected) -> None:
        """Запись треда после ``commit`` — разницей к той, что workflow
        соберёт сам (известная + свои части): следующий ``get`` ответит
        «не изменилась», и состояние не идёт в историю второй раз."""
        record = await store.get_thread(key)
        if record is None:
            return
        data = record_json(record)
        version = meta_hash(data)
        slot = (call.store_id, call.system, call.thread, version)
        self._remember(self._records, slot, data, _RECORD_CACHE_SIZE)
        delta = diff(expected, data)
        if payload_size(delta) <= _DELTA_BYTES:
            reply.delta, reply.version = delta, version

    @activity.defn(name="universal_agent.thread.append_side")
    async def append_side(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._append_side, call)

    async def _append_side(self, store, key, call):
        change = SideChange.model_validate(call.args["change"])
        receipt = await store.append_side(key, change)
        return ThreadReply(value=receipt.model_dump(mode="json"))

    # --- аренда и жизненный цикл --------------------------------------------

    @activity.defn(name="universal_agent.thread.claim")
    async def claim(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._claim, call)

    async def _claim(self, store, key, call):
        ttl = call.args.get("ttl")
        request = LeaseRequest(
            owner=call.args["owner"],
            ttl=None if ttl is None else timedelta(seconds=ttl),
        )
        try:
            record = await store.claim(key, request)
        except OwnershipError:
            if not call.args.get("take_over"):
                raise
            record = await _take_over(store, key, request)
        return ThreadReply(
            value={
                "lease": record.lease.model_dump(mode="json"),
                "revision": record.revision,
                "generation": record.generation,
                "history_version": record.history_version,
            }
        )

    @activity.defn(name="universal_agent.thread.release")
    async def release(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._release, call)

    async def _release(self, store, key, call):
        await store.release(key, Lease.model_validate(call.args["lease"]))
        return ThreadReply()

    @activity.defn(name="universal_agent.thread.delete")
    async def delete(self, call: ThreadCall) -> ThreadReply:
        return await self._run(self._delete, call)

    async def _delete(self, store, key, call):
        await store.delete(key, op_id=call.args["op_id"])
        return ThreadReply()

    @property
    def registered(self):
        return [
            self.get,
            self.read_stream,
            self.read_archive,
            self.resolve_alias,
            self.commit,
            self.append_side,
            self.claim,
            self.release,
            self.delete,
        ]


def _state_from(spec: dict, record: ThreadRecord | None, change: dict):
    """``set_state`` из провода: целиком или база записи + разница.

    База — состояние записи хранилища на ``expected_revision`` с тем же
    хэшем; другой ревизии (повтор после записи, чужая запись) или хэша —
    ``BaseMismatch``: workflow пришлёт состояние целиком, и хранилище само
    узнает повтор (квитанция) или конфликт.
    """
    if "full" in spec:
        return spec["full"]
    if record is None or record.revision != change["expected_revision"]:
        raise BaseMismatch("ревизия треда не та, что у базы состояния")
    base = state_json(record)
    if meta_hash(base) != spec["base"]:
        raise BaseMismatch("состояние треда не то, что у базы разницы")
    try:
        return apply(base, spec["delta"])
    except MetaDeltaError as exc:
        raise BaseMismatch(str(exc)) from exc


async def _take_over(store, key, request: LeaseRequest):
    """Захват треда у владельца, чей workflow уже не RUNNING.

    Владелец треда — workflow ID сессии; упавшая, снятая (terminate) или
    исчезнувшая сессия иначе держала бы тред вечно. Живой чужой владелец —
    по-прежнему отказ.
    """
    record = await store.get_thread(key)
    stale = record.lease if record is not None else None
    if stale is None or stale.owner == request.owner:
        return await store.claim(key, request)
    if await _owner_alive(stale.owner):
        raise OwnershipError(f"тред ведёт живая сессия {stale.owner!r}")
    await store.release(key, stale)
    activity.logger.warning(
        "Released thread owner %s: its workflow is not running", stale.owner
    )
    return await store.claim(key, request)


async def _owner_alive(workflow_id: str, client=None) -> bool:
    """Workflow владельца ещё исполняется (без клиента — считать живым)."""
    if client is None:
        try:
            client = activity.client()
        except RuntimeError:
            return True
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        # Транзиентный сбой Temporal (UNAVAILABLE, DEADLINE…): повтор activity,
        # а не отказ захвата.
        raise
    return description.status == WorkflowExecutionStatus.RUNNING


async def release_stale_owner(
    store: ThreadStore, system_id: str, thread_id: str, client
) -> str | None:
    """Снять владельца треда, чей workflow уже не исполняется.

    Для локального TUI/CLI после terminate или падения сессии: иначе тред
    закрыт, пока его не займёт новая сессия. Живой владелец не снимается.
    Возвращает снятого владельца или ``None``.
    """
    key = ThreadKey(system=system_id, thread=thread_id)
    record = await store.get_thread(key)
    lease = record.lease if record is not None else None
    if lease is None or await _owner_alive(lease.owner, client):
        return None
    await store.release(key, lease)
    return lease.owner


__all__ = [
    "ThreadStoreActivities",
    "record_json",
    "release_stale_owner",
    "state_json",
]
