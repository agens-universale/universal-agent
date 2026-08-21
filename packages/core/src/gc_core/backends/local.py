"""Host implementation of FsBackend (os / open() / subprocess); the host-I/O
details that used to live inline in the tools are concentrated here."""
import asyncio
import codecs
import hashlib
import os
import stat as stat_module
import subprocess
import time
import sys
from typing import Any, Callable, Dict, Iterable, Optional

from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS, ExecResult, FsBackend
from gc_core.utils.file_text import encode_text_for_append, encode_text_for_write, read_text_for_edit


class LocalFsBackend(FsBackend):
    is_sandbox = False
    default_exec_timeout = None

    def __init__(
        self,
        *,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ) -> None:
        self.exec_output_capture_max_chars = exec_output_capture_max_chars

    # --- stat ---
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def lexists(self, path: str) -> bool:
        return os.path.lexists(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_symlink(self, path: str) -> bool:
        return os.path.islink(path)

    def getsize(self, path: str) -> int:
        return os.path.getsize(path)

    def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        st = os.stat(path, follow_symlinks=follow_symlinks)
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mode": st.st_mode,
            "is_file": stat_module.S_ISREG(st.st_mode),
            "is_dir": stat_module.S_ISDIR(st.st_mode),
            "is_symlink": os.path.islink(path),
        }

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def writable(self, path: str) -> bool:
        return os.access(path, os.W_OK)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def is_hidden(self, path: str) -> bool:
        basename = os.path.basename(path)
        if os.name == "nt":
            try:
                attrs = os.stat(path).st_file_attributes  # Windows-specific
                return bool(attrs & stat_module.FILE_ATTRIBUTE_HIDDEN)
            except AttributeError:
                return basename.startswith(".")
            except Exception:
                return False
        return basename.startswith(".")

    # --- read ---
    def read(self, path: str) -> str:
        return read_text_for_edit(path)

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def iter_lines(self, path: str) -> Iterable[str]:
        # Preserve raw line endings; same codec as read_text_for_edit.
        text = self.read(path)
        yield from text.splitlines(keepends=True)

    def read_head_bytes(self, path: str, n: int) -> bytes:
        with open(path, "rb") as f:
            return f.read(n)

    def sha256_bytes(self, path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    # --- write ---
    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Allowlist + strict encode → atomic bytes write (no errors=replace).
        self.write_bytes(path, encode_text_for_write(content, encoding))

    def write_bytes(self, path: str, data: bytes) -> None:
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "wb") as fd:
                fd.write(data)
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

    def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Append payload WITHOUT a leading BOM: a second BOM in the middle of an
        # existing utf-16 file decodes as U+FEFF (ZERO WIDTH NO-BREAK SPACE).
        with open(path, "ab") as fd:
            fd.write(encode_text_for_append(content, encoding))

    def copy(self, src: str, dst: str) -> None:
        with open(src, "rb") as s, open(dst, "wb") as d:
            d.write(s.read())

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    # --- traverse ---
    def listdir(self, path: str) -> list:
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)  # lstat: symlink-to-dir counts as file (base parity)
                is_dir = stat_module.S_ISDIR(st.st_mode)
            except OSError:
                is_dir = False
            entries.append((name, is_dir))
        return entries

    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        """Stream top-down DFS via ``os.walk`` (dirs first, then files per level)."""
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, path)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"

            for d in dirnames:
                full = os.path.join(dirpath, d)
                try:
                    st = os.lstat(full)
                    is_dir = stat_module.S_ISDIR(st.st_mode)
                except OSError:
                    is_dir = False
                yield (prefix + d, is_dir)

            for f in filenames:
                yield (prefix + f, False)

            if should_descend is not None:
                dirnames[:] = [
                    d for d in dirnames
                    if should_descend(os.path.join(dirpath, d))
                ]

    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        """Stream file relpaths in native ``os.walk`` filename order."""
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, path)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            for f in filenames:
                yield prefix + f

            if should_descend is not None:
                dirnames[:] = [
                    d for d in dirnames
                    if should_descend(os.path.join(dirpath, d))
                ]

    def walk(self, path: str) -> list:
        return list(self.iter_walk(path))

    # --- path semantics ---
    def normpath(self, path: str) -> str:
        return os.path.normpath(path)

    def join(self, base: str, name: str) -> str:
        return os.path.join(base, name)

    # --- exec ---
    def exec(self, command: str, *, cwd: Optional[str] = None,
             timeout: Optional[int] = None,
             env: Optional[dict] = None) -> ExecResult:
        # Реализация одна — асинхронная (см. _arun_bounded_process): она не
        # держит по два дренажных потока на вызов. Синхронный вход остаётся
        # блокирующим, как и был.
        return run_coro_blocking(self.aexec(command, cwd=cwd, timeout=timeout, env=env))

    def exec_argv(self, argv: list, *, stdin: Optional[str] = None,
                  cwd: Optional[str] = None,
                  timeout: Optional[int] = None,
                  env: Optional[dict] = None) -> ExecResult:
        # Shell-less on purpose: the base-class default builds a POSIX shell
        # string, which breaks under Windows cmd.exe (no printf, single quotes
        # not honored). An argv list + stdin goes through CreateProcess /
        # execvp uniformly on every OS.
        return run_coro_blocking(
            self.aexec_argv(argv, stdin=stdin, cwd=cwd, timeout=timeout, env=env)
        )

    def run_python(self, code: str, *, cwd: Optional[str] = None,
                   timeout: Optional[int] = None,
                   env: Optional[dict] = None) -> ExecResult:
        return run_coro_blocking(
            self.arun_python(code, cwd=cwd, timeout=timeout, env=env)
        )

    # --- exec (async) ---
    async def aexec(self, command: str, *, cwd: Optional[str] = None,
                    timeout: Optional[int] = None,
                    env: Optional[dict] = None) -> ExecResult:
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return await _arun_bounded_process(
            command, shell=True, cwd=cwd, timeout=timeout, env=merged_env,
            stdin_text=None, capture_max_chars=self.exec_output_capture_max_chars,
        )

    async def aexec_argv(self, argv: list, *, stdin: Optional[str] = None,
                         cwd: Optional[str] = None,
                         timeout: Optional[int] = None,
                         env: Optional[dict] = None) -> ExecResult:
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return await _arun_bounded_process(
            [str(a) for a in argv], shell=False, cwd=cwd, timeout=timeout,
            env=merged_env, stdin_text=stdin,
            capture_max_chars=self.exec_output_capture_max_chars,
        )

    async def arun_python(self, code: str, *, cwd: Optional[str] = None,
                          timeout: Optional[int] = None,
                          env: Optional[dict] = None) -> ExecResult:
        return await _arun_bounded_process(
            [sys.executable, "-u", "-"], shell=False, cwd=cwd, timeout=timeout,
            env=_sanitize_env(env), stdin_text=code,
            capture_max_chars=self.exec_output_capture_max_chars,
        )

    def default_cwd(self) -> Optional[str]:
        return os.getcwd()


