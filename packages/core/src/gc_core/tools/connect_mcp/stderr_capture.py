"""Ограниченный stderr: отдельный файл запуска и непрерывный слив pipe."""

import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from gc_core.utils.pid_alive import is_pid_alive

MAX_BYTES = 1024 * 1024
KEEP_CLOSED = 32
_READERS: dict[Path, threading.Event] = {}
_NAME = re.compile(r".+\.(\d+)\.[0-9a-f]{12}\.stderr\.log$")


def _close(handle) -> None:
    try:
        handle.close()
    except OSError:
        pass


def _prune(directory: Path, closed_path: Path) -> None:
    """Живые писатели (в том числе другого процесса) не затрагиваются."""
    try:
        closed = []
        for path in directory.glob("*.stderr.log"):
            match = _NAME.fullmatch(path.name)
            if match is None or (path in _READERS and path != closed_path):
                continue
            pid = int(match[1])
            if pid != os.getpid() and is_pid_alive(pid):
                continue
            closed.append(path)
        closed.sort(key=lambda path: path.stat().st_mtime)
        for path in closed[:-KEEP_CLOSED]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _drain(fd: int, handle, path: Path, done: threading.Event) -> None:
    tail = bytearray()
    size = 0
    try:
        with os.fdopen(fd, "rb", buffering=0):
            while chunk := os.read(fd, 65536):
                if handle is None:
                    continue  # Ошибка диска не должна заблокировать MCP-процесс.
                try:
                    tail.extend(chunk)
                    if size + len(chunk) > MAX_BYTES:
                        del tail[: -MAX_BYTES // 2]
                        handle.truncate(0)
                        handle.write(tail)
                        size = len(tail)
                    else:
                        handle.write(chunk)
                        size += len(chunk)
                    del tail[: -MAX_BYTES // 2]
                    handle.flush()
                except OSError:
                    _close(handle)
                    handle = None
    finally:
        if handle is not None:
            _close(handle)
        _prune(path.parent, path)
        done.set()
        _READERS.pop(path, None)


@contextmanager
def capture(path: Path, opener):
    handle = opener(path)
    try:
        read_fd, write_fd = os.pipe()
    except BaseException:
        handle.close()
        raise
    done = threading.Event()
    _READERS[path] = done
    worker = threading.Thread(
        target=_drain, args=(read_fd, handle, path, done), daemon=True
    )
    try:
        worker.start()
    except BaseException:
        _READERS.pop(path, None)
        os.close(read_fd)
        os.close(write_fd)
        handle.close()
        raise
    # После spawn у ребёнка своя копия write_fd. Читатель живёт до её EOF.
    with os.fdopen(write_fd, "wb", buffering=0) as writer:
        yield writer, path


def finish(path: Path) -> None:
    """После завершения дочернего процесса дождаться последнего stderr-чанка."""
    done = _READERS.get(path)
    if done is not None:
        done.wait(timeout=1)
