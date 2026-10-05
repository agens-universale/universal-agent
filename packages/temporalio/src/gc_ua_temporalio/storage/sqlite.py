"""SQLite ThreadStore with durable, fenced operations.

One connection per transaction: no worker-local mutable connection or ledger.
A Temporal-owned thread rejects plain ThreadStore mutations.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.message_keys import make_message_key
from gc_core.utils.generate_message_id import generate_msg_id_sha256

from gc_ua_temporalio.storage.durable_api import DurableThreadStoreAPI
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
    """Another owner or a reused operation ID with new content."""


class SQLiteThreadStore(SQLiteDurableOperations, DurableThreadStoreAPI):
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

    def read_subagent_trace(self, system_id, thread_id):
        with self.transaction() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM traces WHERE system=? AND thread=? ORDER BY seq",
                    (system_id, thread_id),
                )
            ]
