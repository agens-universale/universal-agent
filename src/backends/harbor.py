"""Synchronous FsBackend over an async Harbor BaseEnvironment.

The gc-cli tool loop is synchronous; Harbor's environment is async and bound to the
adapter's event loop. Every method marshals its coroutine onto that loop via
run_coroutine_threadsafe and blocks the (worker-thread) caller until it completes.
File ops mirror deepagents' HarborSandbox: write/edit use native upload/download,
read/list use shell through exec.
"""
import asyncio
import base64
import posixpath
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    FIND_PRINTF_FORMAT,
    WALK_OUTPUT_MAX_CHARS,
    ExecResult,
    FsBackend,
    WalkOutputTooLargeError,
    bound_exec_result,
)
from backends.capabilities import parse_find_printf_entries
from backends.walk_util import iter_files_from_flat, iter_walk_from_flat
from utils.file_text import (
    decode_text_best_effort,
    encode_text_for_append,
    encode_text_for_write,
)
from utils.native_fs_search import find_prune_group, find_walk_head


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
    # Дефолт берётся из agent.exec_timeout_sec (harbor bench_defaults → 600).
    default_exec_timeout = None

    def __init__(
        self,
        environment: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
        walk_output_max_chars: int = WALK_OUTPUT_MAX_CHARS,
    ) -> None:
        self._env = environment
        self._loop = loop
        self.exec_output_capture_max_chars = exec_output_capture_max_chars
        self.walk_output_max_chars = walk_output_max_chars

    def _await(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _bound(
        self, result: ExecResult, *, max_chars: Optional[int] = None
    ) -> ExecResult:
        cap = (
            self.exec_output_capture_max_chars
            if max_chars is None
            else max_chars
        )
        return bound_exec_result(result, cap)

    @staticmethod
    def _is_exec_timeout(exc: BaseException) -> bool:
        # Harbor Docker/Apple envs raise RuntimeError("Command timed out after N seconds");
        # also accept bare TimeoutError / asyncio.TimeoutError.
        if isinstance(exc, TimeoutError):
            return True
        return "Command timed out after" in str(exc)

    @staticmethod
    def _partial_stream(exc: BaseException, name: str) -> str:
        val = getattr(exc, name, None)
        return val if isinstance(val, str) else ""

    # --- shell ---
    def _exec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """Internal exec. ``capture_max_chars=None`` → tool-trace cap (~60k)."""
        # env forwarding via inline `env K=V` prefix (docker exec has no env dict).
        if env:
            prefix = "env " + " ".join(
                f"{k}={shlex.quote(str(v))}" for k, v in env.items()
            )
            command = f"{prefix} {command}"
        full = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            res = self._await(self._env.exec(full, timeout_sec=timeout))
        except Exception as exc:
            if not self._is_exec_timeout(exc):
                raise
            return self._bound(
                ExecResult(
                    stdout=self._partial_stream(exc, "stdout"),
                    stderr=self._partial_stream(exc, "stderr"),
                    return_code=None,
                    timed_out=True,
                ),
                max_chars=capture_max_chars,
            )
        return self._bound(
            ExecResult(
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                return_code=res.return_code,
                timed_out=bool(getattr(res, "timed_out", False)),
            ),
            max_chars=capture_max_chars,
        )

    def exec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        return self._exec(
            command,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    def _fs_exec(self, command: str) -> ExecResult:
        """Internal FS helper (stat/head/mkdir). Walk ceiling, not tool-trace 60k."""
        return self._exec(command, capture_max_chars=self.walk_output_max_chars)

    def run_python(self, code: str, *, cwd: Optional[str] = None, timeout: Optional[int] = None,
                    env: Optional[dict] = None) -> ExecResult:
        # Quoted heredoc → code body taken literally. Tool-trace 60k: this is
        # the model-facing python tool, not an internal FS helper.
        command = f"python3 - <<'GC_HARBOR_PY_EOF'\n{code}\nGC_HARBOR_PY_EOF"
        return self.exec(command, cwd=cwd, timeout=timeout, env=env)

    # --- file read (shell / download) ---
    def read(self, path: str) -> str:
        # Raw bytes via download, then same codec as local ``read_text_for_edit``.
        if not self.exists(path):
            raise FileNotFoundError(path)
        text = decode_text_best_effort(self.read_bytes(path))
        if text is not None:
            return text
        raise UnicodeDecodeError(
            "utf-8",
            b"",
            0,
            1,
            f"undecodable or binary file: {path}",
        )

    def read_bytes(self, path: str) -> bytes:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self._await(self._env.download_file(path, tmp_path))
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

    def iter_lines(self, path: str) -> Iterable[str]:
        return iter(self.read(path).splitlines(keepends=True))

    def read_head_bytes(self, path: str, n: int) -> bytes:
        """Raw file prefix. ``exec`` stdout is text — pipe via base64 to avoid mangling."""
        n = max(0, int(n))
        if n == 0:
            return b""
        # GNU coreutils in harbor images (same assumption as ``stat -c`` / ``find -printf``).
        # Capture at walk ceiling: append samples 100KiB (~137k base64) > tool 60k.
        res = self._fs_exec(f"head -c {n} {shlex.quote(path)} | base64 -w 0")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"head failed: {path}")
        if res.stdout_hit_capture_cap:
            raise OSError(f"head output exceeded capture ceiling: {path}")
        try:
            return base64.b64decode(res.stdout or "", validate=False)
        except Exception as exc:
            raise OSError(f"base64 decode failed for head of {path}") from exc

    def sha256_bytes(self, path: str) -> str:
        return self.sha256(path)

    # --- stat: native BaseEnvironment handles where they exist, shell otherwise ---
    def exists(self, path: str) -> bool:
        return self._fs_exec(f"test -e {shlex.quote(path)}").return_code == 0

    def lexists(self, path: str) -> bool:
        return self._fs_exec(f"test -e {shlex.quote(path)} || test -L {shlex.quote(path)}").return_code == 0

    def is_file(self, path: str) -> bool:
        return self._await(self._env.is_file(path))

    def is_dir(self, path: str) -> bool:
        return self._await(self._env.is_dir(path))

    def is_symlink(self, path: str) -> bool:
        return self._fs_exec(f"test -L {shlex.quote(path)}").return_code == 0

    def realpath(self, path: str) -> str:
        res = self._fs_exec(f"readlink -f {shlex.quote(path)}")
        return res.stdout.strip() if res.return_code == 0 else path

    def readable(self, path: str) -> bool:
        return True   # container-root perspective (current harbor pack behavior)

    def writable(self, path: str) -> bool:
        return True

    def is_hidden(self, path: str) -> bool:
        return path.rstrip("/").rsplit("/", 1)[-1].startswith(".")

    def getsize(self, path: str) -> int:
        res = self._fs_exec(f"stat -c %s {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"stat failed: {path}")
        return int(res.stdout.strip())

    def sha256(self, path: str) -> str:
        res = self._fs_exec(f"sha256sum {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"sha256sum failed: {path}")
        return res.stdout.split()[0]

    def stat(self, path: str) -> dict:
        res = self._fs_exec(f"stat -c '%s|%Y|%a|%F' {shlex.quote(path)}")
        if res.return_code != 0:
            raise FileNotFoundError(path)
        size, mtime, mode, kind = res.stdout.strip().split("|", 3)
        return {
            "size": int(size), "mtime": int(mtime), "mode": mode,
            "is_file": kind == "regular file", "is_dir": kind == "directory",
            "is_symlink": "symbolic link" in kind,
        }

    def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        st = self.stat(path)  # existing stat -c '%s|%Y|%a|%F' wrapper
        return {
            "size": st["size"], "mtime": st["mtime"],
            "mode": int(str(st["mode"]), 8),
            "is_file": st["is_file"], "is_dir": st["is_dir"], "is_symlink": st["is_symlink"],
        }

    def walk_entries(
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
        res = self._exec(
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

    def walk(self, path: str) -> list:
        """Recursive listing under `path` as [(relpath, is_dir)] (relpath excludes path)."""
        return [(e.rel, e.is_dir) for e in self.walk_entries(path)]

    def listdir(self, path: str) -> list:
        """Immediate children via private traverse channel (same ceiling as walk)."""
        # Тот же общий рецепт, что у обхода: без -H find не идёт по ссылке-аргументу,
        # и каталог-ссылка выглядел пустым (пустой stdout при коде возврата 0).
        head = " ".join(shlex.quote(a) for a in find_walk_head(path))
        res = self._exec(
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

    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        """DFS from flat ``find`` listing (materialized ``walk()``)."""
        yield from iter_walk_from_flat(
            self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        yield from iter_files_from_flat(
            self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

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
        return None  # container's own workdir

    # --- file write/edit (native transfer) ---
    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Allowlist + strict encode on host → bytes upload (same path as write_bytes).
        self.write_bytes(path, encode_text_for_write(content, encoding))

    def write_bytes(self, path: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            self._await(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            self._await(self._env.upload_file(tmp_path, path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Upload only the new chunk, then shell-append (no full-file download).
        # Append payload WITHOUT a leading BOM: a second BOM in the middle of an
        # existing utf-16 file decodes as U+FEFF (ZERO WIDTH NO-BREAK SPACE).
        data = encode_text_for_append(content, encoding)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            local_tmp = Path(tmp.name)
        remote_tmp = f"{path}.gc-append-tmp.{uuid.uuid4().hex}"
        try:
            self._await(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            self._await(self._env.upload_file(local_tmp, remote_tmp))
            res = self._fs_exec(
                f"cat {shlex.quote(remote_tmp)} >> {shlex.quote(path)}; "
                f"rm -f {shlex.quote(remote_tmp)}"
            )
            if res.return_code != 0:
                raise OSError(res.stderr.strip() or f"append failed: {path}")
        finally:
            local_tmp.unlink(missing_ok=True)
            # Best-effort: upload/exec may leave remote_tmp if the cat+rm line never ran.
            try:
                self._fs_exec(f"rm -f {shlex.quote(remote_tmp)}")
            except Exception:
                pass

    def copy(self, src: str, dst: str) -> None:
        res = self._fs_exec(f"cp {shlex.quote(src)} {shlex.quote(dst)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"cp failed: {src} -> {dst}")

    def makedirs(self, path: str) -> None:
        res = self._fs_exec(f"mkdir -p {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"mkdir failed: {path}")
