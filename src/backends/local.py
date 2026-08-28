"""Host implementation of FsBackend (os / open() / subprocess); the host-I/O
details that used to live inline in the tools are concentrated here."""
import codecs
import hashlib
import os
import stat as stat_module
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ExecResult,
    FsBackend,
)
from backends.walk_util import WalkEntry, pruned_dir_predicate
from utils.file_text import encode_text_for_append, encode_text_for_write, read_text_for_edit


def _is_real_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _size_without_following(entry: os.DirEntry) -> Optional[int]:
    try:
        if entry.is_symlink():
            return None
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None


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
        """``iter_walk_entries`` without sizes (dirs first, then files per level)."""
        for entry in self.iter_walk_entries(path, should_descend=should_descend):
            yield entry.rel, entry.is_dir

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

    def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> Iterable[WalkEntry]:
        """Top-down DFS over ``os.scandir``: dirs first, then files per level (sorted).

        Sizes come from the directory listing (``DirEntry.stat(follow_symlinks=False)``:
        free on Windows, one ``lstat`` elsewhere) — no second stat per file. A
        symlink's size is unknown (``None``), never the link's own length. A pruned
        or non-descendable directory is still listed; only its subtree is skipped.
        Symlinks are never followed (``os.walk(followlinks=False)`` parity).
        """
        pruned = pruned_dir_predicate(prune_names, prune_dot_dirs)

        def _level(abs_dir: str, prefix: str) -> Iterable[WalkEntry]:
            try:
                with os.scandir(abs_dir) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                return
            dirs = [e for e in children if _is_real_dir(e)]
            files = [e for e in children if not _is_real_dir(e)]
            for d in dirs:
                yield WalkEntry(prefix + d.name, True)
            for f in files:
                yield WalkEntry(prefix + f.name, False, _size_without_following(f))
            for d in dirs:
                if pruned(d.name):
                    continue
                full = os.path.join(abs_dir, d.name)
                if should_descend is not None and not should_descend(full):
                    continue
                yield from _level(full, prefix + d.name + "/")

        yield from _level(path, "")

    def walk_entries(
        self,
        path: str,
        *,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> list:
        return list(
            self.iter_walk_entries(path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs)
        )

    # --- path semantics ---
    def normpath(self, path: str) -> str:
        return os.path.normpath(path)

    def join(self, base: str, name: str) -> str:
        return os.path.join(base, name)

    def _exec_capture_max_chars(self, capture_max_chars: Optional[int]) -> int:
        if capture_max_chars is None:
            return self.exec_output_capture_max_chars
        return capture_max_chars

    # --- exec ---
    def exec(self, command: str, *, cwd: Optional[str] = None,
             timeout: Optional[int] = None,
             env: Optional[dict] = None,
             capture_max_chars: Optional[int] = None) -> ExecResult:
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return _run_bounded_process(
            command,
            shell=True,
            cwd=cwd,
            timeout=timeout,
            env=merged_env,
            stdin_text=None,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    def exec_argv(self, argv: list, *, stdin: Optional[str] = None,
                  cwd: Optional[str] = None,
                  timeout: Optional[int] = None,
                  env: Optional[dict] = None,
                  capture_max_chars: Optional[int] = None) -> ExecResult:
        # Shell-less on purpose: the base-class default builds a POSIX shell
        # string, which breaks under Windows cmd.exe (no printf, single quotes
        # not honored). An argv list + stdin goes through CreateProcess /
        # execvp uniformly on every OS.
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return _run_bounded_process(
            [str(a) for a in argv],
            shell=False,
            cwd=cwd,
            timeout=timeout,
            env=merged_env,
            stdin_text=stdin,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    def run_python(self, code: str, *, cwd: Optional[str] = None,
                   timeout: Optional[int] = None,
                   env: Optional[dict] = None) -> ExecResult:
        return _run_bounded_process(
            [sys.executable, "-u", "-"],
            shell=False,
            cwd=cwd,
            timeout=timeout,
            env=_sanitize_env(env),
            stdin_text=code,
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


def _drain_pipe(pipe, capture_max_chars: int, on_captured=None) -> tuple[str, bool]:
    """Прочитать ``pipe`` в строку ≤ ``capture_max_chars``; дальше слить в никуда.

    ``on_captured(text, hit_cap)`` вызывается ровно один раз — сразу после заполнения
    буфера (в т.ч. при hit_cap, *до* конца discard), чтобы caller не ждал drain.

    Возвращает ``(text, hit_cap)``. Инкрементальный UTF-8 декодер (errors=replace)
    безопасно обрабатывает многобайтовые границы чанков.
    """
    decoder = codecs.lookup("utf-8").incrementaldecoder(errors="replace")
    parts: list[str] = []
    total = 0
    hit_cap = False
    published = False

    def _publish() -> None:
        nonlocal published
        if published:
            return
        published = True
        if on_captured is not None:
            on_captured("".join(parts), hit_cap)

    try:
        while True:
            chunk = pipe.read(_READ_CHUNK)
            if not chunk:
                break
            if not hit_cap:
                decoded = decoder.decode(chunk)
                room = capture_max_chars - total
                if room <= 0:
                    hit_cap = True
                    _publish()
                elif len(decoded) <= room:
                    parts.append(decoded)
                    total += len(decoded)
                else:
                    parts.append(decoded[:room])
                    total = capture_max_chars
                    hit_cap = True
                    _publish()
            # после hit_cap продолжаем читать (drain & discard), чтобы pipe не
            # заблокировал процесс и не повесил его запись.
    finally:
        try:
            pipe.close()
        except Exception:
            pass
        # финализируем декодер (flush возможного неполного остатка — только если
        # не hit cap; при hit cap хвост отброшен по определению).
        if not hit_cap:
            parts.append(decoder.decode(b"", final=True))
        _publish()
    return "".join(parts), hit_cap


def _run_bounded_process(
    args,
    *,
    shell: bool,
    cwd: Optional[str],
    timeout: Optional[int],
    env: Optional[Dict[str, str]],
    stdin_text: Optional[str],
    capture_max_chars: int,
) -> ExecResult:
    """Запустить процесс с bounded-буфером на stdout/stderr (thread-per-pipe).

    ``stdin_text`` — опциональный ввод (run_python); ``None`` → stdin=DEVNULL.
    Timeout: kill процесса, ``timed_out=True``, ``return_code=None``, partial-буфер.
    Captured публикуется сразу по заполнению cap — не ждём конца discard.
    """
    from utils.win_console import preserve_console_mode

    stdin_arg = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
    with preserve_console_mode():
        proc = subprocess.Popen(
            args,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin_arg,
            cwd=cwd,
            env=env,
            bufsize=0,
        )

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    stdout_hit = [False]
    stderr_hit = [False]
    stdout_ready = threading.Event()
    stderr_ready = threading.Event()

    def _reader(
        pipe,
        sink: list[str],
        hit_flag: list[bool],
        ready: threading.Event,
    ) -> None:
        def on_captured(text: str, hit: bool) -> None:
            if not sink:
                sink.append(text)
                hit_flag[0] = hit
            ready.set()

        try:
            _drain_pipe(pipe, capture_max_chars, on_captured=on_captured)
        finally:
            if not sink:
                sink.append("")
            ready.set()

    t_out = threading.Thread(
        target=_reader,
        args=(proc.stdout, stdout_buf, stdout_hit, stdout_ready),
        daemon=True,
        name="exec-stdout-reader",
    )
    t_err = threading.Thread(
        target=_reader,
        args=(proc.stderr, stderr_buf, stderr_hit, stderr_ready),
        daemon=True,
        name="exec-stderr-reader",
    )
    t_out.start()
    t_err.start()

    try:
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        proc.wait(timeout=timeout)
        return_code: Optional[int] = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        return_code = None
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    # Captured уже в sink после on_captured — не блокируемся на discard гигабайта.
    stdout_ready.wait(timeout=_CAPTURED_READY_TIMEOUT_SEC)
    stderr_ready.wait(timeout=_CAPTURED_READY_TIMEOUT_SEC)
    # Параллельный короткий join: типичный хвост pipe дочитается; долгий discard
    # не удерживает tool-call (reader продолжит в фоне).
    t_out.join(timeout=_DRAIN_JOIN_TIMEOUT_SEC)
    t_err.join(timeout=_DRAIN_JOIN_TIMEOUT_SEC)

    return ExecResult(
        stdout=stdout_buf[0] if stdout_buf else "",
        stderr=stderr_buf[0] if stderr_buf else "",
        return_code=return_code,
        timed_out=timed_out,
        stdout_hit_capture_cap=stdout_hit[0],
        stderr_hit_capture_cap=stderr_hit[0],
    )


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
