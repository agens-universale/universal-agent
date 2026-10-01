"""SQLite ThreadStore with atomic, fenced projection commits.

One connection per transaction: no worker-local mutable connection or ledger.
A Temporal-owned thread rejects all legacy mutation paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from gc_core.execution.contracts import (
    ExecutionFailure,
    ProjectionAck,
    ProjectionBatch,
)
from gc_core.execution.message_keys import make_message_key
from gc_core.thread_store_base import ThreadStoreBase
from gc_core.utils.generate_message_id import generate_msg_id_sha256

from gc_ua_temporalio.storage.sqlite_durable import SQLiteDurableOperations


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class ProjectionConflict(ExecutionFailure):
    """Another owner, stale sequence, or reused operation ID with new content."""


class SQLiteThreadStore(SQLiteDurableOperations, ThreadStoreBase):
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.ensure_ready()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_ready(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    system TEXT, thread TEXT, owner TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                    sequence INTEGER NOT NULL DEFAULT 0, max_seq INTEGER NOT NULL DEFAULT 0,
                    meta TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(system, thread));
                CREATE TABLE IF NOT EXISTS messages (
                    system TEXT, thread TEXT, seq INTEGER, key TEXT, payload TEXT,
                    PRIMARY KEY(system, thread, key), UNIQUE(system, thread, seq));
                CREATE TABLE IF NOT EXISTS traces (
                    system TEXT, thread TEXT, seq INTEGER, payload TEXT,
                    PRIMARY KEY(system, thread, seq));
                CREATE TABLE IF NOT EXISTS events (
                    system TEXT, thread TEXT, epoch INTEGER, event TEXT, digest TEXT, ack TEXT,
                    PRIMARY KEY(system, thread, epoch, event));
                CREATE TABLE IF NOT EXISTS imports (
                    system TEXT, thread TEXT, digest TEXT, PRIMARY KEY(system,thread));
                CREATE TABLE IF NOT EXISTS operations (
                    owner TEXT, operation TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY(owner, operation));
                CREATE TABLE IF NOT EXISTS view_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                    operation TEXT NOT NULL, attempt INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(owner,operation,attempt,sequence));
                CREATE INDEX IF NOT EXISTS view_events_owner ON view_events(owner,cursor);
                CREATE TABLE IF NOT EXISTS durable_control (
                    system TEXT, thread TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(system,thread));
                CREATE TABLE IF NOT EXISTS durable_receipts (
                    system TEXT, thread TEXT, operation TEXT, digest TEXT, receipt TEXT,
                    PRIMARY KEY(system,thread,operation));
                CREATE TABLE IF NOT EXISTS thread_archives (
                    system TEXT, thread TEXT, suffix TEXT, messages TEXT, meta TEXT,
                    PRIMARY KEY(system,thread,suffix));
                CREATE TABLE IF NOT EXISTS ui_meta (
                    system TEXT, thread TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(system,thread));
            """)

    @staticmethod
    def _row(db, system, thread):
        db.execute(
            "INSERT OR IGNORE INTO threads(system,thread) VALUES (?,?)",
            (system, thread),
        )
        return db.execute(
            "SELECT * FROM threads WHERE system=? AND thread=?",
            (system, thread),
        ).fetchone()

    @staticmethod
    def _local(row):
        if row["owner"] is not None:
            raise ProjectionConflict(
                "Temporal owns this thread; local writes are forbidden"
            )

    def claim(self, system, thread, owner):
        if not owner:
            raise ValueError("Owner must be nonempty")
        with self.transaction() as db:
            row = self._row(db, system, thread)
            if row["owner"] == owner:
                return row["epoch"]
            if row["owner"] is not None:
                raise ProjectionConflict("Thread already has an owner")
            epoch = row["epoch"] + 1
            db.execute(
                "UPDATE threads SET owner=?,epoch=?,sequence=0 WHERE system=? AND thread=?",
                (owner, epoch, system, thread),
            )
            self._advance_revision(db, system, thread)
            return epoch

    @staticmethod
    def _append(db, system, thread, messages, start_seq):
        seq, keys = start_seq, []
        for message in messages:
            key = make_message_key(message)
            exists = db.execute(
                "SELECT 1 FROM messages WHERE system=? AND thread=? AND key=?",
                (system, thread, key),
            ).fetchone()
            if exists:
                continue
            stored = dict(message)
            if not stored.get("id"):
                stored["id"] = generate_msg_id_sha256("m")
            seq += 1
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                (system, thread, seq, key, encode(stored)),
            )
            keys.append(key)
        db.execute(
            "UPDATE threads SET max_seq=? WHERE system=? AND thread=?",
            (seq, system, thread),
        )
        return seq, keys

    def commit_batch(self, batch: ProjectionBatch) -> ProjectionAck:
        if batch.schema_version != 1:
            raise ProjectionConflict("Unsupported ProjectionBatch version")
        digest = hashlib.sha256(encode(asdict(batch)).encode()).hexdigest()
        key = (batch.system_id, batch.thread_id)
        with self.transaction() as db:
            row = self._row(db, *key)
            if (row["owner"], row["epoch"]) != (batch.owner, batch.epoch):
                raise ProjectionConflict("Projection owner/epoch mismatch")
            receipt = db.execute(
                "SELECT digest,ack FROM events WHERE system=? AND thread=? AND epoch=? AND event=?",
                (*key, batch.epoch, batch.event_id),
            ).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise ProjectionConflict(
                        "Event ID reused with different content"
                    )
                return ProjectionAck(**json.loads(receipt["ack"]))
            if batch.sequence != row["sequence"] + 1:
                raise ProjectionConflict(
                    "Stale or out-of-order projection sequence"
                )
            max_seq, keys = self._append(
                db, *key, batch.messages, row["max_seq"]
            )
            meta = self.stamp_meta(
                {
                    **json.loads(row["meta"]),
                    **batch.meta,
                    "messages_count": max_seq,
                }
            )
            self._after_messages(db)
            db.execute(
                "UPDATE threads SET meta=?,sequence=? WHERE system=? AND thread=?",
                (encode(meta), batch.sequence, *key),
            )
            ack = ProjectionAck(
                batch.event_id, batch.sequence, max_seq, len(keys)
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (
                    *key,
                    batch.epoch,
                    batch.event_id,
                    digest,
                    encode(asdict(ack)),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO view_events(owner,operation,attempt,sequence,kind,payload) VALUES(?,?,0,0,'projection',?)",
                (
                    batch.owner,
                    batch.event_id,
                    encode({"max_seq": max_seq, "sequence": batch.sequence}),
                ),
            )
            self._advance_revision(db, *key)
            return ack

    def _after_messages(self, _db):
        """Fault-injection seam, inside the transaction before meta and ack."""

    def load_messages(self, system_id, thread_id):
        return self.load_thread_state(system_id, thread_id).messages

    def load_thread_state(self, system_id, thread_id):
        with self.transaction() as db:
            return self._state(db, system_id, thread_id)

    def load_meta(self, system_id, thread_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT meta FROM threads WHERE system=? AND thread=?",
                (system_id, thread_id),
            ).fetchone()
            return json.loads(row["meta"]) if row else {}

    def append_messages(
        self, system_id, thread_id, messages, *, known_keys=None, start_seq=0
    ):
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "append_messages",
            {
                "messages": list(messages),
                "known_keys": sorted(known_keys)
                if known_keys is not None
                else None,
                "start_seq": start_seq,
            },
        )
        if known_keys is not None:
            known_keys.update(receipt.added_known_keys)
        return receipt.written

    def save_meta(self, system_id, thread_id, meta):
        self._legacy_commit(system_id, thread_id, "save_meta", {"meta": meta})

    def patch_runtime_flag(
        self, system_id, thread_id, flag, *, active, session_meta=None
    ):
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "patch_runtime_flag",
            {"flag": flag, "active": active},
        )
        if session_meta is not None:
            from copy import deepcopy

            for key in ("runtime", "tui"):
                if key in receipt.meta:
                    session_meta[key] = deepcopy(receipt.meta[key])
                else:
                    session_meta.pop(key, None)

    def reset_run_flags(self, system_id, thread_id, *, session_meta=None):
        runtime = (session_meta or {}).get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "reset_run_flags",
            {"stale_summarizing": bool(runtime.get("summarizing"))},
        )
        if session_meta is not None:
            from copy import deepcopy

            for section in ("runtime", "tui"):
                if section in receipt.meta:
                    session_meta[section] = deepcopy(receipt.meta[section])
                else:
                    session_meta.pop(section, None)

    def update_runtime_meta(self, system_id, thread_id, patch):
        self._legacy_commit(
            system_id, thread_id, "update_runtime_meta", {"patch": patch}
        )

    def discard_message(self, system_id, thread_id, message_id):
        return self._legacy_commit(
            system_id, thread_id, "discard_message", {"message_id": message_id}
        ).result

    def reset(self, system_id, thread_id):
        return self._legacy_commit(system_id, thread_id, "reset", {}).result

    def replace_messages(self, system_id, thread_id, messages):
        self._legacy_commit(
            system_id,
            thread_id,
            "replace_messages",
            {"messages": list(messages)},
        )

    def archive_and_replace(self, system_id, thread_id, messages):
        return self._legacy_commit(
            system_id,
            thread_id,
            "archive_and_replace",
            {"messages": list(messages)},
        ).archive

    def append_subagent_trace(
        self,
        system_id,
        thread_id,
        *,
        parent_call_id,
        child_run_id,
        kind,
        payload,
    ):
        return self._legacy_commit(
            system_id,
            thread_id,
            "append_subagent_trace",
            {
                "parent_call_id": parent_call_id,
                "child_run_id": child_run_id,
                "kind": kind,
                "payload": payload,
            },
        ).result

    def read_subagent_trace(self, system_id, thread_id):
        with self.transaction() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM traces WHERE system=? AND thread=? ORDER BY seq",
                    (system_id, thread_id),
                )
            ]

    def operation_result(self, owner, operation, digest):
        with self.transaction() as db:
            row = db.execute(
                "SELECT digest,result FROM operations WHERE owner=? AND operation=?",
                (owner, operation),
            ).fetchone()
            if row is None:
                return None
            if row["digest"] != digest:
                raise ProjectionConflict(
                    "Operation ID reused with different content"
                )
            return json.loads(row["result"])

    def save_operation_result(self, owner, operation, digest, result):
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?,?,?,?)",
                (owner, operation, digest, encode(result)),
            )
        return self.operation_result(owner, operation, digest)

    def import_snapshot(self, system, thread, digest, messages, meta, traces):
        with self.transaction() as db:
            previous = db.execute(
                "SELECT digest FROM imports WHERE system=? AND thread=?",
                (system, thread),
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise ProjectionConflict("Imported source changed")
                return
            row = self._row(db, system, thread)
            self._local(row)
            if row["max_seq"] or json.loads(row["meta"]):
                raise ProjectionConflict("Import target is not empty")
            max_seq, _ = self._append(db, system, thread, messages, 0)
            stamped = self.stamp_meta(
                {**meta, "messages_count": max_seq, "temporal_import": digest}
            )
            db.execute(
                "UPDATE threads SET meta=? WHERE system=? AND thread=?",
                (encode(stamped), system, thread),
            )
            for record in traces:
                db.execute(
                    "INSERT INTO traces VALUES (?,?,?,?)",
                    (system, thread, record["seq"], encode(record)),
                )
            db.execute(
                "INSERT INTO imports VALUES (?,?,?)", (system, thread, digest)
            )
            self._advance_revision(db, system, thread)
