"""Physical I/O port for the tool layer: LocalFsBackend (host) and HarborBackend
(task container). Tools keep validation/envelopes; backends only answer "where
does this byte live / where does this command run"."""
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    return_code: Optional[int]
    timed_out: bool = False


class FsBackend(ABC):
    # Self-isolated sandbox (task container); tools use it to skip host-only
    # affordances. Security guards do NOT read this flag.
    is_sandbox: bool = False
    # Backend-preferred default exec timeout; None → tool class default.
    default_exec_timeout: Optional[int] = None

    # --- stat ---
    @abstractmethod
    def exists(self, path: str) -> bool: ...
    @abstractmethod
    def lexists(self, path: str) -> bool: ...
    @abstractmethod
    def is_file(self, path: str) -> bool: ...
    @abstractmethod
    def is_dir(self, path: str) -> bool: ...
    @abstractmethod
    def is_symlink(self, path: str) -> bool: ...
    @abstractmethod
    def getsize(self, path: str) -> int: ...
    @abstractmethod
    def stat_info(self, path: str, follow_symlinks: bool = False) -> dict: ...
    @abstractmethod
    def readable(self, path: str) -> bool: ...
    @abstractmethod
    def writable(self, path: str) -> bool: ...
    @abstractmethod
    def realpath(self, path: str) -> str: ...
    @abstractmethod
    def is_hidden(self, path: str) -> bool: ...

    # --- read ---
    @abstractmethod
    def read(self, path: str) -> str: ...
    @abstractmethod
    def iter_lines(self, path: str) -> Iterable[str]: ...
    @abstractmethod
    def read_head_bytes(self, path: str, n: int) -> bytes: ...
    @abstractmethod
    def sha256_bytes(self, path: str) -> str: ...

    # --- write ---
    @abstractmethod
    def write(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None: ...
    @abstractmethod
    def append(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    def copy(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def makedirs(self, path: str) -> None: ...

    # --- traverse ---
    @abstractmethod
    def listdir(self, path: str) -> list: ...
    @abstractmethod
    def walk(self, path: str) -> list: ...

    @abstractmethod
    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]: ...

    @abstractmethod
    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]: ...

    # --- path semantics ---
    @abstractmethod
    def normpath(self, path: str) -> str: ...
    @abstractmethod
    def join(self, base: str, name: str) -> str: ...

    # --- exec ---
    @abstractmethod
    def exec(self, command: str, *, cwd: Optional[str] = None,
             timeout: Optional[int] = None,
             env: Optional[dict] = None) -> ExecResult: ...

    def exec_argv(self, argv: list, *, stdin: Optional[str] = None,
                  cwd: Optional[str] = None,
                  timeout: Optional[int] = None,
                  env: Optional[dict] = None) -> ExecResult:
        """Run a program from an argv list, optionally feeding ``stdin``.

        Default builds a POSIX shell string (valid for sandbox backends whose
        exec runs through a POSIX shell). Backends whose exec shell is not
        POSIX (e.g. local Windows cmd.exe) must override with a shell-less
        implementation.
        """
        command = " ".join(shlex.quote(str(a)) for a in argv)
        if stdin is not None:
            command = f"printf '%s' {shlex.quote(stdin)} | {command}"
        return self.exec(command, cwd=cwd, timeout=timeout, env=env)
    @abstractmethod
    def run_python(self, code: str, *, cwd: Optional[str] = None,
                   timeout: Optional[int] = None,
                   env: Optional[dict] = None) -> ExecResult: ...
    @abstractmethod
    def default_cwd(self) -> Optional[str]: ...
