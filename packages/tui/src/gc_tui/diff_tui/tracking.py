"""TrackingFsBackend: snapshot + per-trace journal для Local+TUI.

Тулы не импортируют diff_tui: обёртка перехватывает write/append/exec/run_python.
Harbor / non-TUI — без этой обёртки.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from gc_tui.diff_tui import git_ops
from gc_tui.diff_tui.mode import resolve_mode
from gc_tui.diff_tui.paths import canonical_path_key
from gc_tui.diff_tui.store import DiffStore
from gc_tui.diff_tui.touched import TouchedJournal
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
)
from gc_core.utils.file_text import BINARY_SAMPLE_BYTES, classify_content_from_sample

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


def attach_to_context(ctx: Any, config_dict: dict) -> None:
    """Включить diff-журнал на готовом контексте: обернуть backend и пересобрать workspace.

    Живёт здесь, а не в ядре. До 27.08.2026 эти три действия делал сам
    `gc_core.run_context.create_run_context` под `if is_tui`, и ради них ядро
    импортировало `gc_tui.diff_tui.tracking` — единственная его зависимость от
    фронтенда. Знание «обернул backend — пересобери workspace» принадлежит тому, кто
    оборачивает, поэтому переехало к обёртке; ядро о diff-экране больше не знает.

    Идемпотентна: повторный вызов ничего не меняет (`wrap_local_for_tui` возвращает
    тот же объект, если он уже Tracking или не Local).
    """
    from gc_core.execution_workspace import build_execution_workspace

    wrapped = wrap_local_for_tui(ctx.os_backend, ctx)
    if wrapped is ctx.os_backend:
        return
    ctx.os_backend = wrapped
    ctx.workspace = build_execution_workspace(wrapped, config_dict)


class TrackingFsBackend:
    """Декоратор LocalFsBackend: DiffStore snapshot + TouchedJournal.

    Порт не наследуется (обёртка повторяет его поверхность, а не является
    им), но форма методов обязана совпадать: файловые операции корутинные,
    обходчики — асинхронные генераторы. Примеси ``AsyncFileOps`` с
    двойниками больше нет: у порта одна форма, дублировать нечего.
    """

    def __init__(self, inner: Any, ctx: Any) -> None:
        self._inner = inner
        self._ctx = ctx
        self.is_sandbox = bool(getattr(inner, "is_sandbox", False))
        self.default_exec_timeout = getattr(inner, "default_exec_timeout", None)

    @property
    def exec_output_capture_max_chars(self) -> int:
        return int(
            getattr(
                self._inner,
                "exec_output_capture_max_chars",
                DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
            )
        )

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

    async def _atrack_text_mutation(self, path: str) -> None:
        """Снимок и journal перед записью — в потоке, не на loop.

        Внутри ``_track_text_mutation`` живёт ``git_ops.check_ignore``, то есть
        ЗАПУСК git, и копирование файла в ``DiffStore``. Пока запись тула была
        синхронной, агента вёл демон-поток TUI, и loop это не касалось. После
        перехода на async-only агент работает НА loop, и та же проба стала
        морозить его на каждой записи файла — код не менялся, изменилось, где
        он исполняется.

        Сосед ``_atrack_shell_delta`` уводит свои ``git status`` в поток с тем
        же обоснованием; здесь ровно оно.
        """
        import asyncio

        await asyncio.to_thread(self._track_text_mutation, path)

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

    async def _atrack_shell_delta(self, run: Callable[[], Any]) -> Any:
        """Команда ждётся, а два `git status` уходят в поток.

        ЕДИНСТВЕННАЯ форма. Синхронный близнец снят вместе с синхронными
        `exec` / `exec_argv` / `run_python` этой обёртки: цепочка
        `SyncExecAdapter` → `to_thread(TrackingFsBackend.exec)` →
        `LocalFsBackend.exec` → `run_coro_blocking(self.aexec)` заводила ЕЩЁ
        ОДИН loop в потоке пула. Кооперативный стоп до подпроцесса не доходил,
        а `asyncio.run` хода на выходе ждал `shutdown_default_executor`: после
        Esc поток жил до таймаута команды.

        `git_status` — это запуск git, то есть блокирующий вызов; на loop его
        звать нельзя, поэтому обе пробы идут через `to_thread`.
        """
        import asyncio

        before: set[str] | None = None
        launch_root = ""
        if self._active():
            try:
                launch_root, _, _, _ = _perimeter(self._ctx)
                if launch_root and resolve_mode(launch_root) == "git-mode":
                    entries = await asyncio.to_thread(git_ops.git_status, launch_root)
                    before = {e.path for e in entries}
            except Exception:
                _logger.warning(
                    "shell mutation scope: pre-status failed", exc_info=True
                )
                before = None
        result = await run()
        if before is not None:
            try:
                entries = await asyncio.to_thread(git_ops.git_status, launch_root)
                after = {e.path for e in entries}
                journal = self._journal()
                if journal is not None:
                    for rel in sorted(after - before):
                        journal.add(rel)
            except Exception:
                _logger.warning(
                    "shell mutation scope: post-status failed", exc_info=True
                )
        return result

    async def aexec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return await self._atrack_shell_delta(
            lambda: self._inner.aexec(
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    async def aexec_raw(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        """Сырой канал тоже меняет дерево (mkdir/mktemp/rm внутренних
        помощников), поэтому дельта журналируется так же, как у ``aexec``."""
        from gc_core.backends.async_protocol import arun_backend_exec_raw

        return await self._atrack_shell_delta(
            lambda: arun_backend_exec_raw(
                self._inner,
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    async def aexec_argv(
        self,
        argv: list,
        *,
        stdin: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return await self._atrack_shell_delta(
            lambda: self._inner.aexec_argv(
                argv,
                stdin=stdin,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    async def arun_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        # `capture_max_chars` доезжает до внутреннего бэкенда, как у `aexec` и
        # `aexec_argv` этой же обёртки. Без него потолок захвата молча
        # подменялся дефолтом бэкенда: вызывающий просил один, получал другой.
        return await self._atrack_shell_delta(
            lambda: self._inner.arun_python(
                code,
                cwd=cwd,
                timeout=timeout,
                env=env,
                capture_max_chars=capture_max_chars,
            )
        )

    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        await self._atrack_text_mutation(path)
        await self._inner.write(path, content, encoding=encoding)

    async def write_bytes(self, path: str, data: bytes) -> None:
        if not classify_content_from_sample(path, data[:BINARY_SAMPLE_BYTES]).is_binary:
            await self._atrack_text_mutation(path)
        await self._inner.write_bytes(path, data)

    async def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        await self._atrack_text_mutation(path)
        await self._inner.append(path, content, encoding=encoding)

    async def copy(self, src: str, dst: str) -> None:
        await self._inner.copy(src, dst)

    async def makedirs(self, path: str) -> None:
        await self._inner.makedirs(path)

    async def awarm_capabilities(self) -> None:
        # Пробы — свойство ВНУТРЕННЕГО бэкенда: журналу diff тут отслеживать
        # нечего, а кэш обязан лечь именно на него, иначе обёртка прогреет
        # свой и первый поиск снова заплатит за пробы.
        #
        # Синхронного близнеца больше нет — как и у самих проб.
        await self._inner.awarm_capabilities()

    def default_cwd(self) -> Optional[str]:
        return self._inner.default_cwd()

    async def exists(self, path: str) -> bool:
        return await self._inner.exists(path)

    async def lexists(self, path: str) -> bool:
        return await self._inner.lexists(path)

    async def is_file(self, path: str) -> bool:
        return await self._inner.is_file(path)

    async def is_dir(self, path: str) -> bool:
        return await self._inner.is_dir(path)

    async def is_symlink(self, path: str) -> bool:
        return await self._inner.is_symlink(path)

    async def getsize(self, path: str) -> int:
        return await self._inner.getsize(path)

    async def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        return await self._inner.stat_info(path, follow_symlinks=follow_symlinks)

    async def readable(self, path: str) -> bool:
        return await self._inner.readable(path)

    async def writable(self, path: str) -> bool:
        return await self._inner.writable(path)

    async def realpath(self, path: str) -> str:
        return await self._inner.realpath(path)

    async def is_hidden(self, path: str) -> bool:
        return await self._inner.is_hidden(path)

    async def read(self, path: str) -> str:
        return await self._inner.read(path)

    async def read_bytes(self, path: str) -> bytes:
        return await self._inner.read_bytes(path)

    async def iter_lines(self, path: str) -> Iterable[str]:
        async for line in self._inner.iter_lines(path):
            yield line

    async def read_head_bytes(self, path: str, n: int) -> bytes:
        return await self._inner.read_head_bytes(path, n)

    async def sha256_bytes(self, path: str) -> str:
        return await self._inner.sha256_bytes(path)

    async def listdir(self, path: str) -> list:
        return await self._inner.listdir(path)

    async def walk(self, path: str) -> list:
        return await self._inner.walk(path)

    async def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        async for item in self._inner.iter_walk(path, should_descend=should_descend):
            yield item

    async def walk_entries(self, path: str, *, prune_names=(), prune_dot_dirs: bool = False) -> list:
        return await self._inner.walk_entries(
            path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
        )

    async def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names=(),
        prune_dot_dirs: bool = False,
    ):
        async for entry in self._inner.iter_walk_entries(
            path,
            should_descend=should_descend,
            prune_names=prune_names,
            prune_dot_dirs=prune_dot_dirs,
        ):
            yield entry

    async def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        async for item in self._inner.iter_files(path, should_descend=should_descend):
            yield item

    def normpath(self, path: str) -> str:
        return self._inner.normpath(path)

    def join(self, base: str, name: str) -> str:
        return self._inner.join(base, name)

    def isabs(self, path: str) -> bool:
        return self._inner.isabs(path)