# --- bounded process runner (spec D1/D2/D6/D9) ---
# Потоки stdout/stderr читаются параллельно (thread-per-pipe), иначе большой
# вывод в один pipe заблокирует другой (deadlock). Буфер ≤ capture_max_chars на
# поток; после cap — drain & discard (НЕ kill). Без записи на диск.

_READ_CHUNK = 8192
# После proc.wait: ждём только публикацию captured (≤60k). Обычно уже готово
# (hit_cap по ходу) или EOF по мелкому остатку в pipe — мгновенно.
_CAPTURED_READY_TIMEOUT_SEC = 30
# Discard после cap — fire-and-forget: данные уже в sink. Короткий join, чтобы
# не копить зомби-треды на типичном хвосте; если discard долгий — уходим с
# captured, reader дочитает в фоне как daemon (не блокирует process exit).
_DRAIN_JOIN_TIMEOUT_SEC = 2







# --- асинхронный запуск процесса (async-переезд, Задача 18) -----------------
#
# Прежняя реализация держала ПО ДВА потока на каждый вызов: pipe нельзя просто
# не читать — заполненный буфер заблокирует запись в дочернем процессе, а
# читать оба потока из одного треда нельзя, не рискуя дедлоком. asyncio решает
# ту же задачу двумя корутинами на одном loop.
#
# Семантика сохранена дословно: буфер ограничен, хвост дочитывается в никуда
# (иначе процесс встанет на записи), таймаут убивает процесс и отдаёт частичный
# буфер с ``return_code=None``.


