"""S3 ThreadStore с durable-контрактом.

Тред — head-объект ``…/head.json`` и неизменяемые объекты рядом с ним:

* ``segments/<uuid>.json`` — сообщения (``[seq, message]``), дописываются новыми
  сегментами; после ``COMPACT_AFTER`` сегментов пишется один общий;
* ``traces/<uuid>.json`` — трасса субагентов, так же сегментами;
* ``receipts/<operation>/<uuid>.json`` — квитанции операций;
* ``archives/<suffix>.json`` — история до компакции.

Атомарность даёт одна условная запись head: ``If-Match`` по ETag прочитанной
версии (``If-None-Match: *`` для нового треда). Всё остальное пишется ДО неё
под уникальными ключами, поэтому проигравшая гонку попытка ничего не портит:
её объекты удаляются, операция перечитывается и применяется заново. Повтор
операции с тем же ``operation_id`` находит квитанцию по индексу в head
(последние ``RECEIPT_WINDOW`` операций) и ничего не пишет.

Объекты, на которые head больше не ссылается, удаляются после успешной
записи head. Если удаление не прошло, остаётся мусор, но не порча; его
убирает lifecycle-правило бакета.

Учётные данные и регион — стандартная цепочка boto3 (переменные окружения
``AWS_*``, профиль, роль); ``endpoint_url`` — для S3-совместимых хранилищ.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from gc_core.execution.message_keys import make_message_key
from gc_core.persistence.live_volume import (
    live_volume_from_messages,
    merge_tui_into_meta,
)
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
    ThreadStoreReceipt,
    ThreadStoreSnapshot,
)
from gc_core.thread_store_types import ThreadState
from gc_core.utils.generate_message_id import generate_msg_id_sha256

from gc_ua_temporalio.storage.durable_api import DurableThreadStoreAPI
from gc_ua_temporalio.storage.sqlite import ProjectionConflict, encode

#: Сегментов сообщений до перезаписи одним сегментом.
COMPACT_AFTER = 32
#: Сколько последних операций треда отвечают квитанцией на повтор.
RECEIPT_WINDOW = 512
#: Попыток на одну операцию при конкурентной записи head.
MAX_ATTEMPTS = 16
_CACHE_SIZE = 512
_CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict"}


def _new_head() -> dict:
    return {
        "schema_version": 1,
        "owner": None,
        "epoch": 0,
        "revision": 0,
        "generation": 0,
        "max_seq": 0,
        "meta": {},
        "segments": [],
        "traces": [],
        "trace_count": 0,
        "receipts": {},
    }


class _HeadConflict(Exception):
    """Head изменился между чтением и записью — операцию нужно повторить."""


@dataclass
class _OperationResult:
    meta: dict
    result: object = None
    archive: dict | None = None
    written: int = 0
    added: list = field(default_factory=list)
    stamp: bool = False


class _Transaction:
    """Одна попытка операции: копия head и объекты, написанные до него."""

    def __init__(self, store: S3ThreadStore, base: str, head: dict):
        self.store = store
        self.base = base
        self.head = head
        self.dirty = False
        self.new_objects: dict[str, bytes] = {}
        self.garbage: list[str] = []
        self._items: list | None = None
        self._traces: list | None = None

    # ---- сообщения -----------------------------------------------------
    def items(self) -> list:
        if self._items is None:
            self._items = [
                item
                for key in self.head["segments"]
                for item in self.store._read_list(key)
            ]
        return self._items

    def messages(self) -> list:
        return [message for _seq, message in self.items()]

    def append_items(self, new_items: list) -> None:
        if not new_items:
            return
        items = self.items()
        items.extend(new_items)
        if len(self.head["segments"]) + 1 > COMPACT_AFTER:
            self.replace_items(items)
            return
        self.head["segments"].append(self._put("segments", new_items))

    def replace_items(self, items: list) -> None:
        self.garbage.extend(self.head["segments"])
        self._items = list(items)
        self.head["segments"] = (
            [self._put("segments", self._items)] if self._items else []
        )

    # ---- трасса субагентов ---------------------------------------------
    def traces(self) -> list:
        if self._traces is None:
            self._traces = [
                record
                for key in self.head["traces"]
                for record in self.store._read_list(key)
            ]
        return self._traces

    def append_trace(self, record: dict) -> None:
        self.traces().append(record)
        if len(self.head["traces"]) + 1 > COMPACT_AFTER:
            self.garbage.extend(self.head["traces"])
            self.head["traces"] = [self._put("traces", self.traces())]
        else:
            self.head["traces"].append(self._put("traces", [record]))
        self.head["trace_count"] += 1

    def clear_traces(self) -> None:
        self.garbage.extend(self.head["traces"])
        self.head["traces"] = []
        self.head["trace_count"] = 0
        self._traces = []

    # ---- объекты -------------------------------------------------------
    def _put(self, kind: str, value: Any) -> str:
        key = f"{self.base}{kind}/{uuid.uuid4().hex}.json"
        self.new_objects[key] = encode(value).encode()
        return key

    def put_named(self, relative: str, value: Any) -> str:
        key = f"{self.base}{relative}"
        self.new_objects[key] = encode(value).encode()
        return key


class S3ThreadStore(DurableThreadStoreAPI):
    """Тред в S3 (или S3-совместимом хранилище) с durable-контрактом."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        if not bucket:
            raise ValueError("S3ThreadStore requires a bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        if client is None:
            import boto3

            client = boto3.client(
                "s3", endpoint_url=endpoint_url, region_name=region_name
            )
        self._client = client
        self._cache: OrderedDict[str, list] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ---- адресация -----------------------------------------------------
    def _base(self, system_id: str, thread_id: str) -> str:
        return (
            f"{self.prefix}threads/{quote(system_id, safe='')}/"
            f"{quote(thread_id, safe='')}/"
        )

    # ---- низкий уровень ------------------------------------------------
    @staticmethod
    def _error_code(exc: Exception) -> str:
        response = getattr(exc, "response", None) or {}
        return str(response.get("Error", {}).get("Code", ""))

    def _get_head(self, base: str) -> tuple[dict, str | None]:
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=base + "head.json"
            )
        except Exception as exc:
            if self._error_code(exc) in {"NoSuchKey", "404"}:
                return _new_head(), None
            raise
        head = json.loads(response["Body"].read())
        if head.get("schema_version") != 1:
            raise ValueError("Unsupported S3 thread head schema version")
        return head, response["ETag"]

    def _put_head(self, base: str, head: dict, etag: str | None) -> None:
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=base + "head.json",
                Body=encode(head).encode(),
                ContentType="application/json",
                **condition,
            )
        except Exception as exc:
            if self._error_code(exc) in _CONFLICT_CODES:
                raise _HeadConflict() from exc
            raise

    def _put_object(self, key: str, body: bytes) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    def _delete_quietly(self, keys) -> None:
        for key in keys:
            try:
                self._client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                pass  # мусор, не порча: дочистит lifecycle-правило бакета
            with self._cache_lock:
                self._cache.pop(key, None)

    def _read_list(self, key: str) -> list:
        """Неизменяемый объект-список: кэшируется в процессе по ключу."""
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return copy.deepcopy(cached)
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        value = json.loads(response["Body"].read())
        with self._cache_lock:
            self._cache[key] = value
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return copy.deepcopy(value)

    def _read_object(self, key: str) -> Any:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read())

    def _transact(self, system_id: str, thread_id: str, apply):
        """Применить ``apply(tx)`` атомарно; при гонке за head — повторить."""
        base = self._base(system_id, thread_id)
        for attempt in range(MAX_ATTEMPTS):
            head, etag = self._get_head(base)
            tx = _Transaction(self, base, head)
            result = apply(tx)
            if not tx.dirty:
                return result
            for key, body in tx.new_objects.items():
                self._put_object(key, body)
            try:
                self._put_head(base, tx.head, etag)
            except _HeadConflict:
                self._delete_quietly(tx.new_objects)
                time.sleep(random.uniform(0, 0.05 * (attempt + 1)))
                continue
            self._delete_quietly(tx.garbage)
            return result
        raise ThreadStoreConflict(
            "S3 thread head kept changing; operation not applied"
        )

    # ---- чтение --------------------------------------------------------
    def read_snapshot(self, system_id, thread_id, *, include_state=True):
        base = self._base(system_id, thread_id)
        head, _ = self._get_head(base)
        tx = _Transaction(self, base, head)
        return ThreadStoreSnapshot(
            meta=head["meta"],
            state=self._thread_state(tx) if include_state else None,
            revision=head["revision"],
            generation=head["generation"],
            max_seq=head["max_seq"],
            owner_id=head["owner"],
            owner_epoch=head["epoch"],
        )

    @staticmethod
    def _thread_state(tx: _Transaction) -> ThreadState:
        messages = tx.messages()
        return ThreadState(
            messages,
            {make_message_key(message) for message in messages},
            tx.head["max_seq"],
        )

    def load_thread_state(self, system_id, thread_id):
        base = self._base(system_id, thread_id)
        head, _ = self._get_head(base)
        return self._thread_state(_Transaction(self, base, head))

    def load_meta(self, system_id, thread_id):
        head, _ = self._get_head(self._base(system_id, thread_id))
        return head["meta"]

    def read_subagent_trace(self, system_id, thread_id):
        base = self._base(system_id, thread_id)
        head, _ = self._get_head(base)
        return _Transaction(self, base, head).traces()

    # ---- запись --------------------------------------------------------
    def commit_operation(self, system_id, thread_id, operation):
        return self._transact(
            system_id, thread_id, lambda tx: self._commit(tx, operation)
        )

    def _legacy_commit(self, system, thread, kind, payload):
        def apply(tx):
            if tx.head["owner"] is not None:
                raise ProjectionConflict(
                    "Temporal owns this thread; local writes are forbidden"
                )
            return self._commit(
                tx,
                ThreadStoreOperation(
                    uuid.uuid4().hex,
                    kind,
                    payload,
                    generation=tx.head["generation"],
                ),
            )

        return self._transact(system, thread, apply)

    def _commit(self, tx: _Transaction, operation: ThreadStoreOperation):
        if operation.schema_version != 1:
            raise ValueError("Unsupported ThreadStoreOperation schema version")
        head = tx.head
        digest = hashlib.sha256(encode(asdict(operation)).encode()).hexdigest()
        previous = head["receipts"].get(operation.operation_id)
        if previous:
            if previous["digest"] != digest:
                raise ThreadStoreOperationMismatch(
                    "Operation identity reused with different input"
                )
            return ThreadStoreReceipt(**self._read_object(previous["key"]))
        if operation.generation != head["generation"]:
            raise ThreadStoreOwnershipError("Thread generation changed")
        if operation.kind not in ("claim_owner", "update_tui") and (
            operation.owner_id != head["owner"]
            or (
                operation.owner_epoch is not None
                and operation.owner_epoch != head["epoch"]
            )
        ):
            raise ThreadStoreOwnershipError("Thread writer ownership changed")
        if (
            operation.expected_revision is not None
            and operation.expected_revision != head["revision"]
        ):
            raise ThreadStoreConflict("Thread revision changed")
        out = _OperationResult(meta=copy.deepcopy(head["meta"]))
        self._apply(tx, copy.deepcopy(operation), out)
        if out.stamp:
            out.meta = self.stamp_meta(out.meta)
        head["meta"] = out.meta
        head["revision"] += 1
        receipt = ThreadStoreReceipt(
            operation.operation_id,
            operation.kind,
            digest,
            head["revision"],
            head["generation"],
            result=out.result,
            written=out.written,
            max_seq=head["max_seq"],
            added_known_keys=out.added,
            meta=out.meta,
            archive=out.archive,
            owner_id=head["owner"],
            owner_epoch=head["epoch"],
        )
        key = tx.put_named(
            f"receipts/{quote(operation.operation_id, safe='')}/"
            f"{uuid.uuid4().hex}.json",
            asdict(receipt),
        )
        receipts = head["receipts"]
        receipts[operation.operation_id] = {"digest": digest, "key": key}
        while len(receipts) > RECEIPT_WINDOW:
            oldest = next(iter(receipts))
            tx.garbage.append(receipts.pop(oldest)["key"])
        tx.dirty = True
        return receipt

    def _apply(self, tx, operation, out):
        handlers = {
            "persist": self._append,
            "append_messages": self._append,
            "compact": self._replace,
            "archive_and_replace": self._replace,
            "replace_messages": self._replace,
            "save_meta": self._save_meta,
            "patch_runtime_flag": self._patch_runtime_flag,
            "reset_run_flags": self._reset_run_flags,
            "update_runtime_meta": self._update_runtime_meta,
            "update_tui": self._update_tui,
            "discard_message": self._discard_message,
            "reset": self._reset,
            "append_subagent_trace": self._append_subagent_trace,
            "claim_owner": self._claim_owner,
            "release_owner": self._release_owner,
        }
        method = handlers.get(operation.kind)
        if method is None:
            raise ValueError(
                f"Unsupported durable operation: {operation.kind}"
            )
        method(tx, operation, out)

    @staticmethod
    def _new_items(tx, messages, start_seq):
        known = {make_message_key(message) for message in tx.messages()}
        seq, items, added = start_seq, [], []
        for message in messages:
            key = make_message_key(message)
            if key in known:
                continue
            known.add(key)
            stored = dict(message)
            if not stored.get("id"):
                stored["id"] = generate_msg_id_sha256("m")
            seq += 1
            items.append([seq, stored])
            added.append(key)
        return seq, items, added

    def _append(self, tx, operation, out):
        payload = operation.payload
        messages = payload["messages"]
        known = payload.get("known_keys")
        if known is not None:
            known = set(known)
            messages = [
                message
                for message in messages
                if make_message_key(message) not in known
            ]
        start = max(payload.get("start_seq", 0), tx.head["max_seq"])
        seq, items, out.added = self._new_items(tx, messages, start)
        out.written = len(out.added)
        if (
            "expected_written" in payload
            and payload["expected_written"] != out.written
        ):
            raise ThreadStoreConflict("Prepared message count changed")
        tx.append_items(items)
        if items:
            tx.head["max_seq"] = seq
        if operation.kind == "persist":
            out.meta, out.stamp = (payload["meta"], True)
        out.result = out.written

    def _replace(self, tx, operation, out):
        payload = operation.payload
        if operation.kind != "replace_messages":
            suffix = payload.get("archive_suffix") or uuid.uuid4().hex
            tx.put_named(
                f"archives/{quote(suffix, safe='')}.json",
                {"messages": tx.messages(), "meta": out.meta},
            )
            out.archive = {
                "archive_suffix": suffix,
                "archive_data_path": None,
                "archive_meta_path": None,
            }
        tx.replace_items([])
        seq, items, out.added = self._new_items(tx, payload["messages"], 0)
        tx.replace_items(items)
        tx.head["max_seq"] = seq
        out.written = len(out.added)
        if "meta" in payload:
            out.meta, out.stamp = (payload["meta"], True)
        out.result = (
            out.archive if operation.kind != "replace_messages" else None
        )

    @staticmethod
    def _save_meta(_tx, operation, out):
        out.meta, out.stamp = (operation.payload["meta"], True)

    def _patch_runtime_flag(self, tx, operation, out):
        payload = operation.payload
        out.meta = self.apply_runtime_flag(
            out.meta, payload["flag"], active=payload["active"]
        )
        out.stamp = True

    @staticmethod
    def _reset_run_flags(_tx, operation, out):
        from gc_core.persistence.meta_ownership import clear_run_flags

        clear_run_flags(
            out.meta,
            stale_summarizing=operation.payload.get(
                "stale_summarizing", False
            ),
        )
        out.stamp = True

    def _update_runtime_meta(self, tx, operation, out):
        out.meta = self.merge_runtime_meta(
            out.meta, operation.payload["patch"]
        )
        out.stamp = True

    @staticmethod
    def _update_tui(_tx, operation, out):
        out.meta.setdefault("tui", {}).update(operation.payload["patch"])
        out.stamp = True

    @staticmethod
    def _discard_message(tx, operation, out):
        message_id = operation.payload["message_id"]
        items = tx.items()
        kept = [item for item in items if item[1].get("id") != message_id]
        removed = len(items) - len(kept)
        out.result = bool(removed)
        if not removed:
            return
        tx.replace_items(kept)
        tx.head["max_seq"] = max((seq for seq, _ in kept), default=0)
        try:
            out.meta["messages_count"] = max(
                0, int(out.meta.get("messages_count", 0)) - removed
            )
        except (TypeError, ValueError):
            pass
        merge_tui_into_meta(
            out.meta, live_volume_from_messages([m for _, m in kept])
        )
        out.stamp = True

    @staticmethod
    def _reset(tx, _operation, out):
        out.result = []
        if tx.items():
            out.result.append("messages")
        if tx.traces():
            out.result.append("traces")
        out.result.append("threads")
        tx.replace_items([])
        tx.clear_traces()
        out.meta = {}
        head = tx.head
        head["owner"] = None
        head["epoch"] += 1
        head["max_seq"] = 0
        head["generation"] += 1

    @staticmethod
    def _append_subagent_trace(tx, operation, out):
        payload = operation.payload
        out.result = dict(
            ts=datetime.now(UTC).isoformat(),
            seq=tx.head["trace_count"] + 1,
            parent_call_id=payload["parent_call_id"],
            child_run_id=payload["child_run_id"],
            kind=payload["kind"],
            payload=payload["payload"],
        )
        tx.append_trace(out.result)

    @staticmethod
    def _claim_owner(tx, operation, _out):
        owner = operation.payload.get("owner_id", operation.owner_id)
        current = tx.head["owner"]
        if not owner or current not in (None, owner):
            raise ThreadStoreOwnershipError(
                "Thread already has a different owner"
            )
        if current != owner:
            tx.head["owner"] = owner
            tx.head["epoch"] += 1

    @staticmethod
    def _release_owner(tx, _operation, _out):
        tx.head["owner"] = None
        tx.head["epoch"] += 1


__all__ = ["S3ThreadStore"]
