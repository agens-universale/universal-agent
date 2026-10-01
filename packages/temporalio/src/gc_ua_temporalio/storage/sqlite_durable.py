"""Optional durable operations over SQLiteThreadStore's existing tables.

SQL owns atomicity and codecs; ThreadSession owns batch metadata policy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

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

_SELECT_MAX_SEQ = (
    "SELECT coalesce(max(seq),0) FROM messages WHERE system=? AND thread=?"
)


def _encode(value):
    from gc_ua_temporalio.storage.sqlite import encode

    return encode(value)


@dataclass
class _OperationResult:
    meta: dict
    result: object = None
    archive: dict | None = None
    written: int = 0
    added: list = field(default_factory=list)
    stamp: bool = False


class SQLiteDurableOperations:
    @staticmethod
    def _control(db, system, thread):
        db.execute(
            "INSERT OR IGNORE INTO durable_control(system,thread) VALUES (?,?)",
            (system, thread),
        )
        return db.execute(
            "SELECT * FROM durable_control WHERE system=? AND thread=?",
            (system, thread),
        ).fetchone()

    @classmethod
    def _advance_revision(cls, db, system, thread):
        cls._control(db, system, thread)
        db.execute(
            "UPDATE durable_control SET revision=revision+1 WHERE system=? AND thread=?",
            (system, thread),
        )

    @staticmethod
    def _state(db, system, thread):
        rows = db.execute(
            "SELECT seq,payload FROM messages WHERE system=? AND thread=? ORDER BY seq",
            (system, thread),
        ).fetchall()
        messages = [json.loads(row["payload"]) for row in rows]
        return ThreadState(
            messages,
            {make_message_key(message) for message in messages},
            max((row["seq"] for row in rows), default=0),
        )

    def read_snapshot(self, system_id, thread_id, *, include_state=True):
        with self.transaction() as db:
            row = self._row(db, system_id, thread_id)
            control = self._control(db, system_id, thread_id)
            maximum = db.execute(
                _SELECT_MAX_SEQ,
                (system_id, thread_id),
            ).fetchone()[0]
            return ThreadStoreSnapshot(
                meta=json.loads(row["meta"]),
                state=self._state(db, system_id, thread_id)
                if include_state
                else None,
                revision=control["revision"],
                generation=control["generation"],
                max_seq=maximum,
                owner_id=row["owner"],
                owner_epoch=row["epoch"],
            )

    def commit_operation(self, system_id, thread_id, operation):
        with self.transaction() as db:
            return self._commit_operation(db, system_id, thread_id, operation)

    def _legacy_commit(self, system, thread, kind, payload):
        with self.transaction() as db:
            self._local(self._row(db, system, thread))
            control = self._control(db, system, thread)
            return self._commit_operation(
                db,
                system,
                thread,
                ThreadStoreOperation(
                    uuid.uuid4().hex,
                    kind,
                    payload,
                    generation=control["generation"],
                ),
            )

    def _commit_operation(self, db, system, thread, operation):
        if operation.schema_version != 1:
            raise ValueError("Unsupported ThreadStoreOperation schema version")
        key = (system, thread)
        digest = hashlib.sha256(
            _encode(asdict(operation)).encode()
        ).hexdigest()
        previous = db.execute(
            "SELECT digest,receipt FROM durable_receipts WHERE system=? AND thread=? AND operation=?",
            (*key, operation.operation_id),
        ).fetchone()
        if previous:
            if previous["digest"] != digest:
                raise ThreadStoreOperationMismatch(
                    "Operation identity reused with different input"
                )
            return ThreadStoreReceipt(**json.loads(previous["receipt"]))
        row = self._row(db, *key)
        control = self._control(db, *key)
        if operation.generation != control["generation"]:
            raise ThreadStoreOwnershipError("Thread generation changed")
        kind = operation.kind
        if kind not in ("claim_owner", "update_tui") and (
            operation.owner_id != row["owner"]
            or (
                operation.owner_epoch is not None
                and operation.owner_epoch != row["epoch"]
            )
        ):
            raise ThreadStoreOwnershipError("Thread writer ownership changed")
        if (
            operation.expected_revision is not None
            and operation.expected_revision != control["revision"]
        ):
            raise ThreadStoreConflict("Thread revision changed")
        out = _OperationResult(meta=json.loads(row["meta"]))
        self._apply_durable(db, key, copy.deepcopy(operation), row, out)
        if out.stamp:
            out.meta = self.stamp_meta(out.meta)
        maximum = db.execute(
            _SELECT_MAX_SEQ,
            key,
        ).fetchone()[0]
        db.execute(
            "UPDATE threads SET meta=?,max_seq=? WHERE system=? AND thread=?",
            (_encode(out.meta), maximum, *key),
        )
        self._advance_revision(db, *key)
        row, control = (self._row(db, *key), self._control(db, *key))
        receipt = ThreadStoreReceipt(
            operation.operation_id,
            kind,
            digest,
            control["revision"],
            control["generation"],
            result=out.result,
            written=out.written,
            max_seq=maximum,
            added_known_keys=out.added,
            meta=out.meta,
            archive=out.archive,
            owner_id=row["owner"],
            owner_epoch=row["epoch"],
        )
        db.execute(
            "INSERT INTO durable_receipts VALUES (?,?,?,?,?)",
            (*key, operation.operation_id, digest, _encode(asdict(receipt))),
        )
        return receipt

    def _apply_durable(self, db, key, operation, row, out):
        handlers = {
            "persist": self._durable_append,
            "append_messages": self._durable_append,
            "compact": self._durable_replace,
            "archive_and_replace": self._durable_replace,
            "replace_messages": self._durable_replace,
            "save_meta": self._durable_save_meta,
            "patch_runtime_flag": self._durable_patch_runtime_flag,
            "reset_run_flags": self._durable_reset_run_flags,
            "update_runtime_meta": self._durable_update_runtime_meta,
            "update_tui": self._durable_update_tui,
            "discard_message": self._durable_discard_message,
            "reset": self._durable_reset,
            "append_subagent_trace": self._durable_append_subagent_trace,
            "claim_owner": self._durable_claim_owner,
            "release_owner": self._durable_release_owner,
        }
        method = handlers.get(operation.kind)
        if method is None:
            raise ValueError(
                f"Unsupported durable operation: {operation.kind}"
            )
        method(db, key, operation, row, out)

    def _durable_append(self, db, key, operation, row, out):
        payload = operation.payload
        kind = operation.kind
        messages = payload["messages"]
        known = payload.get("known_keys")
        if known is not None:
            known = set(known)
            messages = [
                message
                for message in messages
                if make_message_key(message) not in known
            ]
        maximum = db.execute(
            _SELECT_MAX_SEQ,
            key,
        ).fetchone()[0]
        _, out.added = self._append(
            db, *key, messages, max(payload.get("start_seq", 0), maximum)
        )
        out.written = len(out.added)
        if (
            "expected_written" in payload
            and payload["expected_written"] != out.written
        ):
            raise ThreadStoreConflict("Prepared message count changed")
        self._after_messages(db)
        if kind == "persist":
            out.meta, out.stamp = (payload["meta"], True)
        out.result = out.written

    def _durable_replace(self, db, key, operation, row, out):
        payload = operation.payload
        kind = operation.kind
        if kind != "replace_messages":
            suffix = payload.get("archive_suffix") or uuid.uuid4().hex
            state = self._state(db, *key)
            db.execute(
                "INSERT INTO thread_archives(system,thread,suffix,messages,meta) VALUES (?,?,?,?,?)",
                (*key, suffix, _encode(state.messages), _encode(out.meta)),
            )
            out.archive = {
                "archive_suffix": suffix,
                "archive_data_path": None,
                "archive_meta_path": None,
            }
        db.execute("DELETE FROM messages WHERE system=? AND thread=?", key)
        _, out.added = self._append(db, *key, payload["messages"], 0)
        out.written = len(out.added)
        self._after_messages(db)
        if "meta" in payload:
            out.meta, out.stamp = (payload["meta"], True)
        out.result = out.archive if kind != "replace_messages" else None

    def _durable_save_meta(self, db, key, operation, row, out):
        payload = operation.payload
        out.meta, out.stamp = (payload["meta"], True)

    def _durable_patch_runtime_flag(self, db, key, operation, row, out):
        payload = operation.payload
        out.meta = self.apply_runtime_flag(
            out.meta, payload["flag"], active=payload["active"]
        )
        out.stamp = True

    def _durable_reset_run_flags(self, db, key, operation, row, out):
        payload = operation.payload
        from gc_core.persistence.meta_ownership import clear_run_flags

        clear_run_flags(
            out.meta, stale_summarizing=payload.get("stale_summarizing", False)
        )
        out.stamp = True

    def _durable_update_runtime_meta(self, db, key, operation, row, out):
        payload = operation.payload
        out.meta = self.merge_runtime_meta(out.meta, payload["patch"])
        out.stamp = True

    def _durable_update_tui(self, db, key, operation, row, out):
        payload = operation.payload
        out.meta.setdefault("tui", {}).update(payload["patch"])
        out.stamp = True

    def _durable_discard_message(self, db, key, operation, row, out):
        payload = operation.payload
        removed = db.execute(
            "DELETE FROM messages WHERE system=? AND thread=? AND json_extract(payload,'$.id')=?",
            (*key, payload["message_id"]),
        ).rowcount
        out.result = bool(removed)
        if removed:
            try:
                out.meta["messages_count"] = max(
                    0, int(out.meta.get("messages_count", 0)) - removed
                )
            except (TypeError, ValueError):
                pass
            merge_tui_into_meta(
                out.meta,
                live_volume_from_messages(self._state(db, *key).messages),
            )
            out.stamp = True

    def _durable_reset(self, db, key, operation, row, out):
        out.result = []
        for table in ("messages", "traces", "events", "imports", "ui_meta"):
            if db.execute(
                f"DELETE FROM {table} WHERE system=? AND thread=?", key
            ).rowcount:
                out.result.append(table)
        out.result.append("threads")
        out.meta = {}
        db.execute(
            "UPDATE threads SET owner=NULL,epoch=epoch+1,sequence=0,max_seq=0 WHERE system=? AND thread=?",
            key,
        )
        db.execute(
            "UPDATE durable_control SET generation=generation+1 WHERE system=? AND thread=?",
            key,
        )

    def _durable_append_subagent_trace(self, db, key, operation, row, out):
        payload = operation.payload
        seq = db.execute(
            "SELECT coalesce(max(seq),0)+1 FROM traces WHERE system=? AND thread=?",
            key,
        ).fetchone()[0]
        out.result = dict(
            ts=datetime.now(UTC).isoformat(),
            seq=seq,
            parent_call_id=payload["parent_call_id"],
            child_run_id=payload["child_run_id"],
            kind=payload["kind"],
            payload=payload["payload"],
        )
        db.execute(
            "INSERT INTO traces VALUES (?,?,?,?)",
            (*key, seq, _encode(out.result)),
        )

    def _durable_claim_owner(self, db, key, operation, row, out):
        payload = operation.payload
        owner = payload.get("owner_id", operation.owner_id)
        if not owner or row["owner"] not in (None, owner):
            raise ThreadStoreOwnershipError(
                "Thread already has a different owner"
            )
        if row["owner"] != owner:
            db.execute(
                "UPDATE threads SET owner=?,epoch=epoch+1,sequence=0 WHERE system=? AND thread=?",
                (owner, *key),
            )

    def _durable_release_owner(self, db, key, operation, row, out):
        db.execute(
            "UPDATE threads SET owner=NULL,epoch=epoch+1 WHERE system=? AND thread=?",
            key,
        )
