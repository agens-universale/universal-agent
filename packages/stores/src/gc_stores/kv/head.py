"""Заголовок треда в KV: строка треда (как у SQL) и то, что заменяет
таблицы — куски потоков, архивы, алиасы, неотписанные квитанции, ключ
индекса. Условная запись заголовка — точка фиксации любой записи в тред.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from gc_core.threads.models import ThreadKey

from gc_stores.rows import THREAD_COLUMNS, ThreadRow

_TIMES = ("created_at", "updated_at", "lease_expires_at")


@dataclass
class Chunk:
    """Неизменяемый кусок потока: записи с ``first`` по ``last`` (после
    удаления сообщений их может быть меньше — ``count``)."""

    id: str
    first: int
    last: int
    count: int


@dataclass
class Head:
    row: ThreadRow
    streams: dict[str, list[Chunk]] = field(default_factory=dict)
    archive_chunks: dict[str, list[Chunk]] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    #: Квитанции записей, ещё не вынесенные в свои объекты:
    #: ``{op_id, digest, receipt, at}``.
    pending: list[dict[str, Any]] = field(default_factory=list)
    index: str | None = None
    #: Надгробие: тред удалён; ``row.generation`` и ``row.lease_epoch`` —
    #: для треда, созданного заново под тем же ключом.
    deleted: bool = False

    @classmethod
    def fresh(
        cls, key: ThreadKey, now: datetime, tomb: Head | None = None
    ) -> Head:
        row = ThreadRow(key=key, created_at=now, updated_at=now)
        if tomb is not None:
            row.generation = tomb.row.generation
            row.lease_epoch = tomb.row.lease_epoch
        return cls(row=row)

    def tombstone(self) -> Head:
        row = ThreadRow(
            key=self.row.key,
            created_at=self.row.created_at,
            updated_at=self.row.updated_at,
        )
        row.generation = self.row.generation + 1
        row.lease_epoch = self.row.lease_epoch
        return Head(row=row, deleted=True)

    def copy(self) -> Head:
        return copy.deepcopy(self)

    def chunks(self, stream: str) -> list[Chunk]:
        return self.streams.setdefault(stream, [])

    def dumps(self) -> bytes:
        values = dict(
            zip(
                ("system", "thread_id", *THREAD_COLUMNS),
                self.row.values(),
                strict=True,
            )
        )
        for name in _TIMES:
            if values[name] is not None:
                values[name] = values[name].isoformat()
        document = {
            "row": values,
            "streams": {
                name: [asdict(c) for c in chunks]
                for name, chunks in self.streams.items()
            },
            "archive_chunks": {
                name: [asdict(c) for c in chunks]
                for name, chunks in self.archive_chunks.items()
            },
            "aliases": self.aliases,
            "pending": self.pending,
            "index": self.index,
            "deleted": self.deleted,
        }
        return json.dumps(
            document, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def loads(cls, data: bytes) -> Head:
        document = json.loads(data)
        values = document["row"]
        for name in _TIMES:
            if values[name] is not None:
                values[name] = datetime.fromisoformat(values[name])
        return cls(
            row=ThreadRow.from_db(values),
            streams={
                name: [Chunk(**c) for c in chunks]
                for name, chunks in document["streams"].items()
            },
            archive_chunks={
                name: [Chunk(**c) for c in chunks]
                for name, chunks in document["archive_chunks"].items()
            },
            aliases=list(document["aliases"]),
            pending=list(document["pending"]),
            index=document["index"],
            deleted=bool(document["deleted"]),
        )
