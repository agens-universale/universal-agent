"""Synchronous FsBackend over an async Harbor BaseEnvironment.

The gc-cli tool loop is synchronous; Harbor's environment is async and bound to the
adapter's event loop. Every method marshals its coroutine onto that loop via
run_coroutine_threadsafe and blocks the (worker-thread) caller until it completes.
File ops mirror deepagents' HarborSandbox: write/edit use native upload/download,
read/list use shell through exec.
"""
import asyncio
import shlex
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ExecResult,
    FsBackend,
    bound_exec_result,
)
from backends.walk_util import iter_files_from_flat, iter_walk_from_flat


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
    ) -> None:
        self._env = environment
        self._loop = loop
        self.exec_output_capture_max_chars = exec_output_capture_max_chars

    def _await(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _bound(self, result: ExecResult) -> ExecResult:
        return bound_exec_result(result, self.exec_output_capture_max_chars)

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
    def exec(self, command: str, *, cwd: Optional[str] = None, timeout: Optional[int] = None,
              env: Optional[dict] = None) -> ExecResult:
        # env forwarding via inline `env K=V` prefix (docker exec has no env dict).
        if env:
            prefix = "env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
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
                )
            )
        return self._bound(
            ExecResult(
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                return_code=res.return_code,
                timed_out=bool(getattr(res, "timed_out", False)),
            )
        )

    def run_python(self, code: str, *, cwd: Optional[str] = None, timeout: Optional[int] = None,
                    env: Optional[dict] = None) -> ExecResult:
        # Quoted heredoc → code body taken literally.
        command = f"python3 - <<'GC_HARBOR_PY_EOF'\n{code}\nGC_HARBOR_PY_EOF"
        return self.exec(command, cwd=cwd, timeout=timeout, env=env)

    # --- file read (shell) ---
    def read(self, path: str) -> str:
        res = self._await(self._env.exec(f"cat {shlex.quote(path)}"))
        if res.return_code != 0:
            raise FileNotFoundError(path)
        return res.stdout or ""

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
        res = self.exec(f"head -c {int(n)} {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"head failed: {path}")
        return (res.stdout or "").encode("utf-8", errors="surrogateescape")[:n]

    def sha256_bytes(self, path: str) -> str:
        return self.sha256(path)

    # --- stat: native BaseEnvironment handles where they exist, shell otherwise ---
    def exists(self, path: str) -> bool:
        return self.exec(f"test -e {shlex.quote(path)}").return_code == 0

    def lexists(self, path: str) -> bool:
        return self.exec(f"test -e {shlex.quote(path)} || test -L {shlex.quote(path)}").return_code == 0

    def is_file(self, path: str) -> bool:
        return self._await(self._env.is_file(path))

    def is_dir(self, path: str) -> bool:
        return self._await(self._env.is_dir(path))

    def is_symlink(self, path: str) -> bool:
        return self.exec(f"test -L {shlex.quote(path)}").return_code == 0

    def realpath(self, path: str) -> str:
        res = self.exec(f"readlink -f {shlex.quote(path)}")
        return res.stdout.strip() if res.return_code == 0 else path

    def readable(self, path: str) -> bool:
        return True   # container-root perspective (current harbor pack behavior)

    def writable(self, path: str) -> bool:
        return True

    def is_hidden(self, path: str) -> bool:
        return path.rstrip("/").rsplit("/", 1)[-1].startswith(".")

    def getsize(self, path: str) -> int:
        res = self.exec(f"stat -c %s {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"stat failed: {path}")
        return int(res.stdout.strip())

    def sha256(self, path: str) -> str:
        res = self.exec(f"sha256sum {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"sha256sum failed: {path}")
        return res.stdout.split()[0]

    def stat(self, path: str) -> dict:
        res = self.exec(f"stat -c '%s|%Y|%a|%F' {shlex.quote(path)}")
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

    def walk(self, path: str) -> list:
        """Recursive listing under `path` as [(relpath, is_dir)] (relpath excludes path)."""
        res = self.exec(f"find {shlex.quote(path)} -mindepth 1 -printf '%P\\t%y\\n'")
        if res.return_code != 0:
            raise FileNotFoundError(path)
        out = []
        for line in (res.stdout or "").splitlines():
            if not line.strip():
                continue
            rel, _, kind = line.partition("\t")
            out.append((rel, kind == "d"))
        return out

    def listdir(self, path: str) -> list:
        res = self.exec(f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 -printf '%f\\t%y\\n'")
        if res.return_code != 0:
            raise FileNotFoundError(path)
        out = []
        for line in (res.stdout or "").splitlines():
            if not line.strip():
                continue
            name, _, kind = line.partition("\t")
            out.append((name, kind == "d"))
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
        return path.rstrip("/") or "/"

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    def default_cwd(self) -> Optional[str]:
        return None  # container's own workdir

    # --- file write/edit (native transfer) ---
    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Strict encode on host → bytes upload (same path as write_bytes).
        self.write_bytes(path, content.encode(encoding))

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
        current = self.read_bytes(path) if self.exists(path) else b""
        self.write_bytes(path, current + content.encode(encoding))

    def copy(self, src: str, dst: str) -> None:
        res = self.exec(f"cp {shlex.quote(src)} {shlex.quote(dst)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"cp failed: {src} -> {dst}")

    def makedirs(self, path: str) -> None:
        res = self.exec(f"mkdir -p {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"mkdir failed: {path}")
