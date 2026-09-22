"""Оркестрация Diff-TUI: единый API list/approve/undo для git- и cache-mode.

В git-mode (v2) — гибрид: git-visible через index, ignored — через ``.gc/diffs/``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence, Union

from gc_core.config_modules.diff_tui import (
    DEFAULT_CACHE_MAX_ORIG,
    DEFAULT_FULL_FILE_MAX_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_PREVIEW_CONTEXT_LINES,
)
from gc_core.utils.file_text import decode_text_best_effort

from gc_tui.diff_tui import git_ops
from gc_tui.diff_tui.mode import evict_stale_cache, resolve_mode
from gc_tui.diff_tui.paths import canonical_path_key, orig_key
from gc_tui.diff_tui.store import STATUS_CREATED, STATUS_DELETED, DiffStore, UndoResult
from gc_tui.diff_tui.touched import TouchedJournal
from gc_tui.diff_tui.visual import build_created_file_diff, build_visual_diff

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
    """Единый порт операций для TUI. Mode выбирается по ``launch_root``.

    Listing и approve/undo scoped по per-trace ``TouchedJournal``
    (``threads_dir`` + ``system_id`` + ``thread_id``). Без identity —
    пустой list (чужой dirty не показываем).
    """

    def __init__(
        self,
        launch_root: PathLike,
        *,
        extra_paths: Sequence[PathLike] | None = None,
        protected_extra: Sequence[str] | None = None,
        cache_max_orig: int = DEFAULT_CACHE_MAX_ORIG,
        preview_context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES,
        full_file_max_lines: int = DEFAULT_FULL_FILE_MAX_LINES,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        emit_notices: bool = True,
        threads_dir: str | None = None,
        system_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self.launch_root = str(Path(launch_root))
        self.extra_paths = list(extra_paths or ())
        self.protected_extra = list(protected_extra or ())
        self.cache_max_orig = cache_max_orig
        self.preview_context_lines = preview_context_lines
        self.full_file_max_lines = full_file_max_lines
        self.max_diff_chars = max_diff_chars
        self.emit_notices = emit_notices
        self.threads_dir = threads_dir
        self.system_id = system_id
        self.thread_id = thread_id
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._cache_cleared = False
        self._store = DiffStore(
            self.launch_root,
            extra_paths=self.extra_paths,
            protected_extra=self.protected_extra,
            cache_max_orig=self.cache_max_orig,
        )
        self._journal: TouchedJournal | None = None
        if (
            isinstance(threads_dir, str)
            and threads_dir.strip()
            and isinstance(system_id, str)
            and system_id.strip()
            and isinstance(thread_id, str)
            and thread_id.strip()
        ):
            try:
                self._journal = TouchedJournal(threads_dir, system_id, thread_id)
            except ValueError:
                self._journal = None

    @property
    def mode(self) -> str:
        return resolve_mode(self.launch_root)

    def ensure_mode_ready(self) -> None:
        """Первый вход: stale-cleanup cache-ключей, видимых git (без полного wipe)."""
        if self._cache_cleared:
            return
        if self.mode == "git-mode":
            git_keys = {e.path for e in git_ops.git_status(self.launch_root) if self._cache_key(e.path) is not None}
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
            return Path(path).resolve().relative_to(Path(self.launch_root).resolve()).as_posix()
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

    def _touched_paths(self) -> set[str]:
        if self._journal is None:
            return set()
        return self._journal.paths()

    def _discard_own(self, *keys: str) -> None:
        if self._journal is None or not keys:
            return
        self._journal.discard_many(keys)

    # --- list ---

    def list_entries(self, *, pending_only: bool = False) -> list[DiffFileEntry]:
        self.ensure_mode_ready()
        touched = self._touched_paths()
        if not touched:
            return []
        if self.mode == "git-mode":
            raw = self._list_hybrid(pending_only=pending_only, touched=touched)
        else:
            raw = self._list_cache(pending_only=pending_only, touched=touched)
        shown = {e.key for e in raw}
        stale = touched - shown
        if stale:
            self._discard_own(*stale)
        return raw

    def _list_hybrid(self, *, pending_only: bool, touched: set[str]) -> list[DiffFileEntry]:
        git_entries = list(git_ops.git_status(self.launch_root))
        # protected / вне perimeter (.gc и т.п.) — не показываем в /diff
        trackable_git = [e for e in git_entries if e.path in touched and self._cache_key(e.path) is not None]
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

        for entry in self._list_cache(pending_only=pending_only, touched=touched):
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

    def _list_cache(self, *, pending_only: bool, touched: set[str]) -> list[DiffFileEntry]:
        out: list[DiffFileEntry] = []
        for e in self._store.list_entries(cleanup_stale=True):
            if e.key not in touched:
                continue
            disk = self._store._disk_path(e.key)
            orig = self._store._orig_path(e.key)
            kind: FileKind
            if e.status == STATUS_CREATED and not e.baseline_exists:
                kind = "created"
            elif e.status == STATUS_DELETED or (not disk.is_file() and orig.is_file()):
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

    def _cache_review_state(self, key: str, status: str, baseline_exists: bool) -> ReviewState:
        if status == STATUS_CREATED and not baseline_exists:
            return "pending"
        orig = self._store._orig_path(key)
        disk = self._store._disk_path(key)
        if not orig.is_file():
            return "pending"
        try:
            if disk.is_file() and disk.read_bytes() == orig.read_bytes():
                return "approved"
        except OSError:
            pass
        return "pending"

    # --- approve ---

    def approve(self, path: PathLike) -> OpResult:
        self.ensure_mode_ready()
        backend = self.backend_for(path)
        if backend == "git":
            result = self._approve_git(path)
        elif backend == "cache":
            result = self._approve_cache(path)
        else:
            result = OpResult(ok=False, reason="not_trackable")
        if result.ok and result.key:
            self._discard_own(result.key)
        return result

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
                    if r.key:
                        self._discard_own(r.key)
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
            r = self._approve_git(entry.key)
            if r.ok:
                git_n += 1
                if r.key:
                    self._discard_own(r.key)

        cache_n = 0
        for entry in cache_pending:
            r = self._approve_cache(entry.key)
            if r.ok:
                cache_n += 1
                if r.key:
                    self._discard_own(r.key)

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
        result = self._with_undone_keys(result)
        if result.ok and result.key:
            self._discard_own(result.key)
        return result

    def undo_all(self) -> OpResult:
        """Откат только файлов текущего list (после ∩ journal). Per-path restore."""
        self.ensure_mode_ready()
        pending = self.list_entries(pending_only=True)
        if self.mode == "cache-mode":
            restored: list[str] = []
            deleted: list[str] = []
            for entry in list(pending):
                r = self._undo_cache(entry.key)
                if not r.ok or not r.key:
                    continue
                self._discard_own(r.key)
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

        git_pending = [e for e in pending if e.backend == "git"]
        cache_pending = [e for e in pending if e.backend == "cache"]

        status = {e.path: e for e in git_ops.git_status(self.launch_root)}
        restored_git: list[str] = []
        deleted_git: list[str] = []
        for entry in git_pending:
            st = status.get(entry.key)
            if st is not None and st.xy == "??":
                self._delete_path(Path(self.launch_root) / entry.key)
                deleted_git.append(entry.key)
                self._discard_own(entry.key)
                continue
            result = git_ops.git_restore_worktree(self.launch_root, entry.key)
            if result.ok:
                restored_git.append(entry.key)
                self._discard_own(entry.key)

        cache_restored: list[str] = []
        cache_deleted: list[str] = []
        for entry in cache_pending:
            r = self._undo_cache(entry.key)
            if not r.ok or not r.key:
                continue
            self._discard_own(r.key)
            if r.reason == "deleted_created":
                cache_deleted.append(r.key)
            else:
                cache_restored.append(r.key)

        restored = tuple(restored_git) + tuple(cache_restored)
        deleted = tuple(deleted_git) + tuple(cache_deleted)
        all_undone = restored + deleted
        return OpResult(
            ok=bool(all_undone) or (not git_pending and not cache_pending),
            undone_keys=all_undone,
            restored_keys=restored,
            deleted_keys=deleted,
            git_count=len(restored_git) + len(deleted_git),
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
            if entry is not None and entry.kind == "deleted" and entry.state == "approved":
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

    def visual_diff(self, path: PathLike, *, mode: str = "adaptive") -> str:
        """Pending visual diff; пустая строка если не pending / ошибка.

        ``mode``: ``adaptive`` (Enter — порог 300/50) или ``full`` (``f`` — весь
        файл без порога). Context/truncation — из ``diff_tui.*``. Git- и cache-
        backend ведут себя одинаково (D1) — baseline↔disk через ``build_visual_diff``,
        без сырого ``git diff``.
        """
        self.ensure_mode_ready()
        backend = self.backend_for(path)
        if backend == "git":
            return self._visual_diff_git(path, mode=mode)
        if backend != "cache":
            return ""
        return self._visual_diff_cache(path, mode=mode)

    def _visual_diff_git(self, path: PathLike, *, mode: str = "adaptive") -> str:
        rel = self._git_rel(path)
        if rel is None:
            return ""
        # baseline из index (``git show :<path>``); для untracked/deleted — нет в index.
        baseline_res = git_ops.git_show_index(self.launch_root, rel)
        disk = Path(self.launch_root) / rel
        by_path = {e.path: e for e in git_ops.git_status(self.launch_root)}
        entry = by_path.get(rel)

        if baseline_res.ok:
            # index↔disk (modified или staged-add с worktree-изм.)
            old = baseline_res.stdout
            new = self._read_disk(disk)
            if new is None:
                # файл удалён с диска, но index жив → deleted
                return self._build_deleted(key=rel, orig_text=old, mode=mode)
            return build_visual_diff(
                old,
                new,
                mode=mode,
                full_file_max_lines=self.full_file_max_lines,
                preview_context_lines=self.preview_context_lines,
                max_diff_chars=self.max_diff_chars,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        # Нет в index: untracked (created) или staged-delete (worktree удалён).
        if entry is not None and entry.kind == "created":
            return self._visual_diff_as_created(rel, disk)
        # staged-delete: index пуст, но git status помечает deleted → покажем
        # удаление от последнего staged не можем (нет baseline в index).
        return ""

    def _visual_diff_as_created(self, key: str, disk: Path) -> str:
        """Unified diff ``/dev/null`` → содержимое нового файла на диске.

        ``mode`` не нужен: созданный файл всегда показывается целиком как
        addition (difflib ``/dev/null`` → content; контекст не релевантен).
        """
        if not disk.is_file():
            return "(файл был создан, затем удалён с диска)"
        content = self._read_disk(disk)
        if content is None:
            return ""
        return build_created_file_diff(
            content,
            tofile=key,
            context_lines=self.preview_context_lines,
            max_diff_chars=self.max_diff_chars,
        )

    def _build_deleted(self, *, key: str, orig_text: str, mode: str = "adaptive") -> str:
        """disk↔``/dev/null`` удаление через ``build_visual_diff``."""
        return build_visual_diff(
            orig_text,
            "",
            mode=mode,
            full_file_max_lines=self.full_file_max_lines,
            preview_context_lines=self.preview_context_lines,
            max_diff_chars=self.max_diff_chars,
            fromfile=f"a/{key}",
            tofile="/dev/null",
        )

    @staticmethod
    def _read_disk(path: Path) -> Optional[str]:
        """Decode path bytes for visual diff (utf-8 / BOM / chardet allowlist)."""
        if not path.is_file():
            return None
        try:
            return decode_text_best_effort(path.read_bytes())
        except OSError:
            return None

    def _visual_diff_cache(self, path: PathLike, *, mode: str = "adaptive") -> str:
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
            old = self._read_disk(orig)
            if old is None:
                return ""
            return build_visual_diff(
                old,
                "",
                mode=mode,
                full_file_max_lines=self.full_file_max_lines,
                preview_context_lines=self.preview_context_lines,
                max_diff_chars=self.max_diff_chars,
                fromfile=f"a/{key}",
                tofile="/dev/null",
            )
        if not orig.is_file() or not disk.is_file():
            return ""
        old = self._read_disk(orig)
        new = self._read_disk(disk)
        if old is None or new is None:
            return ""
        return build_visual_diff(
            old,
            new,
            mode=mode,
            full_file_max_lines=self.full_file_max_lines,
            preview_context_lines=self.preview_context_lines,
            max_diff_chars=self.max_diff_chars,
            fromfile=f"a/{key}",
            tofile=f"b/{key}",
        )
