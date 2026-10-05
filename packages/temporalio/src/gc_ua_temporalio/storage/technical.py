"""Техническая SQLite worker'а: квитанции результатов activities и preview-события.

Это не хранилище диалога. Тред живёт в выбранном ThreadStore (файлы, SQLite,
своя БД); здесь — только то, что нужно исполнителю: результат завершённой
operation, чтобы повтор activity не платил второй раз, и необязательные
preview для потокового вывода.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from gc_ua_temporalio.storage.sqlite import ProjectionConflict, encode


class TechnicalStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    owner TEXT, operation TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY(owner, operation));
                CREATE TABLE IF NOT EXISTS view_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                    operation TEXT NOT NULL, attempt INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(owner,operation,attempt,sequence));
                CREATE INDEX IF NOT EXISTS view_events_owner ON view_events(owner,cursor);
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

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

    def owners(self):
        """Владельцы, у которых есть квитанции или preview."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT owner FROM operations UNION SELECT owner FROM view_events"
            ).fetchall()
        return [row[0] for row in rows]

    def delete_owner(self, owner):
        """Квитанции и preview владельца, чей workflow Temporal уже удалил."""
        with self.transaction() as db:
            db.execute("DELETE FROM operations WHERE owner=?", (owner,))
            db.execute("DELETE FROM view_events WHERE owner=?", (owner,))

    def save_operation_result(self, owner, operation, digest, result):
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?,?,?,?)",
                (owner, operation, digest, encode(result)),
            )
        return self.operation_result(owner, operation, digest)


__all__ = ["TechnicalStore"]
