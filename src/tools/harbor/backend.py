"""Synchronous backend over an async Harbor BaseEnvironment.

The gc-cli tool loop is synchronous; Harbor's environment is async and bound to the
adapter's event loop. Every method marshals its coroutine onto that loop via
run_coroutine_threadsafe and blocks the (worker-thread) caller until it completes.
File ops mirror deepagents' HarborSandbox: write/edit use native upload/download,
read/list use shell through exec.
"""
import asyncio
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    return_code: int


class HarborBackend:
    def __init__(self, environment: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._env = environment
        self._loop = loop

    def _await(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # --- shell ---
    def exec(self, command: str, *, cwd: Optional[str] = None, timeout: Optional[int] = None) -> ExecResult:
        full = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        res = self._await(self._env.exec(full, timeout_sec=timeout))
        return ExecResult(
            stdout=res.stdout or "", stderr=res.stderr or "", return_code=res.return_code
        )

    # --- file read (shell) ---
    def read(self, path: str) -> str:
        res = self._await(self._env.exec(f"cat {shlex.quote(path)}"))
        if res.return_code != 0:
            raise FileNotFoundError(path)
        return res.stdout or ""

    def list(self, path: str) -> str:
        res = self._await(self._env.exec(f"ls -la {shlex.quote(path)}"))
        return res.stdout or ""

    # --- stat primitives — let shims mirror the base tools' os.path checks ---
    # is_file/is_dir use Harbor's native BaseEnvironment handles (environment-agnostic
    # across docker/gke/modal/e2b…); exists has no native handle, so it stays shell.
    def exists(self, path: str) -> bool:
        return self.exec(f"test -e {shlex.quote(path)}").return_code == 0

    def is_file(self, path: str) -> bool:
        return self._await(self._env.is_file(path))

    def is_dir(self, path: str) -> bool:
        return self._await(self._env.is_dir(path))

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

    # --- file write/edit (native transfer) ---
    def write(self, path: str, content: str) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            self._await(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            self._await(self._env.upload_file(tmp_path, path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def write_bytes(self, path: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            self._await(self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})"))
            self._await(self._env.upload_file(tmp_path, path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def edit(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> int:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "f"
            self._await(self._env.download_file(path, local))
            text = local.read_bytes().decode("utf-8")
            count = text.count(old_string)
            text = (
                text.replace(old_string, new_string)
                if replace_all
                else text.replace(old_string, new_string, 1)
            )
            local.write_bytes(text.encode("utf-8"))
            self._await(self._env.upload_file(local, path))
        return count