class _Capture:
    """Захваченное из одного pipe, доступное ДО конца дренажа.

    Синхронный ``_drain_pipe`` публиковал буфер через колбэк ``on_captured``
    сразу по достижении лимита и не заставлял вызывающего ждать конца
    отбрасывания хвоста. Асинхронный двойник возвращал результат только
    через ``task.result()`` — то есть при отмене или таймауте дренажа всё
    захваченное терялось. Держим его в объекте, переживающем отмену таски.
    """

    __slots__ = ("text", "hit_cap")

    def __init__(self) -> None:
        self.text = ""
        self.hit_cap = False


async def _adrain_stream(stream, capture_max_chars: int, sink: "_Capture") -> None:
    """Асинхронный двойник ``_drain_pipe``: то же ограничение, тот же декодер.

    Пишет в ``sink`` по ходу дела: по достижении лимита — сразу, иначе — на
    EOF. Отмена таски после этого уже не отнимает захваченное.
    """
    decoder = codecs.lookup("utf-8").incrementaldecoder(errors="replace")
    parts: list = []
    total = 0
    hit_cap = False
    try:
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            if hit_cap:
                # drain & discard: не читать нельзя — pipe заблокирует процесс.
                continue
            decoded = decoder.decode(chunk)
            room = capture_max_chars - total
            if room <= 0:
                hit_cap = True
            elif len(decoded) <= room:
                parts.append(decoded)
                total += len(decoded)
            else:
                parts.append(decoded[:room])
                total = capture_max_chars
                hit_cap = True
            if hit_cap:
                # Публикуем немедленно: дальше только отбрасывание хвоста, и
                # ждать его вызывающему незачем.
                sink.text = "".join(parts)
                sink.hit_cap = True
    except asyncio.CancelledError:
        # Отмена по таймауту дренажа: отдаём то, что уже прочли, и уходим.
        if not hit_cap:
            sink.text = "".join(parts)
        raise
    except Exception:
        if not hit_cap:
            sink.text = "".join(parts)
        return
    if not hit_cap:
        parts.append(decoder.decode(b"", final=True))
        sink.text = "".join(parts)
        sink.hit_cap = False


async def _await_process_exit(proc, timeout: Optional[float]):
    """Дождаться ВЫХОДА процесса, а не EOF его pipe'ов.

    ``Process.wait()`` резолвится только когда закрыты все пайпы (asyncio
    будит ожидающих из ``_call_connection_lost``). Команда, оставившая
    фонового потомка с унаследованным stdout, держит pipe открытым — и такой
    wait выжигал весь таймаут, отдавая ложный ``timed_out`` и теряя вывод,
    который команда уже успела напечатать. Синхронный ``Popen.wait()``
    возвращался по выходу процесса независимо от пайпов; сохраняем это.

    ``proc.returncode`` выставляется в ``_process_exited`` — раньше закрытия
    пайпов и независимо от него.
    """
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    delay = 0.001
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(delay)
        # Растим паузу: короткие команды ловим почти мгновенно, длинные не
        # заставляют loop просыпаться сотни раз в секунду.
        delay = min(delay * 2, 0.05)
    return proc.returncode


