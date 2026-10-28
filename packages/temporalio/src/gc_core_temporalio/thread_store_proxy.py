"""Хранилище тредов v2 в коде workflow: операции — activities воркера.

``TemporalThreadStore`` реализует ``ThreadStore`` v2; ядро говорит с ним
через сессию треда (``ThreadSession``) — так же, как локально с файловым
хранилищем; политику аренды сессии задаёт ``session_lease_policy``. Каждая
операция — activity ``universal_agent.thread.<операция>`` (транспорт —
``ThreadStoreTransport``), хранилище на воркере — любое v2.

Запись истории Temporal ограничена (2 МБ на запись, 50 МБ на историю),
поэтому прокси (``thread_store_contracts``):

* читает большие ответы кусками и собирает их;
* держит последнюю запись треда, которую видел: воркер отвечает разницей к
  ней или признаком «не изменилась»;
* шлёт ``set_state`` разницей к состоянию на ``expected_revision``, а на
  ``BaseMismatch`` — целиком (повтор после успешной записи хранилище узнаёт
  по ``op_id``).

Владелец аренды — workflow ID сессии, бессрочно (``ttl=None``). Код
исполняется в песочнице workflow: без часов, случайности и файлов.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

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
    check_stream_name,
)
from gc_core.threads.store import ThreadStore

from gc_core_temporalio.message_refs import message_hash
from gc_core_temporalio.meta_delta import (
    TextIndex,
    apply,
    diff,
    meta_hash,
    normalize,
)
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    RECORD_PARTS,
    BaseMismatch,
    ThreadCall,
    ThreadChanged,
    ThreadReply,
    expected_record,
    strip_fetch_cache,
)

_HISTORY = "messages"
#: Activities операций хранилища (полные имена — их сверяет
#: ``test_port_contract`` с регистрацией воркера).
_ACTIVITIES = {
    "get": "universal_agent.thread.get",
    "read_stream": "universal_agent.thread.read_stream",
    "read_archive": "universal_agent.thread.read_archive",
    "resolve_alias": "universal_agent.thread.resolve_alias",
    "commit": "universal_agent.thread.commit",
    "append_side": "universal_agent.thread.append_side",
    "claim": "universal_agent.thread.claim",
    "release": "universal_agent.thread.release",
    "delete": "universal_agent.thread.delete",
}
#: Чтений заново, если ответ менялся между кусками.
_ATTEMPTS = 3
_NOT_IN_WORKFLOW = "операция хранилища тредов не вызывается из кода workflow"


class TemporalThreadStore(ThreadStore):
    """``ThreadStore`` v2 поверх activities хранилища ``store_id``.

    ``call(name, ThreadCall) -> ThreadReply`` — транспорт activity.
    ``owner`` — владелец аренды (workflow ID сессии).
    """

    def __init__(
        self,
        store_id: str,
        call: Callable[[str, ThreadCall], Any],
        *,
        owner: str,
        threads_dir: str = "",
    ) -> None:
        self.store_id = store_id
        self._call = call
        self.owner = owner
        #: Каталог тредов воркера; в workflow пустой — путей здесь нет.
        self.threads_dir = threads_dir
        #: Потолок одной записи: отложенные шаги хода (persist: turn) ядро
        #: пишет частями по нему.
        self.max_operation_bytes = PAGE_BYTES
        #: (система, тред) → хэши сообщений, которые есть в треде: activity
        #: модели получает их ссылками (``message_refs``).
        self.message_hashes: dict[tuple[str, str], set[str]] = {}
        #: Аренда треда сессии (переживает Continue-As-New и идёт детям).
        self.lease: Lease | None = None
        #: тред → (отпечаток, запись JSON), которую видел workflow.
        self._records: dict[ThreadKey, tuple[str, dict]] = {}
        #: тред → ключи истории хранилища из последнего чтения.
        self._keys: dict[ThreadKey, set[str] | None] = {}

    # --- провод -----------------------------------------------------------

    async def _ask(self, op: str, key: ThreadKey, args: dict) -> ThreadReply:
        return await self._call(
            _ACTIVITIES[op],
            ThreadCall(self.store_id, key.system, key.thread, args),
        )

    async def _text(
        self, op: str, key: ThreadKey, reply: ThreadReply, args: dict
    ):
        """JSON большого ответа: первый кусок ``reply`` и следующие той же
        версии (с теми же аргументами — воркер пересчитает, если забыл)."""
        parts, position = [reply.chunk], reply.next
        while position is not None:
            more = await self._ask(
                op, key, {**args, "offset": position, "version": reply.version}
            )
            parts.append(more.chunk)
            position = more.next
        return json.loads("".join(parts))

    async def _read(self, op: str, key: ThreadKey, args: dict):
        """Ответ целиком; изменился между кусками — заново."""
        for _attempt in range(_ATTEMPTS):
            reply = await self._ask(op, key, args)
            if reply.chunk is None:
                return reply.value
            try:
                return await self._text(op, key, reply, args)
            except ThreadChanged:
                continue
        raise ThreadChanged(
            f"тред {key.system}/{key.thread} менялся при чтении"
        )

    # --- аренда сессии ---------------------------------------------------

    def adopt(self, key: ThreadKey, lease: Lease) -> None:
        """Аренда, взятая раньше (прошлый run до Continue-As-New, родитель
        субагента): запись идёт по ней без нового ``claim``."""
        self.lease = lease
        del key  # аренда одна на сессию workflow — тред сессии

    def session_lease_policy(self) -> tuple[str, None, Lease | None]:
        """Политика аренды сессии ядра: владелец — workflow, бессрочно,
        аренда — та, что взял (или унаследовал) workflow."""
        return self.owner, None, self.lease

    # --- чтение ----------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        known = self._records.get(key)
        args = {"known": known[0]} if known is not None else {}
        for _attempt in range(_ATTEMPTS):
            reply = await self._ask("get", key, args)
            try:
                data = await self._record_data(key, reply, known)
            except ThreadChanged:
                continue
            break
        else:
            raise ThreadChanged(
                f"тред {key.system}/{key.thread} менялся при чтении"
            )
        if data is None:
            self._records.pop(key, None)
            return None
        data = normalize(data)
        self._records[key] = (meta_hash(data), data)
        record = ThreadRecord.model_validate(data)
        return record

    async def _record_data(self, key, reply: ThreadReply, known):
        if reply.unchanged:
            return known[1]
        if reply.delta is not None:
            return apply(known[1], reply.delta)
        if reply.chunk is not None:
            return await self._text("get", key, reply, {})
        return reply.value

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        raise NotImplementedError(_NOT_IN_WORKFLOW)

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        whole_history = stream == _HISTORY and not after_seq and limit is None
        data = await self._read(
            "read_stream",
            key,
            {
                "stream": stream,
                "after_seq": after_seq,
                "limit": limit,
                "keys": whole_history,
            },
        )
        entries = [StreamEntry.model_validate(e) for e in data["entries"]]
        if whole_history:
            keys = data.get("keys")
            self._keys[key] = None if keys is None else set(keys)
            self.message_hashes[(key.system, key.thread)] = {
                message_hash(e.data) for e in entries
            }
        return entries

    def history_keys(self, key: ThreadKey) -> set[str] | None:
        """Ключи истории хранилища из последнего чтения (сессия сверяет
        по ним дубли, как у файлового хранилища локально);
        ``None`` — хранилище их не ведёт."""
        return self._keys.get(key)

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        data = await self._read(
            "read_archive", key, {"archive_id": archive_id}
        )
        return [StreamEntry.model_validate(e) for e in data["entries"]]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        reply = await self._call(
            _ACTIVITIES["resolve_alias"],
            ThreadCall(self.store_id, "", "", {"alias": alias}),
        )
        return None if reply.value is None else ThreadKey(**reply.value)

    # --- запись -----------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        messages = list(change.append_messages)
        if change.replace_history is not None:
            messages += change.replace_history.messages
        reply = await self._write("commit", key, change, messages)
        receipt = Receipt.model_validate(reply.value)
        self._after_commit(key, change, receipt)
        return receipt

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        reply = await self._write("append_side", key, change, [])
        return Receipt.model_validate(reply.value)

    async def _write(self, op: str, key: ThreadKey, change, messages):
        """Запись с частями (``set_state``, ``set_runtime``…) разницей к
        известной записи треда, длинные тексты — ссылками на сообщения
        записи; воркер не знает записи — те же части с нуля. После записи —
        запись треда, какой её видит workflow (разница воркера)."""
        data = change.model_dump(mode="json", exclude_unset=True)
        values = {}
        for name in RECORD_PARTS:
            value = data.pop(name, None)
            if value is not None:
                value = normalize(value)
                values[name] = (
                    strip_fetch_cache(value) if name == "set_state" else value
                )
        known = self._records.get(key)
        texts = TextIndex()
        texts.add(messages)
        args = {"change": data, "parts": _parts(values, known, texts)}
        if known is not None:
            args["known"] = known[0]
        try:
            reply = await self._ask(op, key, args)
        except BaseMismatch:
            args = {"change": data, "parts": _parts(values, None, texts)}
            reply = await self._ask(op, key, args)
        written = {**data, **values}
        self._learn_record(
            key, known if "known" in args else None, written, reply
        )
        return reply

    def _learn_record(self, key, known, change: dict, reply: ThreadReply):
        """Запись треда после своей записи — из известной, своих частей и
        разницы воркера; без разницы — неизвестна до следующего чтения."""
        self._records.pop(key, None)
        if known is None or reply.delta is None:
            return
        data = apply(expected_record(known[1], change), reply.delta)
        if meta_hash(data) == reply.version:
            self._records[key] = (reply.version, data)

    def _after_commit(self, key, change, receipt: Receipt) -> None:
        """Хэши сообщений треда (идут в activity модели ссылками) и аренда."""
        hashes = self.message_hashes.setdefault(
            (key.system, key.thread), set()
        )
        if change.reset_history or change.replace_history is not None:
            hashes.clear()
            self._keys.pop(key, None)
        if change.replace_history is not None:
            hashes.update(
                message_hash(m) for m in change.replace_history.messages
            )
        hashes.update(message_hash(m) for m in change.append_messages)
        if receipt.lease is None and self.lease is not None:
            self.lease = None

    # --- аренда и жизненный цикл -------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        """Взять тред (у владельца, чей workflow уже не исполняется, —
        отобрать). Запись — без состояния: в историю идут только аренда и
        версии треда."""
        ttl = None if lease.ttl is None else lease.ttl / timedelta(seconds=1)
        reply = await self._ask(
            "claim", key, {"owner": lease.owner, "ttl": ttl, "take_over": True}
        )
        value = reply.value
        granted = Lease.model_validate(value["lease"])
        self.adopt(key, granted)
        return ThreadRecord(
            key=key,
            lease=granted,
            revision=value["revision"],
            generation=value["generation"],
            history_version=value["history_version"],
        )

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        await self._ask(
            "release", key, {"lease": lease.model_dump(mode="json")}
        )
        if self.lease == lease:
            self.lease = None

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        await self._ask("delete", key, {"op_id": op_id})
        self._records.pop(key, None)

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        raise NotImplementedError(_NOT_IN_WORKFLOW)

    async def get_blob(self, ref: BlobRef) -> bytes:
        raise NotImplementedError(_NOT_IN_WORKFLOW)

    async def prune_receipts(self, older_than: timedelta) -> int:
        raise NotImplementedError(_NOT_IN_WORKFLOW)


def _parts(values: dict, known, texts: TextIndex) -> dict:
    """Части записи для провода: разница к полю известной записи (``known``)
    или с нуля; длинные тексты — ссылками на сообщения записи."""
    parts = {}
    for name, value in values.items():
        if known is None:
            parts[name] = {"delta": diff(None, value, texts)}
        else:
            base = known[1].get(RECORD_PARTS[name])
            parts[name] = {"known": True, "delta": diff(base, value, texts)}
    return parts


__all__ = ["TemporalThreadStore"]
