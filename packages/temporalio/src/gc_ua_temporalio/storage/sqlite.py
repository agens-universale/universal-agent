"""SQLite ThreadStore with durable, fenced operations.

One connection per transaction: no worker-local mutable connection or ledger.
A Temporal-owned thread rejects plain ThreadStore mutations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from gc_core.execution.contracts import ExecutionFailure
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


def _private_file(path):
    """Создать файл базы с правами 0600: в ней история и квитанции агента.

    SQLite создаёт ``-wal``/``-shm`` с правами файла базы.
    """
    if not Path(path).exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


class ProjectionConflict(ExecutionFailure):
    """Another owner or a reused operation ID with new content."""


class SQLiteThreadStore(SQLiteDurableOperations, ThreadStoreBase):
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.ensure_ready()

    @contextmanager
    def transaction(self, *, readonly=False):
        """Транзакция; ``readonly`` — чтение без блокировки записи (WAL)."""
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_ready(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        _private_file(self.path)
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
                CREATE TABLE IF NOT EXISTS durable_control (
                    system TEXT, thread TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(system,thread));
                CREATE TABLE IF NOT EXISTS durable_receipts (
                    system TEXT, thread TEXT, operation TEXT, digest TEXT, receipt TEXT,
                    PRIMARY KEY(system,thread,operation));
                CREATE TABLE IF NOT EXISTS thread_archives (
                    system TEXT, thread TEXT, suffix TEXT, messages TEXT, meta TEXT,
                    PRIMARY KEY(system,thread,suffix));
            """)
            columns = {
                row[1]
                for row in db.execute("PRAGMA table_info(durable_receipts)")
            }
            if "created" not in columns:
                # Время квитанции — для очистки (prune_receipts).
                db.execute(
                    "ALTER TABLE durable_receipts ADD COLUMN created REAL"
                )

    def prune_receipts(self, *, max_age_seconds):
        """Удалить квитанции операций старше срока; нужны они только окну
        повторов activity (минуты). Квитанции без времени (до этой версии)
        остаются."""
        with self.transaction() as db:
            return db.execute(
                "DELETE FROM durable_receipts WHERE created < ?",
                (time.time() - max_age_seconds,),
            ).rowcount

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

    def _after_messages(self, _db):
        """Fault-injection seam, inside the transaction before meta and ack."""

    def load_messages(self, system_id, thread_id):
        return self.load_thread_state(system_id, thread_id).messages

    def load_thread_state(self, system_id, thread_id):
        with self.transaction(readonly=True) as db:
            return self._state(db, system_id, thread_id)

    def load_meta(self, system_id, thread_id):
        with self.transaction(readonly=True) as db:
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
        with self.transaction(readonly=True) as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM traces WHERE system=? AND thread=? ORDER BY seq",
                    (system_id, thread_id),
                )
            ]
