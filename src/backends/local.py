"""Host implementation of FsBackend (os / open() / subprocess); the host-I/O
details that used to live inline in the tools are concentrated here."""
import hashlib
import os
import stat as stat_module
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, Optional

from backends.protocol import ExecResult, FsBackend
from utils.file_text import read_text_for_edit


class LocalFsBackend(FsBackend):
    is_sandbox = False
    default_exec_timeout = None

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

    def iter_lines(self, path: str) -> Iterable[str]:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                yield line

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
        # Atomic: tmp + os.replace.
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding=encoding, errors="replace") as fd:
                fd.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

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
        with open(path, "a", encoding=encoding, errors="replace") as fd:
            fd.write(content)

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
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        from utils.win_console import preserve_console_mode
        try:
            with preserve_console_mode():
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    encoding="utf-8",
                    errors="replace",
                    env=merged_env,
                )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                stdout=str(getattr(e, "stdout", "") or ""),
                stderr=str(getattr(e, "stderr", "") or ""),
                return_code=None,
                timed_out=True,
            )
        return ExecResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            return_code=result.returncode,
        )

    def run_python(self, code: str, *, cwd: Optional[str] = None,
                   timeout: Optional[int] = None,
                   env: Optional[dict] = None) -> ExecResult:
        from utils.win_console import preserve_console_mode
        try:
            with preserve_console_mode():
                result = subprocess.run(
                    [sys.executable, "-"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=_sanitize_env(env),
                    cwd=cwd,
                    input=code,
                )
        except subprocess.TimeoutExpired:
            return ExecResult(stdout="", stderr="", return_code=None, timed_out=True)
        return ExecResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            return_code=result.returncode,
        )

    def default_cwd(self) -> Optional[str]:
        return os.getcwd()


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
