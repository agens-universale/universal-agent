"""Ответы Responses — в тредах ``system=responses`` хранилища тредов.

Каждое сохранение ответа (начало хода и его итог) — запись потока
``chat.responses`` треда, id ответа — алиас треда, сводка последнего ответа —
``frontend["chat"]``: по ней список диалогов строится без чтения потоков.
Запись потока, алиас и сводка — одна запись хранилища.

Ответы develop лежали файлами ``responses-index/<id>.json`` рядом с каталогом
тредов. Они читаются, когда ответа нет в хранилище, и пишутся вместе с ним:
откат на develop видит ответы ветки (``previous_response_id``, список
диалогов). Двойная запись — до следующего релиза.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

from gc_core.thread_store import validate_thread_id
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import JsonObject, ThreadKey, ThreadRecord
from gc_core.utils.atomic_io import atomic_replace

SYSTEM = "responses"
_STREAM = "chat.responses"
_FRONTEND = "chat"
_LEGACY_DIR = "responses-index"
_JSON = ".json"
_RESPONSE_ID = re.compile(
    r"^resp_[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class ResponseRecord:
    response_id: str
    thread_id: str
    cwd: str
    previous_response_id: str | None
    created_at: int
    status: str
    response: dict


def thread_key(thread_id: str) -> ThreadKey:
    """Тред диалога. Битый id — ``ValueError``."""
    return ThreadKey(system=SYSTEM, thread=validate_thread_id(thread_id))


class ResponseIndex:
    """``legacy_dir`` — каталог ``responses-index`` develop."""

    def __init__(
        self, threads: Threads, *, legacy_dir: str | None = None
    ) -> None:
        self.threads = threads
        self._legacy_dir = legacy_dir

    @classmethod
    def for_threads_dir(cls, threads_dir: str) -> ResponseIndex:
        parent = os.path.dirname(os.path.abspath(threads_dir))
        return cls(
            Threads(FileThreadStore(threads_dir)),
            legacy_dir=os.path.join(parent, _LEGACY_DIR),
        )

    async def save(self, record: ResponseRecord) -> None:
        key = thread_key(record.thread_id)
        summary = {
            "latest_response_id": record.response_id,
            "cwd": os.path.normpath(record.cwd),
            "created_at": await self._dialog_created(key, record),
            "updated_at": record.created_at,
            "status": record.status,
        }
        await self.threads.append_frontend(
            key,
            _STREAM,
            [{"response_id": record.response_id, **_payload(record)}],
            aliases=[record.response_id],
            patch=(_FRONTEND, summary),
        )
        self._write_legacy(record)

    async def load(self, response_id: str) -> ResponseRecord | None:
        if _RESPONSE_ID.match(response_id) is None:
            return None
        key = await self.threads.resolve(response_id)
        if key is not None:
            entries = await self.threads.read_frontend(key, _STREAM)
            for entry in reversed(entries):
                if entry.data.get("response_id") == response_id:
                    return _record(entry.data, response_id)
        return self._legacy(response_id)

    async def thread_records(self, thread_id: str) -> list[ResponseRecord]:
        """Все ответы диалога: develop и v2 (у одного id побеждает v2)."""
        key = thread_key(thread_id)
        found = {
            record.response_id: record
            for record in self._legacy_all()
            if record.thread_id == key.thread
        }
        for entry in await self.threads.read_frontend(key, _STREAM):
            response_id = entry.data.get("response_id")
            if not isinstance(response_id, str):
                continue
            record = _record(entry.data, response_id)
            if record is not None:
                found[response_id] = record
        return list(found.values())

    @staticmethod
    def summary(thread: ThreadRecord) -> JsonObject | None:
        """Сводка последнего ответа диалога v2; у диалога develop — нет."""
        summary = thread.frontend.get(_FRONTEND)
        return summary if isinstance(summary, dict) else None

    def legacy_dialogs(self) -> dict[str, list[ResponseRecord]]:
        """Ответы develop по тредам."""
        grouped: dict[str, list[ResponseRecord]] = {}
        for record in self._legacy_all():
            grouped.setdefault(record.thread_id, []).append(record)
        return grouped

    async def _dialog_created(
        self, key: ThreadKey, record: ResponseRecord
    ) -> int:
        """Время первого ответа диалога: из сводки, а без неё — по цепочке
        (диалог мог начаться в develop)."""
        thread = await self.threads.get(key)
        summary = None if thread is None else self.summary(thread)
        if summary is not None and isinstance(summary.get("created_at"), int):
            return summary["created_at"]
        if record.previous_response_id is None:
            return record.created_at
        earlier = [
            item for item in self._legacy_all() if item.thread_id == key.thread
        ]
        return chain([*earlier, record])[0].created_at

    def _write_legacy(self, record: ResponseRecord) -> None:
        """Файл ответа develop — как его писал develop (tmp + замена)."""
        if self._legacy_dir is None:
            return
        os.makedirs(self._legacy_dir, exist_ok=True)
        path = os.path.join(self._legacy_dir, record.response_id + _JSON)
        fd, temporary = tempfile.mkstemp(
            dir=self._legacy_dir, prefix=".resp-", suffix=_JSON
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_payload(record), handle, ensure_ascii=False)
                handle.write("\n")
            atomic_replace(temporary, path)
        except Exception:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise

    def _legacy(self, response_id: str) -> ResponseRecord | None:
        if self._legacy_dir is None:
            return None
        path = os.path.join(self._legacy_dir, response_id + _JSON)
        return _read(path, response_id)

    def _legacy_all(self) -> list[ResponseRecord]:
        """Все целые ответы develop. Битый файл и чужое имя пропускаются."""
        if self._legacy_dir is None or not os.path.isdir(self._legacy_dir):
            return []
        found: list[ResponseRecord] = []
        for name in os.listdir(self._legacy_dir):
            response_id = name[: -len(_JSON)]
            if not name.endswith(_JSON) or not _RESPONSE_ID.match(response_id):
                continue
            record = self._legacy(response_id)
            if record is not None:
                found.append(record)
        return found


def chain(records: list[ResponseRecord]) -> list[ResponseRecord]:
    """Ответы диалога от первого к последнему по ``previous_response_id``."""
    by_id = {record.response_id: record for record in records}
    pointed = {
        record.previous_response_id
        for record in records
        if record.previous_response_id
    }
    heads = [record for record in records if record.response_id not in pointed]
    if heads:
        head = max(heads, key=lambda item: (item.created_at, item.response_id))
    else:
        head = max(
            records, key=lambda item: (item.created_at, item.response_id)
        )
    ordered: list[ResponseRecord] = []
    seen: set[str] = set()
    current: ResponseRecord | None = head
    while current is not None and current.response_id not in seen:
        seen.add(current.response_id)
        ordered.append(current)
        previous = current.previous_response_id
        current = by_id.get(previous) if isinstance(previous, str) else None
    ordered.reverse()
    return ordered


def _payload(record: ResponseRecord) -> dict:
    return {
        "thread_id": record.thread_id,
        "cwd": record.cwd,
        "previous_response_id": record.previous_response_id,
        "created_at": record.created_at,
        "status": record.status,
        "response": record.response,
    }


def _read(path: str, response_id: str) -> ResponseRecord | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    return _record(raw, response_id)


def _record(raw: Any, response_id: str) -> ResponseRecord | None:
    if not isinstance(raw, dict):
        return None
    thread_id = raw.get("thread_id")
    cwd = raw.get("cwd")
    created = raw.get("created_at")
    status = raw.get("status")
    response = raw.get("response")
    if not isinstance(thread_id, str) or not isinstance(cwd, str):
        return None
    if not isinstance(created, int) or not isinstance(status, str):
        return None
    if not isinstance(response, dict):
        return None
    previous = raw.get("previous_response_id")
    if previous is not None and not isinstance(previous, str):
        return None
    return ResponseRecord(
        response_id,
        thread_id,
        os.path.normpath(cwd),
        previous,
        created,
        status,
        response,
    )
