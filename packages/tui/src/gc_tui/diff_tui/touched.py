"""Per-trace journal touched-путей для Diff-TUI.

Файл: ``threads_dir/touched_<system>_<thread_id>.json``.
Формат: ``{"paths": {key: {"ts": float}}}``. Контент файлов не хранит.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Set

from gc_core.thread_store import touched_journal_path
from gc_core.thread_store_journal import thread_read_scope
from gc_core.utils.atomic_io import atomic_replace

_logger = logging.getLogger(__name__)


class TouchedJournal:
    """Журнал путей, которые агентский трейс ещё не принял/не откатил."""

    def __init__(
        self, threads_dir: str, system_id: str, thread_id: str
    ) -> None:
        self.path = touched_journal_path(threads_dir, system_id, thread_id)
        self._file = Path(self.path)
        self._thread_identity = (threads_dir, system_id, thread_id)

    def _empty(self) -> dict:
        return {"paths": {}}

    def _load_unlocked(self) -> dict:
        if not self._file.is_file():
            return self._empty()
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
            return self._empty()
        cleaned: dict[str, dict] = {}
        for key, meta in raw["paths"].items():
            if isinstance(key, str) and key and isinstance(meta, dict):
                cleaned[key] = meta
        return {"paths": cleaned}

    def _save_unlocked(self, data: dict) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=0) + "\n",
                encoding="utf-8",
            )
            atomic_replace(str(tmp), str(self._file))
        except OSError as exc:
            _logger.warning(
                "touched journal write failed path=%s: %s", self.path, exc
            )

    @contextmanager
    def _locked(self):
        """The same recovery/lock order used by thread reset and compaction."""
        with thread_read_scope(*self._thread_identity):
            yield

    def paths(self) -> Set[str]:
        with self._locked():
            return set(self._load_unlocked()["paths"])

    def add(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        with self._locked():
            data = self._load_unlocked()
            if key not in data["paths"]:
                data["paths"][key] = {"ts": time.time()}
                self._save_unlocked(data)

    def discard(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        with self._locked():
            data = self._load_unlocked()
            if data["paths"].pop(key, None) is not None:
                self._save_unlocked(data)

    def discard_many(self, keys: Iterable[str]) -> None:
        to_drop = {k for k in keys if isinstance(k, str) and k}
        if not to_drop:
            return
        with self._locked():
            data = self._load_unlocked()
            changed = False
            for key in to_drop:
                if data["paths"].pop(key, None) is not None:
                    changed = True
            if changed:
                self._save_unlocked(data)
