"""Activities delegate to the injected native ThreadStore, without session policy."""

import asyncio
import inspect
import json
from collections import OrderedDict
from dataclasses import replace

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    durable_store_capability,
)
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from gc_core_temporalio.message_refs import message_hash
from gc_core_temporalio.meta_delta import (
    MetaDeltaError,
    TextSource,
    apply,
    canonical,
    diff,
    meta_hash,
    normalize,
    text_refs,
)
from gc_core_temporalio.sizes import payload_size
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    MetaBaseMismatch,
    ThreadStatePage,
    ThreadStoreCall,
    ThreadStoreReply,
)

#: Снимков треда в кэше воркера для страничного чтения.
_SNAPSHOT_CACHE_SIZE = 16
#: Последних известных meta тредов — база разницы записи (``meta_delta``).
_META_CACHE_SIZE = 64
#: Больше — квитанция без разницы, workflow перечитает meta частями.
_RECEIPT_DELTA_BYTES = PAGE_BYTES // 2


def _size(value):
    return payload_size(value)


def _take(items, start, budget):
    """Записи с ``start``, пока суммарно не больше ``budget`` байт (хотя бы
    одна). → (кусок, позиция после него, остаток бюджета)."""
    end, used = start, 0
    while end < len(items):
        size = _size(items[end])
        if end > start and used + size > budget:
            break
        used += size
        end += 1
    return items[start:end], end, budget - used


def _state_page(messages, keys, cursor, budget):
    """Страница состояния: сначала сообщения, затем ключи идемпотентности."""
    m_start, k_start = cursor or (0, 0)
    page_messages, m_end, budget = _take(messages, m_start, budget)
    page_keys, k_end = [], k_start
    if m_end == len(messages) and (budget > 0 or not page_messages):
        page_keys, k_end, _ = _take(keys, k_start, max(budget, 1))
    done = m_end == len(messages) and k_end == len(keys)
    return ThreadStatePage(
        page_messages, page_keys, None if done else [m_end, k_end]
    )


def _keep_fetch_cache(meta, stored):
    """Кэш ``fetch_url`` из meta хранилища (его вёл локальный режим; workflow
    видит его пустым) не стирается полной meta запасного пути — как и
    обычной разницей, которая его не трогает."""
    shared = meta.get("shared") if isinstance(meta, dict) else None
    old = stored.get("shared") if isinstance(stored, dict) else None
    cache = old.get("fetch_url_cache") if isinstance(old, dict) else None
    if (
        isinstance(shared, dict)
        and cache
        and not shared.get("fetch_url_cache")
    ):
        meta = {**meta, "shared": {**shared, "fetch_url_cache": cache}}
    return meta


