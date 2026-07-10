"""Оркестрация Diff-TUI: единый API list/approve/undo для git- и cache-mode.

В git-mode (v2) — гибрид: git-visible через index, ignored — через ``.gc/diffs/``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence, Union

from config_modules.diff_tui import (
    DEFAULT_CACHE_MAX_ORIG,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_PREVIEW_CONTEXT_LINES,
)
from diff_tui import git_ops
from diff_tui.mode import evict_stale_cache, resolve_mode
from diff_tui.paths import canonical_path_key, orig_key
from diff_tui.store import (
    STATUS_CREATED,
    STATUS_DELETED,
    DiffStore,
    UndoResult,
)
from diff_tui.visual import (
    build_created_file_diff,
    build_unified_diff,
    truncate_diff,
)

PathLike = Union[str, Path]

FileKind = Literal["modified", "created", "deleted"]
ReviewState = Literal["pending", "approved"]
FileBackend = Literal["git", "cache"]


@dataclass(frozen=True)
class DiffFileEntry:
    key: str
    kind: FileKind
    state: ReviewState
    mark: str  # "M" | "A" | "D"
    backend: FileBackend = "cache"


@dataclass(frozen=True)
class OpResult:
    ok: bool
    reason: str = ""
    key: str = ""
    undone_keys: tuple[str, ...] = ()
    restored_keys: tuple[str, ...] = ()
    deleted_keys: tuple[str, ...] = ()
    backend: FileBackend | Literal[""] = ""
    git_count: int = 0
    cache_count: int = 0


_KIND_MARK = {
    "modified": "M",
    "created": "A",
    "deleted": "D",
}


class DiffService:
    """Единый порт операций для TUI. Mode выбирается по ``launch_root``."""

    def __init__(
        self,
        launch_root: PathLike,
        *,
        extra_paths: Sequence[PathLike] | None = None,
        protected_extra: Sequence[str] | None = None,
        cache_max_orig: int = DEFAULT_CACHE_MAX_ORIG,
        preview_context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        emit_notices: bool = True,
    ) -> None:
        self.launch_root = str(Path(launch_root))
        self.extra_paths = list(extra_paths or ())
        self.protected_extra = list(protected_extra or ())
        self.cache_max_orig = cache_max_orig
        self.preview_context_lines = preview_context_lines
        self.max_diff_chars = max_diff_chars
        self.emit_notices = emit_notices
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._cache_cleared = False
        self._store = DiffStore(
            self.launch_root,
            extra_paths=self.extra_paths,
            protected_extra=self.protected_extra,
            cache_max_orig=self.cache_max_orig,
        )

    @property
    def mode(self) -> str:
        return resolve_mode(self.launch_root)

    def ensure_mode_ready(self) -> None:
        """Первый вход: stale-cleanup cache-ключей, видимых git (без полного wipe)."""
        if self._cache_cleared:
            return
        if self.mode == "git-mode":
            git_keys = {
                e.path
                for e in git_ops.git_status(self.launch_root)
                if self._cache_key(e.path) is not None
            }
            evict_stale_cache(self._store, git_visible_keys=git_keys)
        self._cache_cleared = True

    def _lock_for(self, key: str) -> threading.Lock:
        token = orig_key(key) if key else "__all__"
        with self._locks_guard:
            lock = self._locks.get(token)
            if lock is None:
                lock = threading.Lock()
                self._locks[token] = lock
            return lock

    def _cache_key(self, path: PathLike) -> Optional[str]:
        return canonical_path_key(
            path,
            self.launch_root,
            extra_paths=self.extra_paths,
            protected_extra=self.protected_extra,
        )

    def _git_rel(self, path: PathLike) -> Optional[str]:
        """Относительный путь для git args (POSIX)."""
        key = self._cache_key(path)
        if key is not None and not Path(key).is_absolute():
            return key
        try:
            return Path(path).resolve().relative_to(
                Path(self.launch_root).resolve()
            ).as_posix()
        except ValueError:
            return None

    def backend_for(self, path: PathLike) -> Optional[FileBackend]:
        """Per-file backend: ``git`` | ``cache`` | ``None`` (не трекаем)."""
        key = self._cache_key(path)
        if key is None:
            return None
        if self.mode == "cache-mode":
            return "cache"

        rel = self._git_rel(path) or key
        ignored = git_ops.check_ignore(self.launch_root, rel)
        if ignored is True:
            return "cache"
        if ignored is False:
            return "git"
        # check-ignore fail: git только если путь уже в status; иначе не трекаем
        status_paths = {e.path for e in git_ops.git_status(self.launch_root)}
        if rel in status_paths or key in status_paths:
            return "git"
        return None

    # --- list ---

    def list_entries(self, *, pending_only: bool = False) -> list[DiffFileEntry]:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            return self._list_hybrid(pending_only=pending_only)
        return self._list_cache(pending_only=pending_only)

    def _list_hybrid(self, *, pending_only: bool) -> list[DiffFileEntry]:
        git_entries = list(git_ops.git_status(self.launch_root))
        # protected / вне perimeter (.gc и т.п.) — не показываем в /diff
        trackable_git = [
            e for e in git_entries if self._cache_key(e.path) is not None
        ]
        git_keys = {e.path for e in trackable_git}
        # конфликт → git побеждает; stale cache удаляем при refresh
        evict_stale_cache(self._store, git_visible_keys=git_keys)

        out: list[DiffFileEntry] = []
        for e in trackable_git:
            if pending_only and e.state != "pending":
                continue
            out.append(
                DiffFileEntry(
                    key=e.path,
                    kind=e.kind,
                    state=e.state,
                    mark=_KIND_MARK[e.kind],
                    backend="git",
                )
            )

        for entry in self._list_cache(pending_only=pending_only):
            if entry.key in git_keys:
                continue
            out.append(
                DiffFileEntry(
                    key=entry.key,
                    kind=entry.kind,
                    state=entry.state,
                    mark=entry.mark,
                    backend="cache",
                )
            )
        return out

    def _list_cache(self, *, pending_only: bool) -> list[DiffFileEntry]:
        out: list[DiffFileEntry] = []
        for e in self._store.list_entries(cleanup_stale=True):
            disk = self._store._disk_path(e.key)
            orig = self._store._orig_path(e.key)
            kind: FileKind
            if e.status == STATUS_CREATED and not e.baseline_exists:
                kind = "created"
            elif e.status == STATUS_DELETED or (
                not disk.is_file() and orig.is_file()
            ):
                kind = "deleted"
            else:
                kind = "modified"
            state = self._cache_review_state(e.key, e.status, e.baseline_exists)
            if pending_only and state != "pending":
                continue
            out.append(
                DiffFileEntry(
                    key=e.key,
                    kind=kind,
                    state=state,
                    mark=_KIND_MARK[kind],
                    backend="cache",
                )
            )
        return out

    def _cache_review_state(
        self, key: str, status: str, baseline_exists: bool
    ) -> ReviewState:
        if status == STATUS_CREATED and not baseline_exists:
            return "pending"
        orig = self._store._orig_path(key)
        disk = self._store._disk_path(key)
        if not orig.is_file():
            return "pending"
        try:
            if disk.is_file() and disk.read_text(encoding="utf-8") == orig.read_text(
                encoding="utf-8"
            ):
                return "approved"
        except OSError:
            pass
        return "pending"

    # --- approve ---

    def approve(self, path: PathLike) -> OpResult:
        self.ensure_mode_ready()
        backend = self.backend_for(path)
        if backend == "git":
            return self._approve_git(path)
        if backend == "cache":
            return self._approve_cache(path)
        return OpResult(ok=False, reason="not_trackable")

    def approve_all(self) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "cache-mode":
            ok_any = False
            cache_n = 0
            for entry in self.list_entries(pending_only=True):
                r = self._approve_cache(entry.key)
                if r.ok:
                    ok_any = True
                    cache_n += 1
            return OpResult(
                ok=ok_any or not self.list_entries(pending_only=True),
                cache_count=cache_n,
            )

        pending = self.list_entries(pending_only=True)
        git_pending = [e for e in pending if e.backend == "git"]
        cache_pending = [e for e in pending if e.backend == "cache"]

        git_n = 0
        for entry in git_pending:
            # Поштучный add: не трогаем protected/.gc (в отличие от ``git add -A``).
            if self._approve_git(entry.key).ok:
                git_n += 1

        cache_n = 0
        for entry in cache_pending:
            if self._approve_cache(entry.key).ok:
                cache_n += 1

        return OpResult(
            ok=True,
            git_count=git_n,
            cache_count=cache_n,
        )

    def _approve_git(self, path: PathLike) -> OpResult:
        rel = self._git_rel(path)
        if rel is None:
            return OpResult(ok=False, reason="outside_repo")
        with self._lock_for(rel):
            result = git_ops.git_add(self.launch_root, rel)
            return OpResult(
                ok=result.ok,
                reason=result.stderr if not result.ok else "",
                key=rel,
                backend="git",
            )

    def _approve_cache(self, path: PathLike) -> OpResult:
        key = self._cache_key(path)
        if key is None:
            return OpResult(ok=False, reason="not_trackable")
        with self._lock_for(key):
            ok = self._store.approve(path)
            return OpResult(
                ok=ok,
                reason="" if ok else "approve_failed",
                key=key,
                backend="cache",
            )

    # --- undo ---

    def undo(self, path: PathLike) -> OpResult:
        self.ensure_mode_ready()
        backend = self.backend_for(path)
        if backend == "git":
            result = self._undo_git(path)
        elif backend == "cache":
            result = self._undo_cache(path)
        else:
            result = OpResult(ok=False, reason="not_trackable")
        return self._with_undone_keys(result)

    def undo_all(self) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "cache-mode":
            restored: list[str] = []
            deleted: list[str] = []
            for entry in list(self.list_entries(pending_only=True)):
                r = self._undo_cache(entry.key)
                if not r.ok or not r.key:
                    continue
                if r.reason == "deleted_created":
                    deleted.append(r.key)
                else:
                    restored.append(r.key)
            undone = tuple(restored) + tuple(deleted)
            return OpResult(
                ok=bool(undone),
                undone_keys=undone,
                restored_keys=tuple(restored),
                deleted_keys=tuple(deleted),
                cache_count=len(undone),
            )

        pending = self.list_entries(pending_only=True)
        git_pending = [e for e in pending if e.backend == "git"]
        cache_pending = [e for e in pending if e.backend == "cache"]

        status = {e.path: e for e in git_ops.git_status(self.launch_root)}
        untracked = [
            e.key
            for e in git_pending
            if status.get(e.key) is not None and status[e.key].xy == "??"
        ]
        tracked = [e.key for e in git_pending if e.key not in set(untracked)]
        for rel in untracked:
            self._delete_path(Path(self.launch_root) / rel)

        git_n = len(untracked)
        if tracked:
            result = git_ops.git_restore_worktree_all(self.launch_root)
            if not result.ok:
                return OpResult(ok=False, reason=result.stderr)
            git_n += len(tracked)

        cache_restored: list[str] = []
        cache_deleted: list[str] = []
        for entry in cache_pending:
            r = self._undo_cache(entry.key)
            if not r.ok or not r.key:
                continue
            if r.reason == "deleted_created":
                cache_deleted.append(r.key)
            else:
                cache_restored.append(r.key)

        restored = tuple(tracked) + tuple(cache_restored)
        deleted = tuple(untracked) + tuple(cache_deleted)
        all_undone = restored + deleted
        return OpResult(
            ok=bool(all_undone) or (not git_pending and not cache_pending),
            undone_keys=all_undone,
            restored_keys=restored,
            deleted_keys=deleted,
            git_count=git_n,
            cache_count=len(cache_restored) + len(cache_deleted),
        )

    def _with_undone_keys(self, result: OpResult) -> OpResult:
        """Успешный undo с реальным эффектом → ключи для thread feedback."""
        if not result.ok or not result.key:
            return result
        if result.reason in ("already_deleted_in_index",):
            return result
        key = result.key
        deleted = result.reason in ("deleted_created", "deleted_untracked")
        return OpResult(
            ok=True,
            reason=result.reason,
            key=key,
            undone_keys=(key,),
            restored_keys=() if deleted else (key,),
            deleted_keys=(key,) if deleted else (),
            backend=result.backend,
        )

    def _undo_git(self, path: PathLike) -> OpResult:
        rel = self._git_rel(path)
        if rel is None:
            return OpResult(ok=False, reason="outside_repo")
        with self._lock_for(rel):
            entries = {e.path: e for e in git_ops.git_status(self.launch_root)}
            entry = entries.get(rel)
            if entry is not None and entry.xy == "??":
                self._delete_path(Path(self.launch_root) / rel)
                return OpResult(
                    ok=True,
                    reason="deleted_untracked",
                    key=rel,
                    backend="git",
                )
            if (
                entry is not None
                and entry.kind == "deleted"
                and entry.state == "approved"
            ):
                return OpResult(
                    ok=True,
                    reason="already_deleted_in_index",
                    key=rel,
                    backend="git",
                )
            result = git_ops.git_restore_worktree(self.launch_root, rel)
            return OpResult(
                ok=result.ok,
                reason=result.stderr if not result.ok else "restored",
                key=rel,
                backend="git",
            )

    def _undo_cache(self, path: PathLike) -> OpResult:
        key = self._cache_key(path)
        if key is None:
            return OpResult(ok=False, reason="not_trackable")
        with self._lock_for(key):
            result: UndoResult = self._store.undo(path)
            return OpResult(
                ok=result.ok,
                reason=result.reason,
                key=key,
                backend="cache",
            )

    @staticmethod
    def _delete_path(path: Path) -> None:
        """Удалить created/untracked файл (или symlink) с диска."""
        if path.is_file() or path.is_symlink():
            path.unlink()

    # --- maintenance ---

    def clear_all(self) -> OpResult:
        """Очистить только ``.gc/diffs/`` (в git-mode git-слой не трогаем)."""
        self.ensure_mode_ready()
        self._store.clear_all()
        return OpResult(ok=True)

    def visual_diff(self, path: PathLike) -> str:
        """Pending visual diff; пустая строка если не pending / ошибка.

        Context/truncation — из ``diff_tui.preview_context_lines`` / ``max_diff_chars``.
        Для created/untracked (``git diff`` пуст) — unified diff содержимого файла.
        """
        self.ensure_mode_ready()
        backend = self.backend_for(path)
        if backend == "git":
            return self._visual_diff_git(path)
        if backend != "cache":
            return ""
        return self._visual_diff_cache(path)

    def _visual_diff_git(self, path: PathLike) -> str:
        rel = self._git_rel(path)
        if rel is None:
            return ""
        raw = git_ops.git_diff_worktree(self.launch_root, rel).stdout
        if raw.strip():
            return truncate_diff(raw, self.max_diff_chars)
        # Untracked / created: ``git diff`` не видит ``??`` — показываем файл как addition
        by_path = {e.path: e for e in git_ops.git_status(self.launch_root)}
        entry = by_path.get(rel)
        if entry is None or entry.kind != "created":
            return ""
        return self._visual_diff_as_created(rel, Path(self.launch_root) / rel)

    def _visual_diff_as_created(self, key: str, disk: Path) -> str:
        """Unified diff ``/dev/null`` → содержимое нового файла на диске."""
        if not disk.is_file():
            return "(файл был создан, затем удалён с диска)"
        try:
            content = disk.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        return build_created_file_diff(
            content,
            tofile=key,
            context_lines=self.preview_context_lines,
            max_diff_chars=self.max_diff_chars,
        )

    def _visual_diff_cache(self, path: PathLike) -> str:
        key = self._cache_key(path)
        if key is None:
            return ""
        orig = self._store._orig_path(key)
        disk = self._store._disk_path(key)
        index = self._store.load_index()
        meta = index.get(key) or {}
        if meta.get("status") == STATUS_CREATED and not meta.get("baseline_exists"):
            return self._visual_diff_as_created(key, disk)
        if orig.is_file() and not disk.is_file():
            return build_unified_diff(
                orig.read_text(encoding="utf-8"),
                "",
                fromfile=f"a/{key}",
                tofile="/dev/null",
                context_lines=self.preview_context_lines,
                max_diff_chars=self.max_diff_chars,
            )
        if not orig.is_file() or not disk.is_file():
            return ""
        return build_unified_diff(
            orig.read_text(encoding="utf-8"),
            disk.read_text(encoding="utf-8"),
            fromfile=f"a/{key}",
            tofile=f"b/{key}",
            context_lines=self.preview_context_lines,
            max_diff_chars=self.max_diff_chars,
        )