async def _arun_bounded_process(
    args,
    *,
    shell: bool,
    cwd: Optional[str],
    timeout: Optional[int],
    env: Optional[Dict[str, str]],
    stdin_text: Optional[str],
    capture_max_chars: int,
) -> ExecResult:
    """Запустить процесс с ограниченным буфером, не занимая ни одного потока."""
    from gc_core.utils.win_console import preserve_console_mode

    stdin_arg = (
        asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
    )
    with preserve_console_mode():
        if shell:
            proc = await asyncio.create_subprocess_shell(
                args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=stdin_arg, cwd=cwd, env=env, limit=_READ_CHUNK * 8,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=stdin_arg, cwd=cwd, env=env, limit=_READ_CHUNK * 8,
            )

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    out_sink, err_sink = _Capture(), _Capture()
    out_task = asyncio.ensure_future(
        _adrain_stream(proc.stdout, capture_max_chars, out_sink)
    )
    err_task = asyncio.ensure_future(
        _adrain_stream(proc.stderr, capture_max_chars, err_sink)
    )

    timed_out = False
    try:
        try:
            await _await_process_exit(proc, timeout)
            return_code = proc.returncode
        except asyncio.TimeoutError:
            timed_out = True
            return_code = None
            _kill_quietly(proc)
            try:
                await _await_process_exit(proc, 5)
            except Exception:
                pass

        # Хвост pipe дочитываем ограниченно: долгий discard не должен удерживать
        # tool-call — тот же приём, что у синхронного join с таймаутом.
        _done, pending = await asyncio.wait(
            {out_task, err_task}, timeout=_DRAIN_JOIN_TIMEOUT_SEC
        )
        for task in pending:
            task.cancel()
        if pending:
            # Дожидаемся отмены: cancel() только ПРОСИТ прекратить, а забрать
            # прочитанное успевает лишь except-ветка внутри корутины — без
            # этого захваченное терялось ровно там, где мы его спасаем.
            await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        # Вызывающего сняли (сервер отменил прогон, упал сосед по TaskGroup) —
        # без этого процесс и обе дренажные корутины пережили бы отмену.
        _kill_quietly(proc)
        for task in (out_task, err_task):
            task.cancel()
        raise

    # Читаем из sink, а НЕ из task.result(): отменённая по таймауту дренажа
    # таска результата не отдаст, но захваченное к тому моменту уже там.
    return ExecResult(
        stdout=out_sink.text,
        stderr=err_sink.text,
        return_code=return_code,
        timed_out=timed_out,
        stdout_hit_capture_cap=out_sink.hit_cap,
        stderr_hit_capture_cap=err_sink.hit_cap,
    )


def _kill_quietly(proc) -> None:
    try:
        proc.kill()
    except Exception:
        pass


# --- python-subprocess env allowlist (moved from run_python_code.py) ---
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LOGNAME",
        "USER",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "SHELL",
        "TERM",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        "PATH",
        "PATHEXT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "no_proxy",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "SYSTEMROOT",
        "WINDIR",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "APPDATA",
        "LOCALAPPDATA",
        "PUBLIC",
    }
)


def _child_env_from_parent() -> Dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k in _CHILD_ENV_ALLOWLIST and isinstance(v, str)
    }


def _sanitize_env(env: Optional[Dict[str, Any]]) -> Dict[str, str]:
    base_env = {**_child_env_from_parent(), "PYTHONPATH": ""}
    if env is None:
        return base_env
    if not isinstance(env, dict):
        return base_env
    sanitized: Dict[str, str] = {}
    for k, v in env.items():
        if k is None:
            continue
        key = k if isinstance(k, str) else str(k)
        if v is None:
            sanitized[key] = ""
        elif isinstance(v, str):
            sanitized[key] = v
        else:
            sanitized[key] = str(v)
    merged = {**base_env, **sanitized}
    merged["PYTHONPATH"] = ""
    return merged
