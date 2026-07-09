"""Оркестрация Diff-TUI: единый API list/approve/undo для git- и cache-mode."""
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
from diff_tui.mode import maybe_clear_cache_on_git, resolve_mode
from diff_tui.notices import notice_undo_created, notice_undo_untracked
from diff_tui.paths import canonical_path_key, orig_key
from diff_tui.store import (
    STATUS_CREATED,
    STATUS_DELETED,
    DiffStore,
    UndoResult,
)
from diff_tui.visual import build_unified_diff, truncate_diff
from runtime_notices import push_runtime_notice

PathLike = Union[str, Path]

FileKind = Literal["modified", "created", "deleted"]
ReviewState = Literal["pending", "approved"]


@dataclass(frozen=True)
class DiffFileEntry:
    key: str
    kind: FileKind
    state: ReviewState
    mark: str  # "M" | "A" | "D"


@dataclass(frozen=True)
class OpResult:
    ok: bool
    reason: str = ""
    key: str = ""
    undone_keys: tuple[str, ...] = ()


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
        """Первый вход в git-mode → auto clear устаревшего cache."""
        if self._cache_cleared:
            return
        if self.mode == "git-mode":
            maybe_clear_cache_on_git(self.launch_root)
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
        # fallback: relative_to launch_root
        try:
            return Path(path).resolve().relative_to(
                Path(self.launch_root).resolve()
            ).as_posix()
        except ValueError:
            return None

    # --- list ---

    def list_entries(self, *, pending_only: bool = False) -> list[DiffFileEntry]:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            return self._list_git(pending_only=pending_only)
        return self._list_cache(pending_only=pending_only)

    def _list_git(self, *, pending_only: bool) -> list[DiffFileEntry]:
        out: list[DiffFileEntry] = []
        for e in git_ops.git_status(self.launch_root):
            if pending_only and e.state != "pending":
                continue
            out.append(
                DiffFileEntry(
                    key=e.path,
                    kind=e.kind,
                    state=e.state,
                    mark=_KIND_MARK[e.kind],
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
                # STATUS_DELETED или внешнее удаление (rm) при живом .orig.
                kind = "deleted"
            else:
                kind = "modified"
            # cache: после approve disk==.orig → файл уходит из pending;
            # в index остаётся, но для UX считаем pending пока disk ≠ baseline
            # или created без approve.
            state = self._cache_review_state(e.key, e.status, e.baseline_exists)
            if pending_only and state != "pending":
                continue
            out.append(
                DiffFileEntry(
                    key=e.key,
                    kind=kind,
                    state=state,
                    mark=_KIND_MARK[kind],
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
        if self.mode == "git-mode":
            return self._approve_git(path)
        return self._approve_cache(path)

    def approve_all(self) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            result = git_ops.git_add_all(self.launch_root)
            return OpResult(ok=result.ok, reason=result.stderr if not result.ok else "")
        # cache: approve каждый pending
        ok_any = False
        for entry in self.list_entries(pending_only=True):
            r = self._approve_cache(entry.key)
            ok_any = ok_any or r.ok
        return OpResult(ok=ok_any or not self.list_entries(pending_only=True))

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
            )

    def _approve_cache(self, path: PathLike) -> OpResult:
        key = self._cache_key(path)
        if key is None:
            return OpResult(ok=False, reason="not_trackable")
        with self._lock_for(key):
            ok = self._store.approve(path)
            return OpResult(ok=ok, reason="" if ok else "approve_failed", key=key)

    # --- undo ---

    def undo(self, path: PathLike) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            result = self._undo_git(path)
        else:
            result = self._undo_cache(path)
        return self._with_undone_keys(result)

    def undo_all(self) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            status = {e.path: e for e in git_ops.git_status(self.launch_root)}
            # untracked restore не трогает — в feedback не включаем
            keys = [
                e.key
                for e in self.list_entries(pending_only=True)
                if status.get(e.key) is None or status[e.key].xy != "??"
            ]
            result = git_ops.git_restore_worktree_all(self.launch_root)
            if not result.ok:
                return OpResult(ok=False, reason=result.stderr)
            return OpResult(ok=True, undone_keys=tuple(keys))

        undone: list[str] = []
        for entry in list(self.list_entries(pending_only=True)):
            if entry.kind == "created":
                self._emit_created_notice(entry.key)
                continue
            r = self._undo_cache(entry.key)
            if r.ok and r.key:
                undone.append(r.key)
        return OpResult(ok=bool(undone), undone_keys=tuple(undone))

    def _with_undone_keys(self, result: OpResult) -> OpResult:
        """Успешный undo с реальным эффектом → ``undone_keys`` для thread feedback."""
        if not result.ok or not result.key:
            return result
        if result.reason in ("already_deleted_in_index",):
            return result
        return OpResult(
            ok=True,
            reason=result.reason,
            key=result.key,
            undone_keys=(result.key,),
        )

    def _undo_git(self, path: PathLike) -> OpResult:
        rel = self._git_rel(path)
        if rel is None:
            return OpResult(ok=False, reason="outside_repo")
        with self._lock_for(rel):
            entries = {e.path: e for e in git_ops.git_status(self.launch_root)}
            entry = entries.get(rel)
            if entry is not None and entry.xy == "??":
                self._emit_untracked_notice(rel)
                return OpResult(ok=False, reason="untracked", key=rel)
            # Staged delete: worktree уже совпадает с index (файла нет).
            # ``git restore --worktree`` на pathspec удалённого из index даёт
            # ошибку — трактуем как успешный no-op (спека T12).
            if (
                entry is not None
                and entry.kind == "deleted"
                and entry.state == "approved"
            ):
                return OpResult(ok=True, reason="already_deleted_in_index", key=rel)
            result = git_ops.git_restore_worktree(self.launch_root, rel)
            return OpResult(
                ok=result.ok,
                reason=result.stderr if not result.ok else "",
                key=rel,
            )

    def _undo_cache(self, path: PathLike) -> OpResult:
        key = self._cache_key(path)
        if key is None:
            return OpResult(ok=False, reason="not_trackable")
        with self._lock_for(key):
            result: UndoResult = self._store.undo(path)
            if result.reason == "created_no_approve":
                self._emit_created_notice(key)
            return OpResult(ok=result.ok, reason=result.reason, key=key)

    def _emit_created_notice(self, key: str) -> None:
        if self.emit_notices:
            push_runtime_notice(notice_undo_created(key))

    def _emit_untracked_notice(self, key: str) -> None:
        if self.emit_notices:
            push_runtime_notice(notice_undo_untracked(key))

    # --- maintenance (cache only) ---

    def clear_all(self) -> OpResult:
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            return OpResult(ok=False, reason="not_supported_in_git_mode")
        self._store.clear_all()
        return OpResult(ok=True)

    def visual_diff(self, path: PathLike) -> str:
        """Pending visual diff; пустая строка если не pending / ошибка.

        Context/truncation — из ``diff_tui.preview_context_lines`` / ``max_diff_chars``.
        """
        self.ensure_mode_ready()
        if self.mode == "git-mode":
            rel = self._git_rel(path)
            if rel is None:
                return ""
            raw = git_ops.git_diff_worktree(self.launch_root, rel).stdout
            return truncate_diff(raw, self.max_diff_chars)
        key = self._cache_key(path)
        if key is None:
            return ""
        orig = self._store._orig_path(key)
        disk = self._store._disk_path(key)
        index = self._store.load_index()
        meta = index.get(key) or {}
        if meta.get("status") == STATUS_CREATED and not meta.get("baseline_exists"):
            if not disk.is_file():
                return "(файл был создан, затем удалён с диска)"
            return build_unified_diff(
                "",
                disk.read_text(encoding="utf-8"),
                fromfile="/dev/null",
                tofile=key,
                context_lines=self.preview_context_lines,
                max_diff_chars=self.max_diff_chars,
            )
        # Удаление: .orig есть, файла на диске нет (в т.ч. внешний rm).
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
