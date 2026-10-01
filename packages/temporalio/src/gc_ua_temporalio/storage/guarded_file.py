"""One-file effects: prepare through the existing tool, publish atomically."""

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from gc_core.execution.contracts import ExecutionFailure
from gc_core.utils.file_text import (
    encode_text_for_append,
    encode_text_for_write,
)


class FileEffectUnknown(BaseException):
    """Bypasses legacy tool Exception-to-feedback conversion after an effect."""


def file_facts(path, workspace):
    path = Path(os.path.abspath(path))
    root = Path(workspace).resolve()
    if not path.is_relative_to(root):
        raise ValueError(
            "Durable writes are restricted to the profile workspace"
        )
    parents = []
    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise ValueError("Durable writes do not support symlink paths")
        if candidate == root:
            break
        if candidate != path:
            info = candidate.stat()
            parents.append([str(candidate), info.st_dev, info.st_ino])
    if not path.parent.is_dir():
        raise ValueError("The parent directory must already exist")
    root_info = root.stat()
    value = {
        "path": str(path),
        "root": [str(root), root_info.st_dev, root_info.st_ino],
        "parents": parents,
        "exists": path.exists(),
    }
    if value["exists"]:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(
                "Durable writes require a regular file without hard links"
            )
        value.update(
            inode=info.st_ino,
            device=info.st_dev,
            mtime_ns=info.st_mtime_ns,
            mode=stat.S_IMODE(info.st_mode),
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return value


@contextmanager
def workspace_lock(directory, workspace):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
    with (directory / key).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Closing the descriptor releases the process lock, including on SIGKILL.
        yield


class PlanningFileBackend:
    def __init__(self, backend, path):
        self.backend = backend
        self.path = path
        self.content = None

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def write_bytes(self, path, data):
        if path != self.path or self.content is not None:
            raise ExecutionFailure(
                "Only one target write is supported per operation"
            )
        self.content = data

    async def write(self, path, content, encoding="utf-8"):
        await self.write_bytes(path, encode_text_for_write(content, encoding))

    async def append(self, path, content, encoding="utf-8"):
        before = await self.backend.read_bytes(path)
        await self.write_bytes(
            path, before + encode_text_for_append(content, encoding)
        )

    async def sha256_bytes(self, path):
        if path == self.path and self.content is not None:
            return hashlib.sha256(self.content).hexdigest()
        return await self.backend.sha256_bytes(path)

    async def copy(self, *_args):
        raise ExecutionFailure(
            "Backups are not supported by the durable write profile"
        )

    async def makedirs(self, *_args, **_kwargs):
        raise ExecutionFailure(
            "Create parent directories before the durable write"
        )


class CheckedFileBackend(PlanningFileBackend):
    def __init__(self, backend, plan, workspace, expected):
        super().__init__(backend, plan.path)
        self.plan = plan
        self.workspace = workspace
        self.expected = expected

    async def write_bytes(self, path, data):
        await super().write_bytes(path, data)
        if (
            data != self.expected
            or file_facts(path, self.workspace) != self.plan.before
        ):
            raise FileEffectUnknown(
                "File precondition changed before publication"
            )
        try:
            atomic_write(path, data, self.plan.before.get("mode", 0o600))
        except OSError as exc:
            raise FileEffectUnknown(type(exc).__name__) from None


def atomic_write(path, data, mode):
    fd, temporary = tempfile.mkstemp(
        dir=Path(path).parent, prefix=".ua-write-"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(Path(path).parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
