"""Сессии ACP — треды ``system=acp`` в хранилище тредов.

cwd сессии ядро не хранит, а session/list обязан его отдать. Он лежит в
состоянии фронтенда ``frontend["acp"]`` (``cwd``, ``updated_at``, ``title``):
шаг исполнителя это состояние не трогает, в отличие от meta develop, которую
исполнитель перезаписывал из своей копии. Заголовок считается после хода и
запоминается — список сессий не читает историю каждой из них.

Сессии develop лежали в ``acp-sessions/<id>.json`` рядом с тредами. Такой
файл читается, пока у треда нет ``frontend["acp"]``, и пишется вместе с ним
(создание, ход, удаление): откат на develop видит сессии ветки. Двойная
запись — до следующего релиза.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.thread_paths import resolve_threads_dir
from gc_core.thread_store import validate_thread_id
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import JsonObject, ThreadKey, ThreadRecord
from gc_core.utils.atomic_io import atomic_replace

logger = logging.getLogger(__name__)

ACP_SYSTEM = "acp"
_FRONTEND = "acp"
_LEGACY_DIR = "acp-sessions"
_TITLE_LIMIT = 80


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    cwd: str
    updated_at: str


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def thread_key(session_id: str) -> ThreadKey:
    """Тред сессии. Битый id — ``ValueError``."""
    return ThreadKey(system=ACP_SYSTEM, thread=validate_thread_id(session_id))


class SessionIndex:
    """``legacy_dir`` — каталог тредов develop с ``acp-sessions/``."""

    def __init__(
        self,
        threads: Threads,
        *,
        legacy_dir: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.threads = threads
        self._legacy_dir = legacy_dir
        self._clock = clock or now_iso

    @classmethod
    def from_settings(cls, settings: Any) -> SessionIndex:
        threads_dir = resolve_threads_dir(settings)
        return cls(
            Threads(FileThreadStore(threads_dir)), legacy_dir=threads_dir
        )

    async def create(self, session_id: str, cwd: str) -> SessionRecord:
        key = thread_key(session_id)
        record = SessionRecord(
            key.thread, os.path.normpath(cwd), self._clock()
        )
        await self.threads.update_frontend(
            key,
            _FRONTEND,
            {"cwd": record.cwd, "updated_at": record.updated_at},
        )
        self._write_legacy(record)
        return record

    async def load(self, session_id: str) -> SessionRecord | None:
        try:
            key = thread_key(session_id)
        except ValueError:
            return None
        state = self._state(await self.threads.get(key), key.thread)
        return None if state is None else _record(key.thread, state)

    async def touch(self, session_id: str) -> None:
        """Время последнего хода; заголовок — пока его нет."""
        try:
            key = thread_key(session_id)
        except ValueError:
            return
        state = self._state(await self.threads.get(key), key.thread)
        record = None if state is None else _record(key.thread, state)
        if record is None:
            return
        patch: JsonObject = {"cwd": record.cwd, "updated_at": self._clock()}
        if state.get("title") is None:
            patch["title"] = await self._title(key)
        await self.threads.update_frontend(key, _FRONTEND, patch)
        self._write_legacy(
            SessionRecord(key.thread, record.cwd, patch["updated_at"])
        )

    async def remove(self, session_id: str) -> None:
        await self.drop_thread(session_id)

    async def drop_record(self, session_id: str) -> None:
        """Убрать сессию из list/load. Историю не трогать: ход ещё пишет."""
        key = thread_key(session_id)
        if await self.threads.get(key) is not None:
            await self.threads.update_frontend(
                key, _FRONTEND, {"removed": True}
            )
        self._drop_legacy(key.thread)

    async def drop_thread(self, session_id: str) -> None:
        key = thread_key(session_id)
        await self.threads.delete(key)
        self._drop_legacy(key.thread)

    async def list_sessions(self, cwd: str | None) -> list[dict]:
        wanted = None if cwd is None else os.path.normpath(cwd)
        items: list[dict] = []
        async for thread in self.threads.iter_threads(system=ACP_SYSTEM):
            try:
                item = await self._list_item(thread, wanted)
            except (OSError, ValueError):
                # Одна нечитаемая сессия (битый UTF-8 в истории) не прячет
                # остальные.
                logger.warning(
                    "сессия %s не читается", thread.key.thread, exc_info=True
                )
                continue
            if item is not None:
                items.append(item)
        items.sort(
            key=lambda item: (item["updatedAt"], item["sessionId"]),
            reverse=True,
        )
        return items

    async def _list_item(
        self, thread: ThreadRecord, wanted: str | None
    ) -> dict | None:
        # Сессия без единого хода в списке не показывается (как в develop,
        # где у неё ещё не было файла треда).
        if thread.info.messages_count == 0:
            return None
        state = self._state(thread, thread.key.thread)
        record = None if state is None else _record(thread.key.thread, state)
        if record is None or (wanted is not None and wanted != record.cwd):
            return None
        item = {
            "sessionId": record.session_id,
            "cwd": record.cwd,
            "updatedAt": record.updated_at,
        }
        # Заголовок не посчитан (сессия develop или ход ещё не закончился) —
        # из истории, как раньше.
        title = (
            state["title"]
            if "title" in state
            else await self._title(thread.key)
        )
        if title is not None:
            item["title"] = title
        return item

    def _state(
        self, thread: ThreadRecord | None, session_id: str
    ) -> JsonObject | None:
        """Состояние сессии: из треда, а без него — из файла develop (сессия
        без единого хода: файла треда у неё ещё нет)."""
        state = None if thread is None else thread.frontend.get(_FRONTEND)
        if isinstance(state, dict):
            return None if state.get("removed") else state
        return self._legacy_state(session_id)

    def _legacy_path(self, session_id: str) -> str | None:
        if self._legacy_dir is None:
            return None
        return os.path.join(
            self._legacy_dir, _LEGACY_DIR, session_id + ".json"
        )

    def _write_legacy(self, record: SessionRecord) -> None:
        """Файл сессии develop — как его писал develop (tmp + замена)."""
        path = self._legacy_path(record.session_id)
        if path is None:
            return
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=".acp-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"cwd": record.cwd, "updatedAt": record.updated_at},
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")
            atomic_replace(temporary, path)
        except Exception:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise

    def _drop_legacy(self, session_id: str) -> None:
        path = self._legacy_path(session_id)
        if path is not None and os.path.isfile(path):
            os.remove(path)

    def _legacy_state(self, session_id: str) -> JsonObject | None:
        path = self._legacy_path(session_id)
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return {"cwd": raw.get("cwd"), "updated_at": raw.get("updatedAt")}

    async def _title(self, key: ThreadKey) -> str | None:
        for entry in await self.threads.history(key):
            line = _first_line(extract_real_user_message_text(entry.data))
            if line is not None:
                return line
        return None


def _record(session_id: str, state: JsonObject) -> SessionRecord | None:
    cwd = state.get("cwd")
    updated = state.get("updated_at")
    if not isinstance(cwd, str) or cwd == "":
        return None
    if not isinstance(updated, str) or updated == "":
        return None
    return SessionRecord(session_id, os.path.normpath(cwd), updated)


def _first_line(text: str) -> str | None:
    stripped = text.strip()
    if stripped == "":
        return None
    line = stripped.splitlines()[0].strip()
    if line == "":
        return None
    return line[:_TITLE_LIMIT]
