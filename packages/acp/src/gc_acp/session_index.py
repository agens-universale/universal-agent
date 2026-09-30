"""Файлы acp-sessions рядом с каталогом тредов.

cwd сессии ядро не хранит, а session/list обязан его отдать. В meta треда
поле класть нельзя: исполнитель перезаписывает meta из своей копии.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.thread_store import (
    delete_thread,
    load_thread_messages,
    read_subagent_trace,
    sanitize_system_id,
    subagent_trace_path,
    validate_thread_id,
)
from gc_core.utils.atomic_io import atomic_replace

ACP_SYSTEM = "acp"
_DIR_NAME = "acp-sessions"
_JSON_SUFFIX = ".json"
_TITLE_LIMIT = 80


@dataclass(frozen=True)
class SessionFile:
    session_id: str
    cwd: str
    updated_at: str


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionIndex:
    def __init__(
        self,
        threads_dir: str,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._threads_dir = threads_dir
        self._clock = clock or now_iso

    def create(self, session_id: str, cwd: str) -> SessionFile:
        record = SessionFile(
            validate_thread_id(session_id),
            os.path.normpath(cwd),
            self._clock(),
        )
        self._write(record)
        return record

    def thread_path(self, session_id: str) -> str:
        """jsonl треда. Ядро дописывает сюда function_call по ходу."""
        return _thread_data_path(self._threads_dir, session_id)

    def subagent_path(self, session_id: str) -> str:
        """jsonl вызовов внутри субагента, data_subagents_acp_*."""
        return subagent_trace_path(self._threads_dir, ACP_SYSTEM, session_id)

    def subagent_records(self, session_id: str) -> list[dict]:
        """Уже записанный sidecar. Нет файла или битый id — пустой список."""
        try:
            return read_subagent_trace(
                self._threads_dir, ACP_SYSTEM, session_id
            )
        except ValueError:
            return []

    def load(self, session_id: str) -> SessionFile | None:
        try:
            path = self._path(session_id)
        except ValueError:
            return None
        return _read_record(path, session_id)

    def touch(self, session_id: str) -> None:
        current = self.load(session_id)
        if current is None:
            return
        self._write(
            SessionFile(current.session_id, current.cwd, self._clock())
        )

    def remove(self, session_id: str) -> None:
        delete_thread(self._threads_dir, ACP_SYSTEM, session_id)
        path = self._path(session_id)
        if os.path.isfile(path):
            os.remove(path)

    def list_sessions(self, cwd: str | None) -> list[dict]:
        directory = self._directory()
        if not os.path.isdir(directory):
            return []
        items: list[dict] = []
        for name in os.listdir(directory):
            item = self._list_item(name, cwd)
            if item is not None:
                items.append(item)
        items.sort(
            key=lambda item: (item["updatedAt"], item["sessionId"]),
            reverse=True,
        )
        return items

    def _list_item(self, name: str, cwd: str | None) -> dict | None:
        if not name.endswith(_JSON_SUFFIX):
            return None
        session_id = name[: -len(_JSON_SUFFIX)]
        record = self.load(session_id)
        if record is None or not _thread_exists(self._threads_dir, session_id):
            return None
        if cwd is not None and os.path.normpath(cwd) != record.cwd:
            return None
        return _list_payload(self._threads_dir, record)

    def _write(self, record: SessionFile) -> None:
        directory = self._directory()
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=".acp-", suffix=_JSON_SUFFIX
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_payload(record), handle, ensure_ascii=False)
                handle.write("\n")
            atomic_replace(temporary, self._path(record.session_id))
        except Exception:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise

    def _path(self, session_id: str) -> str:
        validated = validate_thread_id(session_id)
        return os.path.join(self._directory(), validated + _JSON_SUFFIX)

    def _directory(self) -> str:
        return os.path.join(self._threads_dir, _DIR_NAME)


def _payload(record: SessionFile) -> dict:
    return {"cwd": record.cwd, "updatedAt": record.updated_at}


def _read_record(path: str, session_id: str) -> SessionFile | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    cwd = raw.get("cwd")
    updated = raw.get("updatedAt")
    if not isinstance(cwd, str) or cwd == "":
        return None
    if not isinstance(updated, str) or updated == "":
        return None
    return SessionFile(session_id, os.path.normpath(cwd), updated)


def _thread_exists(threads_dir: str, session_id: str) -> bool:
    try:
        path = _thread_data_path(threads_dir, session_id)
    except ValueError:
        return False
    return os.path.isfile(path)


def _thread_data_path(threads_dir: str, session_id: str) -> str:
    system_id = sanitize_system_id(ACP_SYSTEM)
    thread_id = validate_thread_id(session_id)
    name = f"data_{system_id}_{thread_id}.jsonl"
    return os.path.join(threads_dir, name)


def _list_payload(threads_dir: str, record: SessionFile) -> dict:
    item = {
        "sessionId": record.session_id,
        "cwd": record.cwd,
        "updatedAt": record.updated_at,
    }
    title = _title(threads_dir, record.session_id)
    if title is not None:
        item["title"] = title
    return item


def _title(threads_dir: str, session_id: str) -> str | None:
    try:
        messages, _keys, _seq = load_thread_messages(
            threads_dir, ACP_SYSTEM, session_id
        )
    except (OSError, ValueError):
        return None
    for message in messages:
        line = _first_line(extract_real_user_message_text(message))
        if line is not None:
            return line
    return None


def _first_line(text: str) -> str | None:
    stripped = text.strip()
    if stripped == "":
        return None
    line = stripped.splitlines()[0].strip()
    if line == "":
        return None
    return line[:_TITLE_LIMIT]
