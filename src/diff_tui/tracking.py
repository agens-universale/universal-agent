"""TrackingFsBackend: snapshot + per-trace journal для Local+TUI.

Тулы не импортируют diff_tui: обёртка перехватывает write/append/exec/run_python.
Harbor / non-TUI — без этой обёртки.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from diff_tui import git_ops
from diff_tui.mode import resolve_mode
from diff_tui.paths import canonical_path_key
from diff_tui.store import DiffStore
from diff_tui.touched import TouchedJournal
from utils.file_text import BINARY_SAMPLE_BYTES, classify_content_from_sample

_logger = logging.getLogger(__name__)


def _identity_from_ctx(ctx: Any) -> tuple[str, str, str] | None:
    threads_dir = getattr(ctx, "threads_dir", None)
    system_id = getattr(ctx, "system_id", None)
    thread_id = getattr(ctx, "thread_id", None)
    if not (
        isinstance(threads_dir, str)
        and threads_dir.strip()
        and isinstance(system_id, str)
        and system_id.strip()
        and isinstance(thread_id, str)
        and thread_id.strip()
    ):
        return None
    return threads_dir.strip(), system_id.strip(), thread_id.strip()


def _config_from_ctx(ctx: Any) -> dict:
    config_dict = getattr(ctx, "config_dict", None) or {}
    return config_dict if isinstance(config_dict, dict) else {}


def _perimeter(ctx: Any) -> tuple[str, list, list[str], int]:
    """launch_root, extra_paths, protected_extra, cache_max_orig."""
    config_dict = _config_from_ctx(ctx)
    launch_root = config_dict.get("launch_root") or ""
    if not isinstance(launch_root, str) or not launch_root.strip():
        workspace = getattr(ctx, "workspace", None)
        root = getattr(workspace, "root", None)
        launch_root = str(root) if root else ""
    wg = config_dict.get("write_guard") or {}
    extra_paths = list(wg.get("extra_paths") or []) if isinstance(wg, dict) else []
    protected = config_dict.get("protected_paths") or {}
    protected_extra = (
        list(protected.get("extra") or []) if isinstance(protected, dict) else []
    )
    diff_cfg = config_dict.get("diff_tui") or {}
    if not isinstance(diff_cfg, dict):
        diff_cfg = {}
    cache_max_orig = int(diff_cfg.get("cache_max_orig", 500))
    return launch_root, extra_paths, protected_extra, cache_max_orig


def _rel_for_check_ignore(path: str, launch_root: str, key: Optional[str]) -> str:
    from pathlib import Path

    if key is not None and not Path(key).is_absolute():
        return key
    try:
        return Path(path).resolve().relative_to(
            Path(launch_root).resolve()
        ).as_posix()
    except ValueError:
        return path


def wrap_local_for_tui(inner: Any, ctx: Any) -> Any:
    """Local → TrackingFsBackend. Уже Tracking / не Local — без изменений."""
    if isinstance(inner, TrackingFsBackend):
        return inner
    # LocalFsBackend по имени класса — без импорта tools.* на module load
    if type(inner).__name__ != "LocalFsBackend":
        return inner
    return TrackingFsBackend(inner, ctx)


class TrackingFsBackend:
    """Декоратор LocalFsBackend: DiffStore snapshot + TouchedJournal."""

    def __init__(self, inner: Any, ctx: Any) -> None:
        self._inner = inner
        self._ctx = ctx
        self.is_sandbox = bool(getattr(inner, "is_sandbox", False))
        self.default_exec_timeout = getattr(inner, "default_exec_timeout", None)

    @property
    def exec_output_capture_max_chars(self) -> int:
        return int(getattr(self._inner, "exec_output_capture_max_chars", 60_000))

    @exec_output_capture_max_chars.setter
    def exec_output_capture_max_chars(self, value: int) -> None:
        self._inner.exec_output_capture_max_chars = int(value)

    # Capability probes живут на inner (ленивый кэш там же) — обёртка только
    # пробрасывает, иначе native-пути (find/grep) молча считаются недоступными.
    @property
    def has_find(self) -> bool:
        return self._inner.has_find

    @property
    def has_grep(self) -> bool:
        return self._inner.has_grep

    @property
    def grep_exclude_dir(self) -> bool:
        return self._inner.grep_exclude_dir

    @property
    def grep_include(self) -> bool:
        return self._inner.grep_include

    @property
    def inner(self) -> Any:
        return self._inner

    def _active(self) -> bool:
        return bool(getattr(self._ctx, "is_tui", False)) and _identity_from_ctx(
            self._ctx
        ) is not None

    def _journal(self) -> TouchedJournal | None:
        ident = _identity_from_ctx(self._ctx)
        if ident is None:
            return None
        try:
            return TouchedJournal(*ident)
        except ValueError:
            return None

    def _track_text_mutation(self, path: str) -> None:
        """Перед write/append: cache snapshot при необходимости + journal.add."""
        if not self._active() or not isinstance(path, str) or not path:
            return
        try:
            launch_root, extra_paths, protected_extra, cache_max_orig = _perimeter(
                self._ctx
            )
            if not launch_root:
                return
            key = canonical_path_key(
                path,
                launch_root,
                extra_paths=extra_paths,
                protected_extra=protected_extra,
            )
            if key is None:
                return

            mode = resolve_mode(launch_root)
            need_cache = mode == "cache-mode"
            if mode == "git-mode":
                rel = _rel_for_check_ignore(path, launch_root, key)
                ignored = git_ops.check_ignore(launch_root, rel)
                need_cache = ignored is True
                journal_key = rel if ignored is not True else key
            else:
                journal_key = key

            if need_cache:
                store = DiffStore(
                    launch_root,
                    extra_paths=extra_paths,
                    protected_extra=protected_extra,
                    cache_max_orig=cache_max_orig,
                )
                store.register_mutation(path)

            journal = self._journal()
            if journal is not None:
                journal.add(journal_key)
        except Exception:
            _logger.exception("diff_tui tracking write failed path=%s", path)

    def _track_shell_delta(self, run: Callable[[], Any]) -> Any:
        """git-mode: status before/after → journal newly dirty paths. Soft-fail."""
        before: set[str] | None = None
        launch_root = ""
        if self._active():
            try:
                launch_root, _, _, _ = _perimeter(self._ctx)
                if launch_root and resolve_mode(launch_root) == "git-mode":
                    before = {e.path for e in git_ops.git_status(launch_root)}
            except Exception:
                _logger.warning(
                    "shell mutation scope: pre-status failed", exc_info=True
                )
                before = None
        result = run()
        if before is not None:
            try:
                after = {e.path for e in git_ops.git_status(launch_root)}
                journal = self._journal()
                if journal is not None:
                    for rel in sorted(after - before):
                        journal.add(rel)
            except Exception:
                _logger.warning(
                    "shell mutation scope: post-status failed", exc_info=True
                )
        return result

    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        self._track_text_mutation(path)
        self._inner.write(path, content, encoding=encoding)

    def write_bytes(self, path: str, data: bytes) -> None:
        if not classify_content_from_sample(path, data[:BINARY_SAMPLE_BYTES]).is_binary:
            self._track_text_mutation(path)
        self._inner.write_bytes(path, data)

    def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        self._track_text_mutation(path)
        self._inner.append(path, content, encoding=encoding)

    def copy(self, src: str, dst: str) -> None:
        self._inner.copy(src, dst)

    def makedirs(self, path: str) -> None:
        self._inner.makedirs(path)

    def exec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return self._track_shell_delta(
            lambda: self._inner.exec(
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    def exec_argv(
        self,
        argv: list,
        *,
        stdin: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return self._track_shell_delta(
            lambda: self._inner.exec_argv(
                argv,
                stdin=stdin,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    def run_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
    ) -> Any:
        return self._track_shell_delta(
            lambda: self._inner.run_python(code, cwd=cwd, timeout=timeout, env=env)
        )

    def default_cwd(self) -> Optional[str]:
        return self._inner.default_cwd()

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def lexists(self, path: str) -> bool:
        return self._inner.lexists(path)

    def is_file(self, path: str) -> bool:
        return self._inner.is_file(path)

    def is_dir(self, path: str) -> bool:
        return self._inner.is_dir(path)

    def is_symlink(self, path: str) -> bool:
        return self._inner.is_symlink(path)

    def getsize(self, path: str) -> int:
        return self._inner.getsize(path)

    def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        return self._inner.stat_info(path, follow_symlinks=follow_symlinks)

    def readable(self, path: str) -> bool:
        return self._inner.readable(path)

    def writable(self, path: str) -> bool:
        return self._inner.writable(path)

    def realpath(self, path: str) -> str:
        return self._inner.realpath(path)

    def is_hidden(self, path: str) -> bool:
        return self._inner.is_hidden(path)

    def read(self, path: str) -> str:
        return self._inner.read(path)

    def read_bytes(self, path: str) -> bytes:
        return self._inner.read_bytes(path)

    def iter_lines(self, path: str) -> Iterable[str]:
        return self._inner.iter_lines(path)

    def read_head_bytes(self, path: str, n: int) -> bytes:
        return self._inner.read_head_bytes(path, n)

    def sha256_bytes(self, path: str) -> str:
        return self._inner.sha256_bytes(path)

    def listdir(self, path: str) -> list:
        return self._inner.listdir(path)

    def walk(self, path: str) -> list:
        return self._inner.walk(path)

    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        return self._inner.iter_walk(path, should_descend=should_descend)

    def walk_entries(self, path: str, *, prune_names=(), prune_dot_dirs: bool = False) -> list:
        return self._inner.walk_entries(
            path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
        )

    def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names=(),
        prune_dot_dirs: bool = False,
    ):
        return self._inner.iter_walk_entries(
            path,
            should_descend=should_descend,
            prune_names=prune_names,
            prune_dot_dirs=prune_dot_dirs,
        )

    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        return self._inner.iter_files(path, should_descend=should_descend)

    def normpath(self, path: str) -> str:
        return self._inner.normpath(path)

    def join(self, base: str, name: str) -> str:
        return self._inner.join(base, name)

    def isabs(self, path: str) -> bool:
        return self._inner.isabs(path)
