"""Cache-mode store: ``.gc/diffs/`` — ``.orig`` + ``index.json``."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from config_modules.diff_tui import DEFAULT_CACHE_MAX_ORIG
from config_modules.env_vars import project_gc_dir
from diff_tui.paths import (
    canonical_path_key,
    orig_key,
)
from utils.atomic_io import atomic_replace
from utils.file_text import classify_file_content

PathLike = Union[str, os.PathLike[str]]

STATUS_MODIFIED = "modified"
STATUS_CREATED = "created"
STATUS_DELETED = "deleted"


@dataclass(frozen=True)
class IndexEntry:
    key: str
    status: str
    baseline_exists: bool


@dataclass(frozen=True)
class UndoResult:
    ok: bool
    reason: str = ""


class DiffStore:
    """Persistence для cache-mode Diff-TUI.

    Пишет только обвязка Diff-TUI (не agent tools). Путь:
    ``<launch_root>/.gc/diffs/``.
    """

    def __init__(
        self,
        launch_root: PathLike,
        *,
        extra_paths: Sequence[PathLike] | None = None,
        protected_extra: Sequence[str] | None = None,
        cache_max_orig: int = DEFAULT_CACHE_MAX_ORIG,
    ) -> None:
        self.launch_root = str(launch_root)
        self.extra_paths = list(extra_paths or ())
        self.protected_extra = list(protected_extra or ())
        self.cache_max_orig = max(0, int(cache_max_orig))
        self.diffs_dir = project_gc_dir(self.launch_root) / "diffs"
        self.index_path = self.diffs_dir / "index.json"

    # --- paths helpers ---

    def _key_for(self, path: PathLike) -> Optional[str]:
        return canonical_path_key(
            path,
            self.launch_root,
            extra_paths=self.extra_paths,
            protected_extra=self.protected_extra,
        )

    def _orig_path(self, key: str) -> Path:
        return self.diffs_dir / f"{orig_key(key)}.orig"

    def _disk_path(self, key: str) -> Path:
        """Восстановить абсолютный путь файла по canonical key."""
        if os.path.isabs(key):
            return Path(key)
        return Path(self.launch_root) / Path(key)

    def _ensure_dir(self) -> None:
        self.diffs_dir.mkdir(parents=True, exist_ok=True)

    # --- index ---

    def load_index(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.is_file():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw

    def save_index(self, index: dict[str, dict[str, Any]]) -> None:
        self._ensure_dir()
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        atomic_replace(tmp, self.index_path)

    def _write_orig(self, key: str, content: str) -> None:
        self._ensure_dir()
        dest = self._orig_path(key)
        tmp = dest.with_suffix(".orig.tmp")
        tmp.write_text(content, encoding="utf-8")
        atomic_replace(tmp, dest)

    # --- register / snapshot ---

    def register_mutation(self, path: PathLike) -> Optional[str]:
        """Зарегистрировать text-мутацию: snapshot ``.orig`` при первой правке.

        Возвращает ``canonical_path_key`` или ``None`` (вне perimeter / protected /
        binary). Не перезаписывает существующий ``.orig``.
        """
        key = self._key_for(path)
        if key is None:
            return None

        disk = self._disk_path(key)
        if disk.is_file() and classify_file_content(str(disk)).is_binary:
            return None

        index = self.load_index()
        existing = index.get(key)
        # created без approve: .orig не изобретаем на повторных мутациях
        if (
            isinstance(existing, dict)
            and existing.get("status") == STATUS_CREATED
            and not existing.get("baseline_exists", False)
        ):
            return key

        orig = self._orig_path(key)
        if disk.is_file():
            wrote_orig = False
            if not orig.is_file():
                self._write_orig(key, disk.read_text(encoding="utf-8"))
                wrote_orig = True
            index[key] = {
                "status": STATUS_MODIFIED,
                "baseline_exists": True,
            }
            self.save_index(index)
            if wrote_orig:
                # Не вытесняем только что созданный .orig: сразу после snapshot
                # disk == .orig (clean), иначе LRU съест свежий baseline.
                self._maybe_evict(index, protect_key=key)
        else:
            # Новый файл: .orig не создаём.
            if key not in index:
                index[key] = {
                    "status": STATUS_CREATED,
                    "baseline_exists": False,
                }
                self.save_index(index)

        return key

    # --- approve / undo ---

    def approve(self, path: PathLike) -> bool:
        """Перезаписать baseline = disk. Порядок: ``.orig`` → ``index.json``."""
        key = self._key_for(path)
        if key is None:
            return False
        disk = self._disk_path(key)
        if not disk.is_file():
            # deleted approve (v1+) — не в этом шаге; без файла approve no-op
            return False
        if classify_file_content(str(disk)).is_binary:
            return False

        content = disk.read_text(encoding="utf-8")
        # 1) .orig first
        self._write_orig(key, content)
        # 2) then index
        index = self.load_index()
        index[key] = {
            "status": STATUS_MODIFIED,
            "baseline_exists": True,
        }
        self.save_index(index)
        self._maybe_evict(index, protect_key=key)
        return True

    def undo(self, path: PathLike) -> UndoResult:
        key = self._key_for(path)
        if key is None:
            return UndoResult(ok=False, reason="not_tracked")
        index = self.load_index()
        entry = index.get(key)
        if entry is None:
            return UndoResult(ok=False, reason="not_in_index")

        status = entry.get("status")
        if status == STATUS_CREATED and not entry.get("baseline_exists", False):
            # Новый файл без baseline → undo = удалить с диска и из index.
            disk = self._disk_path(key)
            if disk.is_file() or disk.is_symlink():
                disk.unlink()
            del index[key]
            self.save_index(index)
            return UndoResult(ok=True, reason="deleted_created")

        orig = self._orig_path(key)
        if not orig.is_file():
            return UndoResult(ok=False, reason="no_orig")

        content = orig.read_text(encoding="utf-8")
        disk = self._disk_path(key)
        disk.parent.mkdir(parents=True, exist_ok=True)
        tmp = disk.with_suffix(disk.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        atomic_replace(tmp, disk)
        return UndoResult(ok=True, reason="restored")

    # --- list / cleanup ---

    def list_entries(self, *, cleanup_stale: bool = True) -> list[IndexEntry]:
        index = self.load_index()
        if cleanup_stale:
            index = self._cleanup_stale(index)
        return [
            IndexEntry(
                key=key,
                status=str(meta.get("status", STATUS_MODIFIED)),
                baseline_exists=bool(meta.get("baseline_exists", False)),
            )
            for key, meta in sorted(index.items())
            if isinstance(meta, dict)
        ]

    def _cleanup_stale(
        self, index: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Убрать записи, где ожидается ``.orig``, но файла нет.

        ``created`` без ``baseline_exists`` — валидно без ``.orig``.
        """
        changed = False
        cleaned: dict[str, dict[str, Any]] = {}
        for key, meta in index.items():
            if not isinstance(meta, dict):
                changed = True
                continue
            status = meta.get("status")
            baseline = bool(meta.get("baseline_exists", False))
            needs_orig = baseline or status in (STATUS_MODIFIED, STATUS_DELETED)
            if needs_orig and not self._orig_path(key).is_file():
                changed = True
                continue
            cleaned[key] = meta
        if changed:
            self.save_index(cleaned)
        return cleaned

    # --- LRU ---

    def _maybe_evict(
        self,
        index: dict[str, dict[str, Any]],
        *,
        protect_key: str | None = None,
    ) -> None:
        if self.cache_max_orig <= 0:
            return
        while True:
            orig_files = sorted(
                self.diffs_dir.glob("*.orig"),
                key=lambda p: p.stat().st_mtime,
            )
            if len(orig_files) <= self.cache_max_orig:
                return
            # 1) clean first (disk == .orig), oldest-first
            victim = self._find_clean_victim(
                orig_files, index, protect_key=protect_key
            )
            if victim is None:
                # oldest dirty, но не protect_key
                victim = self._oldest_evictable(orig_files, index, protect_key)
            if victim is None:
                return  # только protect_key остался сверх лимита — не трогаем
            self._evict_orig(victim, index)

    def _oldest_evictable(
        self,
        orig_files: list[Path],
        index: dict[str, dict[str, Any]],
        protect_key: str | None,
    ) -> Optional[Path]:
        protected = self._orig_path(protect_key) if protect_key else None
        for orig in orig_files:
            if protected is not None and orig == protected:
                continue
            return orig
        return None

    def _find_clean_victim(
        self,
        orig_files: list[Path],
        index: dict[str, dict[str, Any]],
        *,
        protect_key: str | None = None,
    ) -> Optional[Path]:
        key_by_orig = {self._orig_path(k): k for k in index}
        protected = self._orig_path(protect_key) if protect_key else None
        for orig in orig_files:
            if protected is not None and orig == protected:
                continue
            key = key_by_orig.get(orig)
            if key is None:
                # orphan .orig — вытесняем первым
                return orig
            disk = self._disk_path(key)
            try:
                if disk.is_file() and disk.read_text(encoding="utf-8") == orig.read_text(
                    encoding="utf-8"
                ):
                    return orig
            except OSError:
                continue
        return None

    def _evict_orig(
        self, orig: Path, index: dict[str, dict[str, Any]]
    ) -> None:
        key_by_orig = {self._orig_path(k): k for k in index}
        key = key_by_orig.get(orig)
        try:
            orig.unlink(missing_ok=True)
        except OSError:
            pass
        if key is not None and key in index:
            del index[key]
            self.save_index(index)

    # --- maintenance ---

    def drop_entry(self, key: str) -> bool:
        """Удалить ключ из index и соответствующий ``.orig`` (если есть)."""
        index = self.load_index()
        had = key in index
        if had:
            del index[key]
            self.save_index(index)
        orig = self._orig_path(key)
        try:
            if orig.is_file():
                orig.unlink()
                had = True
        except OSError:
            pass
        return had

    def clear_all(self) -> None:
        if not self.diffs_dir.exists():
            return
        for p in self.diffs_dir.glob("*.orig"):
            try:
                p.unlink()
            except OSError:
                pass
        if self.index_path.exists():
            try:
                self.index_path.unlink()
            except OSError:
                pass
