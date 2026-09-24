"""Кроссплатформенный межпроцессный файловый лок.

Эксклюзивная блокировка через отдельный lock-файл ``<path>.lock`` рядом
с целевым файлом: ``fcntl.flock`` на POSIX, ``msvcrt.locking`` на Windows.
"""
import contextlib
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_POLL_INTERVAL = 0.05


@contextlib.contextmanager
def file_lock(path, timeout: float = 5.0):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Не удалось захватить лок за {timeout}с: {lock_path}")
            time.sleep(_POLL_INTERVAL)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


def _try_lock(fh) -> bool:
    fh.seek(0)
    if sys.platform == "win32":
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    if sys.platform == "win32":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
