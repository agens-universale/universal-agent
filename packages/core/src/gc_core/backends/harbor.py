"""Coroutine FsBackend over an async Harbor BaseEnvironment.

Harbor's environment is bound to the adapter's event loop. Every operation here is a
coroutine: on that loop it awaits the environment directly, from any other loop (the
agent's own ``asyncio.run``) it schedules the call there and awaits the result via
``wrap_future`` — no thread is blocked either way (see ``_aawait``).
File ops mirror deepagents' HarborSandbox: write/edit use native upload/download,
read/list use shell through exec.
"""
import asyncio
import base64
import contextlib
import posixpath
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Optional, Sequence

from gc_core.backends.capture import BoundedStreamAccumulator
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    argv_to_posix_command,
    FIND_PRINTF_FORMAT,
    WALK_OUTPUT_MAX_CHARS,
    ExecResult,
    FsBackend,
    WalkOutputTooLargeError,
    bound_exec_result,
)
from gc_core.backends.capabilities import parse_find_printf_entries
from gc_core.backends.walk_util import iter_files_from_flat, iter_walk_from_flat
from gc_core.utils.file_text import (
    decode_text_best_effort,
    encode_text_for_append,
    encode_text_for_write,
)
from gc_core.utils.native_fs_search import find_prune_group, find_walk_head


# Встроенная политика ``harbor_exec_init: bashrc``. Неинтерактивный ``bash -c``
# иначе пропускает bashrc (``[ -z "$PS1" ] && return``). Потоки профиля
# глушатся, чтобы conda-хуки не попали в stdout/stderr команды; ``set +e``
# после source — чтобы ``set -e`` / ``return`` в rc не подменили код возврата
# пользовательской команды. Имя окружения не зашито. Из develop, как есть.
#: Потолок на один вопрос «где мы» при сборке контекста. Со стороны ядра
#: дедлайна нет, и без него медленный контейнер задерживал бы сборку сколько
#: угодно; сам ответ приходит за миллисекунды.
_CWD_PRIME_TIMEOUT_SEC = 20

_BASHRC_EXEC_INIT = """\
PS1=${PS1-dummy}
. /etc/profile >/dev/null 2>&1 || true
[ -f "${HOME}/.bashrc" ] && . "${HOME}/.bashrc" >/dev/null 2>&1 || true
set +e
"""


def _env_assignment_prefix(env: Optional[dict]) -> str:
    if not env:
        return ""
    assignments = " ".join(
        f"{k}={shlex.quote(str(v))}" for k, v in env.items()
    )
    return f"env {assignments} "


def build_harbor_shell_command(
    command: str,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    exec_init: Optional[str] = None,
) -> str:
    """Строка, которую Harbor ``environment.exec`` обернёт в ``bash -c``.

    С инициализацией: ``cd cwd && env K=V bash -c <init+command>``. Без неё
    команда остаётся сырой. ``cd`` стоит снаружи ``env``, чтобы ``env_vars`` не
    проглотили cwd бэкенда (R7).
    """
    if exec_init:
        init = _BASHRC_EXEC_INIT if exec_init == "bashrc" else exec_init.rstrip() + "\n"
        body = "bash -c " + shlex.quote(init + command)
    else:
        body = command
    prefix = _env_assignment_prefix(env)
    if prefix:
        body = prefix + body
    if cwd:
        return f"cd {shlex.quote(cwd)} && {body}"
    return body


def _build_find_walk_command(
    path: str,
    *,
    prune_names: Sequence[str] = (),
    prune_dot_dirs: bool = False,
) -> str:
    """``find -H PATH -mindepth 1 [( prunes ) -printf FMT -prune -o] -printf FMT``.

    A pruned directory is printed BEFORE ``-prune``: the walk lists it (name
    search finds ``node_modules`` itself), only its subtree is cut.
    """
    argv = find_walk_head(path)
    group = find_prune_group(exclude_dir_names=prune_names, exclude_nested_dot_dirs=prune_dot_dirs)
    if group:
        argv.extend(group + ["-printf", FIND_PRINTF_FORMAT, "-prune", "-o"])
    argv.extend(["-printf", FIND_PRINTF_FORMAT])
    return " ".join(shlex.quote(a) for a in argv)


