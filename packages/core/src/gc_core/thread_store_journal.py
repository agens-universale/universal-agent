"""Recoverable transactions for canonical thread files (single host).

The journal contains pending file images, never a second readable history.
All supported access takes the same advisory process lock and rolls forward
prepared writes before exposing canonical files.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import shutil
import threading
from pathlib import Path

from gc_core.threads.errors import OwnershipError


class JournalRecoveryError(OSError):
    """Незавершённую запись треда не доиграть: файлы на диске не те, что
    записал прерванный коммит (их правили руками или другой процесс). Тред
    не читается, пока это не разобрать."""


logger = logging.getLogger(__name__)

_active = contextvars.ContextVar("thread_file_transaction", default=False)
_held = contextvars.ContextVar("thread_file_lock", default=())
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
# Служебный каталог журнала, файл счётчиков треда и суффикс квитанций.
_STORE_DIR = ".store"
_CONTROL_FILE = "control.json"
_JSON_SUFFIX = ".json"


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path: Path, data: bytes) -> None:
    from gc_core.utils.atomic_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".durable-tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    atomic_replace(str(temporary), str(path))
    _sync_dir(path.parent)


def _json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _physical_control_path(directory, stem):
    return (
        Path(directory).absolute()
        / _STORE_DIR
        / hashlib.sha256(stem.encode()).hexdigest()
    )


def _control_path(directory, system_id, thread_id):
    from gc_core.thread_store import sanitize_system_id, validate_thread_id

    stem = f"{sanitize_system_id(system_id)}_{validate_thread_id(thread_id)}"
    return _physical_control_path(directory, stem)


def _control(base):
    """``control.json`` треда. Испорченный (пустой после отключения питания
    в режиме без fsync, чужой писатель) — как отсутствующий: тред читается
    и пишется дальше с нуля по ревизии, а не закрывается намертво."""
    path = base / _CONTROL_FILE
    fresh = {
        "revision": 0,
        "generation": 0,
        "owner_id": None,
        "owner_epoch": 0,
    }
    if not path.exists():
        return fresh
    try:
        control = json.loads(path.read_bytes())
    except ValueError:
        control = None
    if not isinstance(control, dict):
        logger.warning("испорченный %s — тред с чистой ревизией", path)
        return fresh
    return control


def durable_owner_id(directory, system_id, thread_id):
    """Владелец durable-треда без блокировки и без создания ``.store/``.

    Нужен обычному ``FileThreadStore``: локальный писатель не должен писать
    в тред, которым сейчас владеет исполнитель с журналом (Temporal).
    """
    path = _control_path(directory, system_id, thread_id) / _CONTROL_FILE
    try:
        return json.loads(path.read_bytes()).get("owner_id")
    except (FileNotFoundError, NotADirectoryError, ValueError, AttributeError):
        return None


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _recover_append(path, change):
    tail = base64.b64decode(change["append"])
    offset = change["offset"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("r+b" if path.exists() else "w+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size < offset or size > offset + len(tail):
            raise JournalRecoveryError(
                f"Unexpected append length during recovery: {path}"
            )
        stream.seek(offset)
        existing = stream.read()
        if existing != tail[: len(existing)]:
            raise JournalRecoveryError(
                f"Unexpected append bytes during recovery: {path}"
            )
        stream.seek(size)
        stream.write(tail[len(existing) :])
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _recover_image(path, change):
    current = path.read_bytes() if path.exists() else None
    after = (
        base64.b64decode(change["after"])
        if change["after"] is not None
        else None
    )
    if _digest(current) not in (change["before"], _digest(after)):
        raise JournalRecoveryError(
            f"Unexpected file content during recovery: {path}"
        )
    if current == after:
        return
    if after is None:
        path.unlink(missing_ok=True)
        _sync_dir(path.parent)
    else:
        _write(path, after)


def _readable_journal(pending: Path) -> dict | None:
    try:
        journal = json.loads(pending.read_bytes())
    except ValueError:
        return None
    keys = ("changes", "control", "receipt")
    if not isinstance(journal, dict) or any(k not in journal for k in keys):
        return None
    return journal


def _recover(directory: Path, base: Path):
    # Stage прерванного коммита (SIGKILL внутри транзакции): под блокировкой
    # треда активного коммита нет, каталог можно убрать.
    for stage in base.glob("prepare-*"):
        shutil.rmtree(stage, ignore_errors=True)
    pending = base / "pending.json"
    if not pending.exists():
        return
    journal = _readable_journal(pending)
    if journal is None:
        # Журнал не дописан (отключение питания без fsync): доиграть нечего,
        # а оставить — значит закрыть тред для всех. Он уходит в сторону.
        aside = pending.with_name(f"pending.json.corrupt-{os.getpid()}")
        os.replace(pending, aside)
        logger.error("испорченный журнал записи треда: %s", aside)
        return
    for change in journal["changes"]:
        path = directory / change["path"]
        if "offset" in change:
            _recover_append(path, change)
        else:
            _recover_image(path, change)
    _write(base / _CONTROL_FILE, _json(journal["control"]))
    receipt = journal["receipt"]
    _write(
        base
        / "receipts"
        / (
            hashlib.sha256(receipt["operation_id"].encode()).hexdigest()
            + _JSON_SUFFIX
        ),
        _json(receipt),
    )
    pending.unlink()
    _sync_dir(base)


@contextlib.contextmanager
def _physical_read_scope(directory, base):
    """One lock per physical canonical filename, including ambiguous IDs."""
    if _active.get() or str(base) in _held.get():
        yield
        return
    if _held.get() and str(base) < max(_held.get()):
        raise RuntimeError(
            "Cross-thread file scopes must acquire locks in global order"
        )
    with _locks_guard:
        lock = _locks.setdefault(str(base), threading.RLock())
    from gc_core.threads.file_namespace import namespace_scope

    with namespace_scope(directory), lock, _locked_file(base):
        token = _held.set((*_held.get(), str(base)))
        try:
            _recover(Path(directory).absolute(), base)
            yield
        finally:
            _held.reset(token)


def _lock_stream(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        if not stream.read(1):
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _still_there(stream, path: Path) -> bool:
    try:
        return os.path.samestat(os.fstat(stream.fileno()), os.stat(path))
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def _locked_file(base: Path):
    """Захватить ``base/lock``. Служебный каталог удалённого треда уборка
    убирает под этой же блокировкой (``FileThreadStore.sweep_tombstones``):
    дождавшийся её получает файл, которого уже нет по пути, — тогда
    каталог заводится и блокировка берётся заново."""
    path = base / "lock"
    while True:
        base.mkdir(parents=True, exist_ok=True)
        try:
            stream = path.open("a+b")
        except FileNotFoundError:
            continue  # каталог убрали между mkdir и open
        _lock_stream(stream)
        if _still_there(stream, path):
            break
        _unlock_stream(stream)
        stream.close()
    try:
        yield stream
    finally:
        try:
            _unlock_stream(stream)
        finally:
            stream.close()


def _bind_identity(directory, base, identity):
    marker = base / "identity.json"
    if marker.exists():
        if json.loads(marker.read_bytes()) != identity:
            raise OwnershipError(
                "Canonical filename belongs to another logical thread"
            )
        return
    meta_path = (
        Path(directory)
        / f"meta_{identity['system_id']}_{identity['thread_id']}.json"
    )
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Reset/delete must still be able to remove broken files.
            # Actual metadata readers retain their parsing errors.
            meta = {}
        if isinstance(meta, dict) and (
            (meta.get("system") and meta["system"] != identity["system_id"])
            or (
                meta.get("thread_id")
                and meta["thread_id"] != identity["thread_id"]
            )
        ):
            raise OwnershipError(
                "Canonical metadata belongs to another logical thread"
            )
    _write(marker, _json(identity))


@contextlib.contextmanager
def thread_read_scope(directory, system_id, thread_id):
    """Recover and bind an unambiguous logical identity under its file lock."""
    from gc_core.thread_store import sanitize_system_id, validate_thread_id

    identity = {
        "system_id": sanitize_system_id(system_id),
        "thread_id": validate_thread_id(thread_id),
    }
    base = _control_path(directory, system_id, thread_id)
    legacy_base = _physical_control_path(directory, identity["thread_id"])
    with contextlib.ExitStack() as scopes:
        for lock_base in sorted({base, legacy_base}):
            scopes.enter_context(_physical_read_scope(directory, lock_base))
        _bind_identity(directory, base, identity)
        yield