def _text_chunk(text, start, budget):
    """Кусок JSON-текста meta с ``start`` не больше ``budget`` байт в истории
    (кавычки и ``\\`` в строке экранируются ещё раз). → (кусок, следующая
    позиция или ``None``)."""
    length = max(budget - 2, 1)
    chunk = text[start : start + length]
    while len(chunk) > 1 and _size(chunk) > budget:
        length = max(length // 2, 1)
        chunk = text[start : start + length]
    end = start + len(chunk)
    return chunk, end if end < len(text) else None


class _CachedSnapshot:
    """Снимок для следующих страниц: состояние, ключи и текст meta."""

    def __init__(self, raw, has_state):
        self.raw = raw
        self.has_state = has_state
        self.snapshot = _without_fetch_cache(raw)
        state = raw.state
        self.messages = list(state.messages) if state is not None else []
        self.keys = (
            sorted(state.known_keys, key=repr) if state is not None else []
        )
        self._meta_text = None

    @property
    def version(self):
        return [self.raw.revision, self.raw.generation]

    @property
    def meta_version(self):
        """Версия meta — ревизия и хэш содержимого: прямая правка ``tui``
        (TUI) в файловом хранилище ревизию не поднимает."""
        return [*self.version, meta_hash(self.snapshot.meta)]

    @property
    def meta_text(self):
        if self._meta_text is None:
            self._meta_text = json.dumps(
                self.snapshot.meta, ensure_ascii=True, separators=(",", ":")
            )
        return self._meta_text


async def _invoke(store, name, *args, **kwargs):
    """Асинхронное хранилище — напрямую; синхронное (файлы, SQLite) — в потоке.

    Иначе блокирующий I/O держит event loop worker'а и задерживает heartbeat
    соседних activities.
    """
    native = getattr(store, "a" + name, None)
    if inspect.iscoroutinefunction(native):
        return await native(*args, **kwargs)
    return await asyncio.to_thread(getattr(store, name), *args, **kwargs)


def _without_fetch_cache(snapshot):
    """Снимок без кэша страниц ``fetch_url`` в meta (``shared``).

    Кэш остаётся в meta треда, который вёл локальный режим; под Temporal он
    живёт в памяти воркера (``activities.FetchPageCache``) и в
    историю не идёт — ни ответом этой activity, ни через состояние workflow
    (checkpoint субагента, запись meta).
    """
    meta = snapshot.meta if snapshot is not None else None
    shared = meta.get("shared") if isinstance(meta, dict) else None
    if not isinstance(shared, dict) or not shared.get("fetch_url_cache"):
        return snapshot
    return replace(
        snapshot, meta={**meta, "shared": {**shared, "fetch_url_cache": {}}}
    )


def _first_page(request, entry):
    """Первая страница: meta и начало состояния вместе в пределах
    страницы; meta больше половины — отдельными кусками."""
    snapshot = entry.snapshot
    reply = ThreadStoreReply(snapshot=replace(snapshot, state=None))
    budget = request.page_bytes - _size(snapshot.meta)
    if (
        request.meta_known is not None
        and request.meta_known == entry.meta_version
    ):
        # Эта версия meta у workflow уже есть (чтение перед ходом,
        # затем с состоянием): второй раз не передавать.
        reply.snapshot = replace(reply.snapshot, meta={})
        reply.meta_unchanged = True
        budget = request.page_bytes
    elif budget < request.page_bytes // 2:
        reply.snapshot = replace(reply.snapshot, meta={})
        reply.meta_chunk, reply.meta_next = _text_chunk(
            entry.meta_text, 0, request.page_bytes // 2
        )
        budget = 0
    if snapshot.state is None:
        # Хранилище не отдало состояние — как без страниц.
        return reply
    if budget > 0:
        page = _state_page(entry.messages, entry.keys, None, budget)
    else:
        page = ThreadStatePage(
            [],
            [],
            [0, 0] if entry.messages or entry.keys else None,
        )
    page.max_seq = snapshot.state.max_seq
    reply.page = page
    return reply


class ThreadStoreActivities:
    def __init__(self, stores):
        self.raw = dict(stores)
        #: (хранилище, система, тред) → снимок для следующих страниц чтения.
        self._snapshots = OrderedDict()
        #: (хранилище, система, тред) → ([revision, generation], meta).
        self._metas = OrderedDict()
        self.stores = {
            name: athread_store(store) for name, store in stores.items()
        }
        for name, port in self.stores.items():
            if durable_store_capability(port) is None:
                raise ValueError(
                    f"ThreadStore {name!r} needs durable snapshot/commit capability"
                )

    def _port(self, request):
        try:
            return self.raw[request.store_id]
        except KeyError:
            raise ApplicationError(
                "Unknown configured ThreadStore",
                type="ExecutionFailure",
                non_retryable=True,
            ) from None

    def _remember_meta(self, request, value):
        """Meta треда после чтения/записи — база следующей разницы."""
        key = (request.store_id, request.system_id, request.thread_id)
        meta = normalize(value.meta if isinstance(value.meta, dict) else {})
        self._metas[key] = (
            [value.revision, value.generation, meta_hash(meta)],
            meta,
        )
        self._metas.move_to_end(key)
        while len(self._metas) > _META_CACHE_SIZE:
            self._metas.popitem(last=False)

    @activity.defn(name="universal_agent.thread.load")
    async def load(self, request: ThreadStoreCall) -> ThreadStoreReply:
        store = self._port(request)
        try:
            if request.page_bytes <= 0:
                snapshot = await _invoke(
                    store,
                    "read_snapshot",
                    request.system_id,
                    request.thread_id,
                    include_state=request.include_state,
                )
                if snapshot is not None:
                    self._remember_meta(request, snapshot)
                return ThreadStoreReply(
                    snapshot=_without_fetch_cache(snapshot)
                )
            entry = await self._paged_snapshot(store, request)
            if entry is None:
                return ThreadStoreReply()
            if request.meta_cursor is not None:
                chunk, following = _text_chunk(
                    entry.meta_text, request.meta_cursor, request.page_bytes
                )
                return ThreadStoreReply(meta_chunk=chunk, meta_next=following)
            if request.cursor is not None:
                page = _state_page(
                    entry.messages,
                    entry.keys,
                    request.cursor,
                    request.page_bytes,
                )
                page.max_seq = entry.snapshot.state.max_seq
                return ThreadStoreReply(page=page)
            return _first_page(request, entry)
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    async def _paged_snapshot(self, store, request):
        """Снимок для страницы: первая читает заново, следующие — из кэша
        воркера, если версия совпадает с ``expect`` (иначе читают сами).
        Тред изменился между страницами — ``ThreadStoreConflict``: workflow
        начнёт чтение заново."""
        key = (request.store_id, request.system_id, request.thread_id)
        cached = self._snapshots.get(key)
        continuing = (
            request.cursor is not None or request.meta_cursor is not None
        )
        if (
            continuing
            and cached is not None
            and request.expect == cached.version
            and (cached.has_state or not request.include_state)
        ):
            self._snapshots.move_to_end(key)
            return cached
        snapshot = await _invoke(
            store,
            "read_snapshot",
            request.system_id,
            request.thread_id,
            include_state=request.include_state,
        )
        if snapshot is None:
            return None
        entry = _CachedSnapshot(snapshot, request.include_state)
        if continuing and request.expect != entry.version:
            raise ThreadStoreConflict("Thread changed between page reads")
        self._remember_meta(request, snapshot)
        self._snapshots[key] = entry
        self._snapshots.move_to_end(key)
        while len(self._snapshots) > _SNAPSHOT_CACHE_SIZE:
            self._snapshots.popitem(last=False)
        return entry

    @activity.defn(name="universal_agent.thread.commit")
    async def commit(self, request: ThreadStoreCall) -> ThreadStoreReply:
        if request.operation is None:
            raise ApplicationError(
                "Missing ThreadStore operation",
                type="ExecutionFailure",
                non_retryable=True,
            )
        store = self._port(request)
        try:
            full = bool((request.meta_delta or {}).get("full"))
            if full:
                # Полная meta (запасной путь): тексты и поддеревья — по
                # содержимому текущей meta хранилища, база не важна.
                base, exact = await self._store_meta(store, request), True
            else:
                base, exact = await self._meta_base(store, request)
            operation = request.operation
            expected = base
            if request.meta_delta is not None:
                # Хранилище получает полную meta, как в локальном режиме.
                expected = await self._rebuild_meta(
                    store, request, base, drift=not (full or exact)
                )
                operation = replace(
                    operation, payload={**operation.payload, "meta": expected}
                )
            if operation.kind == "claim_owner":
                receipt = await _claim(store, request, operation)
            else:
                receipt = await _commit(store, request, operation)
            self._remember_meta(request, receipt)
            # Квитанция — без meta: только то, что хранилище поменяло само
            # (разница от ожидаемой workflow meta).
            reply = ThreadStoreReply(receipt=replace(receipt, meta={}))
            if not request.need_meta:
                return reply
            change = (
                diff(expected, normalize(receipt.meta or {}))
                if exact
                else None
            )
            if not exact or _size(change) > _RECEIPT_DELTA_BYTES:
                reply.meta_reload = True
            else:
                reply.meta_delta = {"d": change}
            return reply
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    async def _meta_base(self, store, request):
        """Meta, от которой workflow считал разницу. → (meta, точно ли она).

        Не точно — у хранилища другая версия: повтор записи после того, как
        она прошла (воркер упал до ответа), или чужая запись. Разница
        абсолютна (хвосты с номера, ключи целиком) — повтор поверх уже
        записанной meta собирает ту же операцию, и хранилище отвечает
        исходной квитанцией."""
        if request.meta_base is None:
            return {}, True
        key = (request.store_id, request.system_id, request.thread_id)
        cached = self._metas.get(key)
        if cached is not None and cached[0] == request.meta_base:
            return cached[1], True
        snapshot = await _invoke(
            store,
            "read_snapshot",
            request.system_id,
            request.thread_id,
            include_state=False,
        )
        self._remember_meta(request, snapshot)
        version, meta = self._metas[key]
        return meta, version == request.meta_base

    async def _store_meta(self, store, request):
        snapshot = await _invoke(
            store,
            "read_snapshot",
            request.system_id,
            request.thread_id,
            include_state=False,
        )
        self._remember_meta(request, snapshot)
        key = (request.store_id, request.system_id, request.thread_id)
        return self._metas[key][1]

    def _foreign_write(self, request) -> bool:
        """Ревизия хранилища ушла от ожидаемой операцией: в тред писали."""
        expected = request.operation.expected_revision
        cached = self._metas.get(
            (request.store_id, request.system_id, request.thread_id)
        )
        return (
            expected is not None
            and cached is not None
            and cached[0][0] != expected
        )

    async def _rebuild_meta(self, store, request, base, *, drift=False):
        """Полная meta записи: база + разница; тексты по ссылкам — из
        сообщений операции, иначе из треда.

        ``drift`` — meta хранилища не та, от которой workflow считал разницу.
        Повтор своей уже записанной операции собирается как есть (разница
        абсолютна) — хранилище ответит исходной квитанцией. Иначе: ревизия
        ушла (чужая запись) — сразу ``ThreadStoreConflict``, ядро перечитает
        тред и повторит, полная meta не нужна; ревизия та же (правка meta без
        ревизии, ``update_tui``) — разница ложится поверх meta хранилища,
        чужая правка не теряется, workflow перечитает meta по квитанции."""
        delta = request.meta_delta.get("d")
        messages = await self._delta_messages(store, request, text_refs(delta))
        full = bool(request.meta_delta.get("full"))
        try:
            meta = apply(
                {} if full else base, delta, TextSource(messages.get, base)
            )
        except MetaDeltaError as exc:
            if self._meta_conflict(request, full=full, drift=drift):
                # Полную meta не собрать из содержимого хранилища (или в тред
                # писали): оно разошлось с workflow — конфликт, ядро
                # перечитает и повторит.
                raise ThreadStoreConflict(
                    f"Thread meta changed during this run: {exc}"
                ) from exc
            raise MetaBaseMismatch(f"Meta треда не собрать: {exc}") from exc
        if full:
            meta = _keep_fetch_cache(meta, base)
        meta = canonical(meta)
        self._check_meta_hash(request, meta, full=full, drift=drift)
        return meta

    async def _delta_messages(self, store, request, wanted):
        """Сообщения для ссылок разницы (хэши ``wanted``): из операции, затем
        из кэша снимков воркера, затем из треда в хранилище."""
        messages = {
            message_hash(message): message
            for message in request.operation.payload.get("messages") or ()
            if isinstance(message, dict)
        }
        cached = self._snapshots.get(
            (request.store_id, request.system_id, request.thread_id)
        )
        if wanted - messages.keys() and cached is not None:
            for message in cached.messages:
                messages.setdefault(message_hash(message), message)
        if wanted - messages.keys():
            snapshot = await _invoke(
                store,
                "read_snapshot",
                request.system_id,
                request.thread_id,
                include_state=True,
            )
            state = snapshot.state if snapshot is not None else None
            for message in state.messages if state is not None else ():
                messages.setdefault(message_hash(message), message)
        return messages

    def _meta_conflict(self, request, *, full, drift):
        """Расхождение meta — конфликт (ядро перечитает тред и повторит):
        полная meta или чужая запись в тред."""
        return full or (drift and self._foreign_write(request))

    def _check_meta_hash(self, request, meta, *, full, drift):
        """Собранная meta против хэша, который записал workflow."""
        expected = request.meta_delta.get("h")
        if expected is None or meta_hash(meta) == expected:
            return
        if self._meta_conflict(request, full=full, drift=drift):
            raise ThreadStoreConflict("Thread meta changed during this run")
        if not drift:
            raise MetaBaseMismatch(
                "Meta треда из разницы не совпала с записанной workflow"
            )
        # Ревизия та же: правка workflow поверх чужой правки meta.

    @activity.defn(name="universal_agent.thread.read_trace")
    async def read_trace(self, request: ThreadStoreCall) -> ThreadStoreReply:
        """Трасса субагента; с ``page_bytes`` — страница с ``cursor[0]``.

        Трасса только дописывается, поэтому позиция в ней стабильна."""
        try:
            records = await _invoke(
                self._port(request),
                "read_subagent_trace",
                request.system_id,
                request.thread_id,
            )
            if request.page_bytes <= 0:
                return ThreadStoreReply(trace=records)
            start = request.cursor[0] if request.cursor else 0
            page, end, _ = _take(list(records), start, request.page_bytes)
            return ThreadStoreReply(
                trace=page, trace_next=end if end < len(records) else None
            )
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    def registered(self):
        return [self.load, self.commit, self.read_trace]


async def _commit(store, request, operation):
    return await _invoke(
        store,
        "commit_operation",
        request.system_id,
        request.thread_id,
        operation,
    )


async def _claim(store, request, operation):
    """Захват треда; владельца, чей workflow уже не RUNNING, снять.

    Владелец треда — workflow ID сессии. Упавшая, снятая (terminate) или
    исчезнувшая сессия иначе держала бы тред вечно. Снятие идёт операцией от
    имени старого владельца с его эпохой: новая эпоха отсекает его поздние
    коммиты. Живой чужой владелец — по-прежнему отказ.
    """
    try:
        return await _commit(store, request, operation)
    except ThreadStoreConflict:
        snapshot = await _invoke(
            store,
            "read_snapshot",
            request.system_id,
            request.thread_id,
            include_state=False,
        )
        desired = operation.payload.get("owner_id", operation.owner_id)
        stale = snapshot.owner_id
        if stale not in (None, desired):
            if await _owner_alive(stale):
                raise
            await _commit(
                store,
                request,
                ThreadStoreOperation(
                    f"{operation.operation_id}/release-stale",
                    "release_owner",
                    generation=snapshot.generation,
                    owner_id=stale,
                    owner_epoch=snapshot.owner_epoch,
                ),
            )
            activity.logger.warning(
                "Released thread owner %s: its workflow is not running", stale
            )
        receipt = await _commit(
            store,
            request,
            replace(
                operation,
                operation_id=f"{operation.operation_id}/takeover",
                expected_revision=None,
                generation=snapshot.generation,
            ),
        )
        # Workflow ждёт квитанцию своей операции: захват — её исход.
        return replace(receipt, operation_id=operation.operation_id)


async def _owner_alive(workflow_id, client=None):
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


async def release_stale_owner(store, system_id, thread_id, client):
    """Снять владельца треда, чей workflow уже не исполняется.

    Для локального TUI/CLI после terminate или падения сессии: иначе тред
    закрыт, пока его не займёт новая сессия. Живой владелец не снимается.
    Возвращает снятого владельца или ``None``.
    """
    import uuid

    snapshot = await _invoke(
        store, "read_snapshot", system_id, thread_id, include_state=False
    )
    owner = snapshot.owner_id
    if owner is None or await _owner_alive(owner, client):
        return None
    await _invoke(
        store,
        "commit_operation",
        system_id,
        thread_id,
        ThreadStoreOperation(
            f"unlock/{uuid.uuid4().hex}",
            "release_owner",
            generation=snapshot.generation,
            owner_id=owner,
            owner_epoch=snapshot.owner_epoch,
        ),
    )
    return owner


def _application_error(exc):
    return ApplicationError(
        str(exc), type=type(exc).__name__, non_retryable=True
    )
