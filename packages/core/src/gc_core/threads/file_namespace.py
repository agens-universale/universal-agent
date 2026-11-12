"""Общее пространство алиасов: читатели параллельны, изменение эксклюзивно.

Порядок блокировок: namespace, затем тред. Указатель незавершённой
транзакции позволяет доиграть её до следующего назначения любого алиаса.
"""

from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path

from gc_core.utils.file_lock import file_lock

_held = contextvars.ContextVar("thread_namespace_locks", default=None)
_STORE = ".store"


def pending_path(directory: str | Path) -> Path:
    return Path(directory).absolute() / _STORE / ".alias-pending"


@contextlib.contextmanager
def namespace_scope(directory: str | Path, *, exclusive: bool = False):
    directory = Path(directory).absolute()
    name = str(directory)
    held = _held.get() or {}
    if name in held:
        if exclusive and not held[name]:
            raise RuntimeError("alias lock must precede the thread lock")
        yield
        return
    pending = pending_path(directory)
    # Перечитка после взятия shared: писатель мог оставить журнал, пока
    # мы ждали его эксклюзивный замок.
    while True:
        write = exclusive or pending.exists()
        with file_lock(
            directory / _STORE / ".namespace", timeout=60, shared=not write
        ):
            if not write and pending.exists():
                continue
            token = _held.set({**held, name: write})
            try:
                if pending.exists():
                    from gc_core.thread_store_journal import _recover

                    base = directory / _STORE / pending.read_text()
                    _recover(directory, base)
                    pending.unlink(missing_ok=True)
                yield
            finally:
                _held.reset(token)
            return
