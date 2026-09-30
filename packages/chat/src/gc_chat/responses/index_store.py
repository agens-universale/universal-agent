"""Файлы ``responses-index`` рядом с каталогом тредов."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

from gc_core.thread_store import sanitize_system_id, validate_thread_id
from gc_core.utils.atomic_io import atomic_replace

SYSTEM = "responses"
_DIR_NAME = "responses-index"
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


class ResponseIndex:
    def __init__(self, threads_dir: str) -> None:
        self._threads_dir = threads_dir

    def thread_path(self, thread_id: str) -> str:
        system_id = sanitize_system_id(SYSTEM)
        normalized = validate_thread_id(thread_id)
        name = f"data_{system_id}_{normalized}.jsonl"
        return os.path.join(self._threads_dir, name)

    def save(self, record: ResponseRecord) -> None:
        directory = self._directory()
        os.makedirs(directory, exist_ok=True)
        path = self._path(record.response_id)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=".resp-", suffix=_JSON
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

    def load(self, response_id: str) -> ResponseRecord | None:
        if _RESPONSE_ID.match(response_id) is None:
            return None
        path = self._path(response_id)
        return _read(path, response_id)

    def _path(self, response_id: str) -> str:
        return os.path.join(self._directory(), response_id + _JSON)

    def _directory(self) -> str:
        parent = os.path.dirname(os.path.abspath(self._threads_dir))
        return os.path.join(parent, _DIR_NAME)


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
