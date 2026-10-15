"""Строка ``gc_threads`` в памяти: чтение из записи драйвера, правка внутри
транзакции, обратно — одним ``UPDATE``.

JSON пишется компактно и без экранирования кириллицы в колонки ``text`` (см.
``schema.sql``): прочитанный текст — ровно записанный, включая ``NaN`` и
``Infinity``, которые ``json.dumps`` пишет, а ``json.loads`` читает, как у
файлового хранилища.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gc_core.threads.models import (
    ArchiveInfo,
    JsonObject,
    Lease,
    Receipt,
    RuntimeStatus,
    SessionState,
    StreamEntry,
    ThreadInfo,
    ThreadKey,
    ThreadRecord,
    Usage,
)
from pydantic import BaseModel

from gc_stores.postgres.queries import THREAD_COLUMNS

HISTORY = "messages"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def stamp(moment: datetime) -> str:
    """Время для моделей — ISO-8601 со смещением (контракт, «Время»)."""
    return moment.isoformat()


def _model(model: BaseModel) -> str:
    """Только явно заданные поля: по ``model_fields_set`` фасад строит meta
    (ключ есть ⇔ поле задано), и после чтения набор обязан быть тем же."""
    return dumps(model.model_dump(mode="json", exclude_unset=True))


def entry_of(row: Any) -> StreamEntry:
    return StreamEntry(
        seq=row["seq"], ts=stamp(row["ts"]), data=json.loads(row["data"])
    )


def id_key(message_id: str) -> str:
    """Ключ id сообщения в базе — sha256: id приходит извне (NUL, длина
    больше строки индекса), а сравнивается только на равенство."""
    return hashlib.sha256(
        message_id.encode("utf-8", "surrogatepass")
    ).hexdigest()


def message_key(data: JsonObject) -> str | None:
    """Ключ ``data["id"]`` для ``discard_message_ids``: сверка — со
    строками, поэтому не строковый id ключа не получает."""
    value = data.get("id")
    return id_key(value) if isinstance(value, str) else None


@dataclass
class ThreadRow:
    key: ThreadKey
    created_at: datetime
    updated_at: datetime
    info: ThreadInfo = field(default_factory=ThreadInfo)
    state: SessionState = field(default_factory=SessionState)
    usage: Usage = field(default_factory=Usage)
    runtime: RuntimeStatus = field(default_factory=RuntimeStatus)
    frontend: dict[str, JsonObject] = field(default_factory=dict)
    archives: list[ArchiveInfo] = field(default_factory=list)
    extra: JsonObject = field(default_factory=dict)
    max_seq: dict[str, int] = field(default_factory=dict)
    messages_count: int = 0
    revision: int = 0
    generation: int = 0
    history_version: int = 0
    lease_owner: str | None = None
    lease_epoch: int = 0
    lease_expires_at: datetime | None = None

    @classmethod
    def from_db(cls, row: Any) -> ThreadRow:
        return cls(
            key=ThreadKey(system=row["system"], thread=row["thread_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            info=ThreadInfo.model_validate_json(row["info"]),
            state=SessionState.model_validate_json(row["state"]),
            usage=Usage.model_validate_json(row["usage"]),
            runtime=RuntimeStatus.model_validate_json(row["runtime"]),
            frontend=json.loads(row["frontend"]),
            archives=[
                ArchiveInfo.model_validate(a)
                for a in json.loads(row["archives"])
            ],
            extra=json.loads(row["extra"]),
            max_seq=json.loads(row["max_seq"]),
            messages_count=row["messages_count"],
            revision=row["revision"],
            generation=row["generation"],
            history_version=row["history_version"],
            lease_owner=row["lease_owner"],
            lease_epoch=row["lease_epoch"],
            lease_expires_at=row["lease_expires_at"],
        )

    def values(
        self, columns: tuple[str, ...] = THREAD_COLUMNS
    ) -> tuple[Any, ...]:
        """Значения ``(system, thread_id, *columns)`` для запроса; документы
        кодируются только для запрошенных колонок."""
        return (
            self.key.system,
            self.key.thread,
            *(_ENCODERS.get(name, _attribute(name))(self) for name in columns),
        )

    @property
    def lease(self) -> Lease | None:
        if self.lease_owner is None:
            return None
        return Lease(
            owner=self.lease_owner,
            epoch=self.lease_epoch,
            expires_at=self.lease_expires_at,
        )

    def grant(self, lease: Lease) -> None:
        self.lease_owner = lease.owner
        self.lease_epoch = lease.epoch
        self.lease_expires_at = lease.expires_at

    def drop_lease(self) -> None:
        self.lease_owner = None
        self.lease_expires_at = None

    def stream_high(self) -> dict[str, int]:
        return {name: seq for name, seq in self.max_seq.items() if seq}

    def record(self) -> ThreadRecord:
        info = self.info.model_copy(
            update={
                "created_at": stamp(self.created_at),
                "updated_at": stamp(self.updated_at),
                "messages_count": self.messages_count,
            }
        )
        return ThreadRecord(
            key=self.key,
            info=info,
            state=self.state,
            usage=self.usage,
            runtime=self.runtime,
            frontend=self.frontend,
            archives=list(self.archives),
            revision=self.revision,
            generation=self.generation,
            history_version=self.history_version,
            max_seq=self.stream_high(),
            lease=self.lease,
            extra=self.extra,
        )

    def receipt(self, op_id: str, archive_id: str | None = None) -> Receipt:
        return Receipt(
            op_id=op_id,
            revision=self.revision,
            generation=self.generation,
            history_version=self.history_version,
            max_seq=self.stream_high(),
            archive_id=archive_id,
        )


def _attribute(name: str) -> Callable[[ThreadRow], Any]:
    return lambda row: getattr(row, name)


#: Колонки-документы строки треда: как их значение попадает в базу.
_ENCODERS: dict[str, Callable[[ThreadRow], Any]] = {
    "workspace": lambda row: row.info.workspace,
    "info": lambda row: _model(row.info),
    "state": lambda row: _model(row.state),
    "usage": lambda row: _model(row.usage),
    "runtime": lambda row: _model(row.runtime),
    "frontend": lambda row: dumps(row.frontend),
    "archives": lambda row: dumps(
        [a.model_dump(mode="json") for a in row.archives]
    ),
    "extra": lambda row: dumps(row.extra),
    "max_seq": lambda row: dumps(row.max_seq),
}