class HarborBackend(FsBackend):
    is_sandbox = True
    # Файлы живут в контейнере: `exists()` это `test -e` по сети, `read()` —
    # `env.download_file`. Прежний флаг `bridges_to_loop` снят вместе с
    # примесью `AsyncFileOps`, которая его читала: развилки «инлайн или поток»
    # больше нет — операции корутинные и ждут через `_aawait`, то есть на loop
    # владельца и без потока.
    # Дефолт берётся из agent.exec_timeout_sec (harbor bench_defaults → 600).
    default_exec_timeout = None

    def __init__(
        self,
        environment: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
        walk_output_max_chars: int = WALK_OUTPUT_MAX_CHARS,
        exec_init: Optional[str] = None,
    ) -> None:
        self._env = environment
        self._loop = loop
        self.exec_output_capture_max_chars = exec_output_capture_max_chars
        self.walk_output_max_chars = walk_output_max_chars
        # Инициализация окружения для канала МОДЕЛИ (`aexec`): None — сырой
        # shell, "bashrc" — встроенный bootstrap, иначе свой префикс.
        self.exec_init = exec_init
        # WORKDIR контейнера, узнанный `aprime_default_cwd`; до того — None.
        self._discovered_cwd: Optional[str] = None

    async def _aawait(self, coro):
        """То же из асинхронного кода — не занимая ни поток, ни event loop.

        ``self._env`` привязан к loop Harbor, и прямой ``await`` верен только
        если мы на нём же. Мы можем быть на другом (агент со своим
        ``asyncio.run``), поэтому корутина ставится на loop владельца, а
        результат ждётся через ``wrap_future``: у вызывающего это обычный
        ``await``, потока не появляется. Замерено — 5 параллельных вызовов,
        ноль новых потоков, худший разрыв loop 11 мс при шаге пульса 10.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return await coro
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        )

    def _capture_cap(self, max_chars: Optional[int]) -> int:
        """``None`` → tool-trace cap (40k/поток), else the per-call ceiling."""
        return (
            self.exec_output_capture_max_chars if max_chars is None else max_chars
        )

    @staticmethod
    def _is_exec_timeout(exc: BaseException) -> bool:
        # Harbor Docker/Apple envs raise RuntimeError("Command timed out after N seconds");
        # also accept bare TimeoutError / asyncio.TimeoutError.
        if isinstance(exc, TimeoutError):
            return True
        return "Command timed out after" in str(exc)

    def _output_capture(
        self, cap: int
    ) -> tuple[ContextManager[None], dict[str, "BoundedStreamAccumulator"]]:
        """Scope Harbor's public ``scoped_output_callback`` around one exec and
        feed the streamed chunks into a bounded head+tail accumulator per stream.

        Returns ``(context_manager, accumulators)``. Without the hook (env does
        not stream) the context is a no-op and the accumulators stay empty —
        empty output on timeout is legal.

        Chunks keep being consumed after the head is full: they are still needed
        for ``total_chars`` and for the ring tail. The accumulator
        runs in the agent's own Python process — nothing is required inside the
        task container.
        """
        accumulators = {
            "stdout": BoundedStreamAccumulator(cap),
            "stderr": BoundedStreamAccumulator(cap),
        }
        scope = getattr(self._env, "scoped_output_callback", None)
        if scope is None:
            return contextlib.nullcontext(), accumulators

        async def on_output(text: str, stream: str) -> None:
            acc = accumulators.get(stream)
            if acc is not None:
                acc.feed(text)

        return scope(on_output), accumulators

    # --- shell ---
    @staticmethod
    def _timed_out_result(streamed: dict, cap: int) -> ExecResult:
        """Таймаут = частичное выполнение: то, что команда успела напечатать.

        Идёт тем же потоком вывода, которым Harbor пишет логи прогона: само
        исключение таймаута вывода не несёт.
        """
        return bound_exec_result(
            ExecResult(
                return_code=None,
                timed_out=True,
                stdout_stream=streamed["stdout"].result(),
                stderr_stream=streamed["stderr"].result(),
            ),
            cap,
        )

    @staticmethod
    def _completed_result(res: Any, cap: int) -> ExecResult:
        return bound_exec_result(
            ExecResult(
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                return_code=res.return_code,
                timed_out=bool(getattr(res, "timed_out", False)),
            ),
            cap,
        )

    async def _aexec_impl(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
        exec_init: Optional[str] = None,
    ) -> ExecResult:
        """Транспорт. ``exec_init`` — только для канала модели (``aexec``).

        Сборка строки — ``build_harbor_shell_command``. Внутренние помощники,
        обход, argv и python зовут транспорт без инициализации: сырой канал.
        """
        full = build_harbor_shell_command(
            command, cwd=cwd, env=env, exec_init=exec_init
        )
        cap = self._capture_cap(capture_max_chars)
        scope, streamed = self._output_capture(cap)
        try:
            with scope:
                res = await self._aawait(self._env.exec(full, timeout_sec=timeout))
        except Exception as exc:
            if not self._is_exec_timeout(exc):
                raise
            return self._timed_out_result(streamed, cap)
        return self._completed_result(res, cap)

    @staticmethod
    def _python_command(code: str) -> str:
        """Quoted heredoc → тело кода берётся буквально."""
        return f"python3 - <<'GC_HARBOR_PY_EOF'\n{code}\nGC_HARBOR_PY_EOF"

    async def _afs_exec(self, command: str) -> ExecResult:
        """Internal FS helper (stat/head/mkdir). Walk ceiling, not the tool-trace cap.

        ЕДИНСТВЕННАЯ форма. Синхронная (`_fs_exec`) снята вместе с `_exec` и
        `_await`: у Harbor файловая операция — это сеть, и ждала она через
        ``run_coroutine_threadsafe(...).result()``. На loop самого Harbor это
        не «блокировка потока», а самоблокировка: поток loop'а ждёт корутину,
        которую выполнить может только он же. Двенадцать ``async def``
        методов (``exists``, ``lexists``, ``is_symlink``, ``realpath``,
        ``getsize``, ``append``, ``copy``, ``makedirs``, ``read_head_bytes``,
        ``walk_entries``, ``listdir`` и через ``sha256``/``stat`` ещё два)
        звали её напрямую.
        """
        return await self._aexec_impl(
            command, capture_max_chars=self.walk_output_max_chars
        )

    async def aexec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """Shell модели: с инициализацией окружения, если она задана."""
        return await self._aexec_impl(
            command, cwd=cwd, timeout=timeout, env=env,
            capture_max_chars=capture_max_chars,
            exec_init=self.exec_init,
        )

    async def aexec_raw(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """POSIX без профиля: помощники ФС, grep, пробы, MCP."""
        return await self._aexec_impl(
            command, cwd=cwd, timeout=timeout, env=env,
            capture_max_chars=capture_max_chars,
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
    ) -> ExecResult:
        # Та же сборка argv → строка шелла, что у синхронного `exec_argv`
        # (общий `argv_to_posix_command`): у песочницы exec идёт через POSIX-шелл.
        return await self._aexec_impl(
            argv_to_posix_command(argv, stdin=stdin),
            cwd=cwd, timeout=timeout, env=env,
            capture_max_chars=capture_max_chars,
        )

    async def arun_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        return await self._aexec_impl(
            self._python_command(code), cwd=cwd, timeout=timeout, env=env,
            capture_max_chars=capture_max_chars,
        )

    # --- file read (shell / download) ---
    async def read(self, path: str) -> str:
        # Raw bytes via download, then same codec as local ``read_text_for_edit``.
        if not await self.exists(path):
            raise FileNotFoundError(path)
        text = decode_text_best_effort(await self.read_bytes(path))
        if text is not None:
            return text
        raise UnicodeDecodeError(
            "utf-8",
            b"",
            0,
            1,
            f"undecodable or binary file: {path}",
        )

    async def read_bytes(self, path: str) -> bytes:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await self._aawait(self._env.download_file(path, tmp_path))
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

    async def iter_lines(self, path: str) -> Iterable[str]:
        for line in (await self.read(path)).splitlines(keepends=True):
            yield line

    async def read_head_bytes(self, path: str, n: int) -> bytes:
        """Raw file prefix. ``exec`` stdout is text — pipe via base64 to avoid mangling."""
        n = max(0, int(n))
        if n == 0:
            return b""
        # GNU coreutils in harbor images (same assumption as ``stat -c`` / ``find -printf``).
        # Capture at walk ceiling: append samples 100KiB (~137k base64) > tool cap.
        # Do not pass ``-w 0``: Harbor's streamed exec reads stdout with
        # ``StreamReader`` readline (64 KiB). An unwrapped blob is one line and
        # raises LimitOverrunError before we see stdout. Default wrap is 76
        # columns; ``b64decode(validate=False)`` ignores the newlines.
        res = await self._afs_exec(f"head -c {n} {shlex.quote(path)} | base64")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"head failed: {path}")
        if res.stdout_hit_capture_cap:
            raise OSError(f"head output exceeded capture ceiling: {path}")
        try:
            return base64.b64decode(res.stdout or "", validate=False)
        except Exception as exc:
            raise OSError(f"base64 decode failed for head of {path}") from exc

    async def sha256_bytes(self, path: str) -> str:
        return await self.sha256(path)

    # --- stat: native BaseEnvironment handles where they exist, shell otherwise ---
    async def exists(self, path: str) -> bool:
        return (await self._afs_exec(f"test -e {shlex.quote(path)}")).return_code == 0

    async def lexists(self, path: str) -> bool:
        return (await self._afs_exec(f"test -e {shlex.quote(path)} || test -L {shlex.quote(path)}")).return_code == 0

    async def is_file(self, path: str) -> bool:
        return await self._aawait(self._env.is_file(path))

    async def is_dir(self, path: str) -> bool:
        return await self._aawait(self._env.is_dir(path))

    async def is_symlink(self, path: str) -> bool:
        return (await self._afs_exec(f"test -L {shlex.quote(path)}")).return_code == 0

    async def realpath(self, path: str) -> str:
        res = await self._afs_exec(f"readlink -f {shlex.quote(path)}")
        return res.stdout.strip() if res.return_code == 0 else path

    async def readable(self, path: str) -> bool:
        return True   # container-root perspective (current harbor pack behavior)

    async def writable(self, path: str) -> bool:
        return True

    async def is_hidden(self, path: str) -> bool:
        return path.rstrip("/").rsplit("/", 1)[-1].startswith(".")

    async def getsize(self, path: str) -> int:
        res = await self._afs_exec(f"stat -c %s {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"stat failed: {path}")
        return int(res.stdout.strip())

    async def sha256(self, path: str) -> str:
        res = await self._afs_exec(f"sha256sum {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"sha256sum failed: {path}")
        return res.stdout.split()[0]

    async def stat(self, path: str) -> dict:
        res = await self._afs_exec(f"stat -c '%s|%Y|%a|%F' {shlex.quote(path)}")
        if res.return_code != 0:
            raise FileNotFoundError(path)
        size, mtime, mode, kind = res.stdout.strip().split("|", 3)
        return {
            "size": int(size), "mtime": int(mtime), "mode": mode,
            "is_file": kind == "regular file", "is_dir": kind == "directory",
            "is_symlink": "symbolic link" in kind,
        }

    async def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        st = await self.stat(path)  # existing stat -c '%s|%Y|%a|%F' wrapper
        return {
            "size": st["size"], "mtime": st["mtime"],
            "mode": int(str(st["mode"]), 8),
            "is_file": st["is_file"], "is_dir": st["is_dir"], "is_symlink": st["is_symlink"],
        }

    async def walk_entries(
        self,
        path: str,
        *,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> list:
        """Recursive listing with sizes (``find -printf %s``) via the private
        traverse channel (50M chars). Pruned dirs are cut inside ``find`` so the
        channel is not spent on junk trees. Hit ceiling → fail-closed.
        """
        res = await self._aexec_impl(
            _build_find_walk_command(
                path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
            ),
            capture_max_chars=self.walk_output_max_chars,
        )
        if res.return_code != 0:
            raise FileNotFoundError(path)
        if res.stdout_hit_capture_cap:
            raise WalkOutputTooLargeError(
                path, ceiling_chars=self.walk_output_max_chars
            )
        return parse_find_printf_entries(res.stdout or "")

    async def walk(self, path: str) -> list:
        """Recursive listing under `path` as [(relpath, is_dir)] (relpath excludes path)."""
        return [(e.rel, e.is_dir) for e in await self.walk_entries(path)]

    async def listdir(self, path: str) -> list:
        """Immediate children via private traverse channel (same ceiling as walk)."""
        # Тот же общий рецепт, что у обхода: без -H find не идёт по ссылке-аргументу,
        # и каталог-ссылка выглядел пустым (пустой stdout при коде возврата 0).
        head = " ".join(shlex.quote(a) for a in find_walk_head(path))
        res = await self._aexec_impl(
            f"{head} -maxdepth 1 -printf '%f\\t%y\\n'",
            capture_max_chars=self.walk_output_max_chars,
        )
        if res.return_code != 0:
            raise FileNotFoundError(path)
        if res.stdout_hit_capture_cap:
            raise WalkOutputTooLargeError(
                path, ceiling_chars=self.walk_output_max_chars
            )
        out = []
        for line in (res.stdout or "").splitlines():
            if not line.strip():
                continue
            name, _, kind = line.partition("\t")
            out.append((name, kind[:1] == "d" if kind else False))
        return sorted(out)

    async def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        """DFS from flat ``find`` listing (materialized ``walk()``)."""
        for _item in iter_walk_from_flat(
            await self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        ):
            yield _item

    async def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        for _item in iter_files_from_flat(
            await self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        ):
            yield _item

    # --- path semantics ---
    def normpath(self, path: str) -> str:
        # find печатает пути префиксом вызова, prune сравнивает строки — «//» и
        # «/./» в префиксе молча ломали исключения. Пути песочницы всегда POSIX.
        return posixpath.normpath(path) if path else path

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    def isabs(self, path: str) -> bool:
        """Container paths are POSIX whatever the host is."""
        return path.startswith("/")

    def default_cwd(self) -> Optional[str]:
        # WORKDIR контейнера: узнаётся один раз при сборке контекста
        # (`aprime_default_cwd`), до того — None.
        return self._discovered_cwd

    async def aprime_default_cwd(self) -> Optional[str]:
        """Узнать WORKDIR контейнера один раз — корутиной, сырым каналом.

        Раньше рабочую папку спрашивал синхронный ``discover_backend_cwd``
        через ``exec("pwd")``. Синхронного ``exec`` у Harbor больше нет,
        умолчание порта отказывает, отказ глотался — и корнем рабочей папки
        становился ``/`` вместо WORKDIR. Синхронных сборщиков рабочей области
        восемь (включая конструктор ``State``), каскадом в корутины их не
        переводим: как и пробы возможностей, значение прогревается один раз
        при сборке контекста и дальше отдаётся из кэша.
        """
        if self._discovered_cwd is None:
            try:
                # Таймаут обязателен: без него сборка контекста ждала бы медленный
                # контейнер сколько угодно — со стороны ядра дедлайна нет.
                res = await self._aexec_impl("pwd", timeout=_CWD_PRIME_TIMEOUT_SEC)
            except Exception:  # noqa: BLE001 — окружение может быть недоступно
                return None
            lines = (res.stdout or "").strip().splitlines()
            if res.return_code == 0 and lines and lines[0].startswith("/"):
                self._discovered_cwd = lines[0]
        return self._discovered_cwd

    # --- file write/edit (native transfer) ---
    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Allowlist + strict encode on host → bytes upload (same path as write_bytes).
        await self.write_bytes(path, encode_text_for_write(content, encoding))

    async def write_bytes(self, path: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            await self._aawait(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            await self._aawait(self._env.upload_file(tmp_path, path))
        finally:
            tmp_path.unlink(missing_ok=True)

    async def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Upload only the new chunk, then shell-append (no full-file download).
        # Append payload WITHOUT a leading BOM: a second BOM in the middle of an
        # existing utf-16 file decodes as U+FEFF (ZERO WIDTH NO-BREAK SPACE).
        data = encode_text_for_append(content, encoding)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            local_tmp = Path(tmp.name)
        remote_tmp = f"{path}.gc-append-tmp.{uuid.uuid4().hex}"
        try:
            await self._aawait(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            await self._aawait(self._env.upload_file(local_tmp, remote_tmp))
            res = await self._afs_exec(
                f"cat {shlex.quote(remote_tmp)} >> {shlex.quote(path)}; "
                f"rm -f {shlex.quote(remote_tmp)}"
            )
            if res.return_code != 0:
                raise OSError(res.stderr.strip() or f"append failed: {path}")
        finally:
            local_tmp.unlink(missing_ok=True)
            # Best-effort: upload/exec may leave remote_tmp if the cat+rm line never ran.
            try:
                await self._afs_exec(f"rm -f {shlex.quote(remote_tmp)}")
            except Exception:
                pass

    async def copy(self, src: str, dst: str) -> None:
        res = await self._afs_exec(f"cp {shlex.quote(src)} {shlex.quote(dst)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"cp failed: {src} -> {dst}")

    async def makedirs(self, path: str) -> None:
        res = await self._afs_exec(f"mkdir -p {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"mkdir failed: {path}")
