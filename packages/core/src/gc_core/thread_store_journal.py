"""Recoverable transactions for canonical thread files (single host).

The journal contains pending file images, never a second readable history.
All supported access takes the same advisory process lock and rolls forward
prepared writes before exposing canonical files.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import copy
import hashlib
import inspect
import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
    ThreadStoreReceipt,
    ThreadStoreRecoveryError,
    ThreadStoreSnapshot,
)
from gc_core.thread_store_types import ThreadState

_active = contextvars.ContextVar("thread_file_transaction", default=False)
_held = contextvars.ContextVar("thread_file_lock", default=())
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_READERS = {"load_thread_messages", "load_thread_meta", "read_subagent_trace"}


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path: Path, data: bytes) -> None:
    from gc_core.thread_store import atomic_replace

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
        / ".store"
        / hashlib.sha256(stem.encode()).hexdigest()
    )


def _control_path(directory, system_id, thread_id):
    from gc_core.thread_store import sanitize_system_id, validate_thread_id

    stem = f"{sanitize_system_id(system_id)}_{validate_thread_id(thread_id)}"
    return _physical_control_path(directory, stem)


def _control(base):
    path = base / "control.json"
    return (
        json.loads(path.read_bytes())
        if path.exists()
        else {
            "revision": 0,
            "generation": 0,
            "owner_id": None,
            "owner_epoch": 0,
        }
    )


def _versions(control):
    return {
        key: control[key]
        for key in ("revision", "generation", "owner_id", "owner_epoch")
    }


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
            raise ThreadStoreRecoveryError(
                f"Unexpected append length during recovery: {path}"
            )
        stream.seek(offset)
        existing = stream.read()
        if existing != tail[: len(existing)]:
            raise ThreadStoreRecoveryError(
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
        raise ThreadStoreRecoveryError(
            f"Unexpected file content during recovery: {path}"
        )
    if current == after:
        return
    if after is None:
        path.unlink(missing_ok=True)
        _sync_dir(path.parent)
    else:
        _write(path, after)


def _recover(directory: Path, base: Path):
    pending = base / "pending.json"
    if not pending.exists():
        return
    journal = json.loads(pending.read_bytes())
    for change in journal["changes"]:
        path = directory / change["path"]
        if "offset" in change:
            _recover_append(path, change)
        else:
            _recover_image(path, change)
    _write(base / "control.json", _json(journal["control"]))
    receipt = journal["receipt"]
    _write(
        base
        / "receipts"
        / (
            hashlib.sha256(receipt["operation_id"].encode()).hexdigest()
            + ".json"
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
    base.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        lock = _locks.setdefault(str(base), threading.RLock())
    with lock, (base / "lock").open("a+b") as stream:
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
        token = _held.set((*_held.get(), str(base)))
        try:
            _recover(Path(directory).absolute(), base)
            yield
        finally:
            _held.reset(token)
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _bind_identity(directory, base, identity):
    marker = base / "identity.json"
    if marker.exists():
        if json.loads(marker.read_bytes()) != identity:
            raise ThreadStoreOwnershipError(
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
            raise ThreadStoreOwnershipError(
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


@contextlib.contextmanager
def thread_file_read_scope(directory, stem):
    """Recover a physical filename stem without guessing system/thread split."""
    from gc_core.thread_store import validate_thread_id

    validate_thread_id(stem)
    with _physical_read_scope(
        directory, _physical_control_path(directory, stem)
    ):
        yield


def recover_pending_threads(directory):
    """Roll forward prepared threads before enumerating their canonical files."""
    root = Path(directory) / ".store"
    for pending in root.glob("*/pending.json"):
        try:
            manifest = json.loads(pending.read_bytes())
        except FileNotFoundError:
            continue
        identity = manifest.get("identity") or {}
        system_id, thread_id = (
            identity.get("system_id"),
            identity.get("thread_id"),
        )
        if system_id and thread_id:
            with thread_read_scope(directory, system_id, thread_id):
                # Entering the scope rolls forward the pending journal under lock.
                pass


@contextlib.contextmanager
def _codec_scope():
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)


def read_snapshot(directory, system_id, thread_id, *, include_state=True):
    from gc_core import thread_store as raw

    with thread_read_scope(directory, system_id, thread_id):
        control = _control(_control_path(directory, system_id, thread_id))
        if include_state or "max_seq" not in control:
            messages, keys, maximum = raw.load_thread_messages(
                directory, system_id, thread_id
            )
        else:
            messages, keys, maximum = [], set(), control["max_seq"]
        return ThreadStoreSnapshot(
            meta=raw.load_thread_meta(directory, system_id, thread_id),
            state=ThreadState(messages, keys, maximum)
            if include_state
            else None,
            max_seq=maximum,
            **_versions(control),
        )


def _paths(directory, system_id, thread_id, *, include_archives=False):
    from gc_core import thread_store as raw

    return set(
        map(
            Path,
            [
                *raw._thread_paths(str(directory), system_id, thread_id),
                *raw._thread_paths_legacy(str(directory), thread_id),
                *(
                    raw._thread_archive_paths(
                        str(directory), system_id, thread_id
                    )
                    if include_archives
                    else []
                ),
                raw.subagent_trace_path(str(directory), system_id, thread_id),
                raw.touched_journal_path(str(directory), system_id, thread_id),
                raw._lock_path(str(directory), system_id, thread_id),
            ],
        )
    )


def _translate(value, stage, directory):
    if isinstance(value, str) and value.startswith(str(stage) + os.sep):
        return str(directory / Path(value).relative_to(stage))
    if isinstance(value, list):
        return [_translate(item, stage, directory) for item in value]
    if isinstance(value, dict):
        return {
            key: _translate(item, stage, directory)
            for key, item in value.items()
        }
    return value


class _FileTransaction:
    """Prepare one canonical file transaction before publishing its redo record."""

    def __init__(self, directory, system_id, thread_id, operation, helper):
        self.directory, self.system_id, self.thread_id = (
            directory,
            system_id,
            thread_id,
        )
        self.operation, self.helper = operation, helper
        self.base = _control_path(directory, system_id, thread_id)
        self.digest = _digest(_json(asdict(operation)))

    def commit(self):
        previous = self._original_receipt()
        if previous is not None:
            return previous
        self.control = _control(self.base)
        self._validate()
        self._prepare_files()
        with (
            _codec_scope(),
            tempfile.TemporaryDirectory(
                prefix="prepare-", dir=self.base
            ) as stage_name,
        ):
            self.stage = Path(stage_name)
            self._copy_files()
            self._prepare_sidecar()
            self._apply()
            receipt = self._receipt()
            self._read_after()
            self._cache_sidecar()
            return self._publish(receipt, self._changes())

    def _original_receipt(self):
        receipt_path = (
            self.base
            / "receipts"
            / (
                hashlib.sha256(
                    self.operation.operation_id.encode()
                ).hexdigest()
                + ".json"
            )
        )
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_bytes())
            if previous["input_digest"] != self.digest:
                raise ThreadStoreOperationMismatch(
                    "Operation identity reused with different input"
                )
            return ThreadStoreReceipt(**previous)

    def _validate(self):
        if self.operation.generation != self.control["generation"]:
            raise ThreadStoreOwnershipError("Thread generation changed")
        if self.operation.kind not in ("claim_owner", "update_tui") and (
            self.operation.owner_id != self.control["owner_id"]
            or (
                self.operation.owner_epoch is not None
                and self.operation.owner_epoch != self.control["owner_epoch"]
            )
        ):
            raise ThreadStoreOwnershipError("Thread writer ownership changed")
        if (
            self.operation.expected_revision is not None
            and self.operation.expected_revision != self.control["revision"]
        ):
            raise ThreadStoreConflict("Thread revision changed")

    def _prepare_files(self):
        from gc_core import thread_store as raw

        self.metadata_only = self.operation.kind in {
            "save_meta",
            "save_thread_meta",
            "patch_runtime_flag",
            "reset_run_flags",
            "update_runtime_meta",
            "update_tui",
            "claim_owner",
            "release_owner",
        }
        self.sidecar_only = self.operation.kind in {
            "append_subagent_trace",
            "append_subagent_trace_record",
            "restore_subagent_trace_records",
        }
        selected = _paths(
            self.directory,
            self.system_id,
            self.thread_id,
            include_archives=self.operation.kind == "delete_thread",
        )
        if self.metadata_only:
            selected = {
                Path(
                    raw._thread_paths(
                        str(self.directory), self.system_id, self.thread_id
                    )[1]
                ),
                Path(
                    raw._thread_paths_legacy(
                        str(self.directory), self.thread_id
                    )[1]
                ),
            }
        elif self.sidecar_only:
            selected = {
                Path(
                    raw.subagent_trace_path(
                        str(self.directory), self.system_id, self.thread_id
                    )
                )
            }
        self.append_paths = {}
        self.append_history = self.operation.kind in {
            "persist",
            "append_messages",
            "append_new_thread_messages",
        }
        self.append_sidecar = self.operation.kind in {
            "append_subagent_trace",
            "append_subagent_trace_record",
        }
        if self.append_history or self.append_sidecar:
            append_path = Path(
                raw._thread_paths(
                    str(self.directory), self.system_id, self.thread_id
                )[0]
                if self.append_history
                else raw.subagent_trace_path(
                    str(self.directory), self.system_id, self.thread_id
                )
            )
            self.append_paths[append_path.relative_to(self.directory)] = (
                append_path.stat().st_size if append_path.exists() else 0
            )
            selected.discard(append_path)
            if self.append_history:
                selected = {
                    Path(
                        raw._thread_paths(
                            str(self.directory), self.system_id, self.thread_id
                        )[1]
                    ),
                    Path(
                        raw._thread_paths_legacy(
                            str(self.directory), self.thread_id
                        )[1]
                    ),
                }
        self.before = {
            path.relative_to(self.directory): path.read_bytes()
            for path in selected
            if path.exists()
        }
        if (
            self.metadata_only or self.sidecar_only or self.append_history
        ) and "max_seq" not in self.control:
            self.control["max_seq"] = raw.load_thread_messages(
                str(self.directory), self.system_id, self.thread_id
            )[2]

    def _copy_files(self):
        for relative, data in self.before.items():
            target = self.stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _prepare_sidecar(self):
        from gc_core import thread_store as raw

        self.sidecar = Path(
            raw.subagent_trace_path(
                *(str(self.stage), self.system_id, self.thread_id)
            )
        )
        self.sidecar_relative = self.sidecar.relative_to(self.stage)
        if self.append_sidecar:
            canonical_sidecar = self.directory / self.sidecar_relative
            current_size = (
                canonical_sidecar.stat().st_size
                if canonical_sidecar.exists()
                else 0
            )
            if (
                self.control.get("sidecar_size") != current_size
                or "sidecar_seq" not in self.control
            ):
                self.control["sidecar_seq"] = raw._max_subagent_trace_seq(
                    str(canonical_sidecar)
                )
            raw._sidecar_last_seq[str(self.sidecar)] = self.control[
                "sidecar_seq"
            ]
        elif (
            self.control.get("sidecar_digest")
            == _digest(self.before.get(self.sidecar_relative))
            and "sidecar_seq" in self.control
        ):
            raw._sidecar_last_seq[str(self.sidecar)] = self.control[
                "sidecar_seq"
            ]

    def _receipt(self):
        from gc_core import thread_store as raw

        if self.kind in ("reset", "delete_thread"):
            self.control["generation"] += 1
            self.control["owner_id"] = None
            self.control["owner_epoch"] += 1
        self.control["revision"] += 1
        maximum = (
            self.control["max_seq"]
            if self.metadata_only or self.sidecar_only
            else raw.load_thread_messages(*self.args)[2]
        )
        if self.append_history:
            maximum = max(self.control["max_seq"], maximum)
        self.control["max_seq"] = maximum
        receipt = ThreadStoreReceipt(
            operation_id=self.operation.operation_id,
            kind=self.kind,
            input_digest=self.digest,
            result=_translate(self.result, self.stage, self.directory),
            written=self.written,
            max_seq=maximum,
            added_known_keys=sorted(self.keys - self.original_keys),
            meta=raw.load_thread_meta(
                str(self.directory), self.system_id, self.thread_id
            )
            if self.sidecar_only
            else raw.load_thread_meta(*self.args),
            archive=_translate(self.archive, self.stage, self.directory),
            **_versions(self.control),
        )
        return receipt

    def _read_after(self):
        self.after = {
            path.relative_to(self.stage): path.read_bytes()
            for path in _paths(
                self.stage,
                self.system_id,
                self.thread_id,
                include_archives=True,
            )
            if path.exists()
        }

    def _cache_sidecar(self):
        from gc_core import thread_store as raw

        cached_seq = raw._sidecar_last_seq.pop(str(self.sidecar), None)
        if cached_seq is not None:
            self.control["sidecar_seq"] = cached_seq
            self.control["sidecar_digest"] = _digest(
                self.after.get(self.sidecar_relative)
            )
            self.control["sidecar_size"] = self.append_paths.get(
                self.sidecar_relative, 0
            ) + len(self.after.get(self.sidecar_relative, b""))
        elif (
            not self.metadata_only
            and (not self.append_history)
            and (self.sidecar_relative not in self.after)
        ):
            self.control.pop("sidecar_seq", None)
            self.control.pop("sidecar_digest", None)

    def _changes(self):
        changes = [
            {
                "path": str(relative),
                "before": _digest(self.before.get(relative)),
                "after": base64.b64encode(self.after[relative]).decode()
                if relative in self.after
                else None,
            }
            for relative in sorted(self.before.keys() | self.after.keys())
            if relative not in self.append_paths
            and self.before.get(relative) != self.after.get(relative)
        ]
        changes.extend(
            (
                {
                    "path": str(relative),
                    "offset": offset,
                    "append": base64.b64encode(
                        self.after.get(relative, b"")
                    ).decode(),
                }
                for relative, offset in self.append_paths.items()
                if self.after.get(relative)
            )
        )
        changes.sort(key=lambda change: "offset" not in change)
        return changes

    def _apply_helper(self):
        function, values = self.helper
        values = copy.deepcopy(values)
        values["threads_dir"] = str(self.stage)
        self.original_keys = set(values.get("known_message_keys", []))
        self.result = function(**values)
        self.keys = values.get("known_message_keys", self.keys)
        self.written = (
            self.result
            if function.__name__ == "append_new_thread_messages"
            else 0
        )

    def _apply_persist(self):
        from gc_core import thread_store as raw

        if self.payload.get("known_keys") is None:
            self.keys = raw.load_thread_messages(
                str(self.directory), self.system_id, self.thread_id
            )[1]
            self.original_keys = set(self.keys)
        self.written = raw.append_new_thread_messages(
            *self.args,
            self.payload["messages"],
            self.keys,
            start_seq=self.payload.get("start_seq", 0),
        )
        if (
            "expected_written" in self.payload
            and self.written != self.payload["expected_written"]
        ):
            raise ThreadStoreConflict("Prepared message count changed")
        if self.kind == "persist":
            raw.save_thread_meta(*self.args, self.payload["meta"])
        self.result = self.written

    def _apply_archive(self):
        from gc_core import thread_store as raw

        self.archive = raw.archive_and_replace_thread_files(
            *self.args,
            self.payload["messages"],
            archive_suffix=self.payload.get("archive_suffix"),
        )
        if "meta" in self.payload:
            raw.save_thread_meta(*self.args, self.payload["meta"])
        self.result = self.archive

    def _apply_replace(self):
        from gc_core import thread_store as raw

        data_path, _ = raw._thread_paths(*self.args)
        Path(data_path).unlink(missing_ok=True)
        self.written = raw.append_new_thread_messages(
            *self.args, self.payload["messages"], set()
        )
        self.result = None

    def _apply_claim(self):
        desired = self.payload.get("owner_id", self.operation.owner_id)
        if not desired or self.control["owner_id"] not in (None, desired):
            raise ThreadStoreOwnershipError(
                "Thread already has a different owner"
            )
        if self.control["owner_id"] != desired:
            self.control["owner_epoch"] += 1
        self.control["owner_id"] = desired
        self.result = None

    def _apply_release(self):
        self.control["owner_id"] = None
        self.control["owner_epoch"] += 1
        self.result = None

    def _apply_simple(self):
        from gc_core import thread_store as raw

        methods = {
            "save_meta": raw.save_thread_meta,
            "patch_runtime_flag": raw.patch_runtime_flag,
            "reset_run_flags": raw.reset_run_flags,
            "update_runtime_meta": raw.update_runtime_meta,
            "discard_message": raw.discard_message_by_id,
            "reset": raw.reset_thread_files,
            "append_subagent_trace": raw.append_subagent_trace_record,
            "update_tui": raw.update_tui_meta,
        }
        if self.kind not in methods:
            raise ValueError(f"Unsupported durable operation: {self.kind}")
        self.result = methods[self.kind](*self.args, **self.payload)

    def _apply(self):
        self.payload = copy.deepcopy(self.operation.payload)
        self.keys = set(self.payload.get("known_keys") or [])
        self.original_keys = set(self.keys)
        self.written = 0
        self.archive = None
        self.kind = self.operation.kind
        self.args = (str(self.stage), self.system_id, self.thread_id)
        if self.helper is not None:
            self._apply_helper()
            return
        handlers = {
            "persist": self._apply_persist,
            "append_messages": self._apply_persist,
            "compact": self._apply_archive,
            "archive_and_replace": self._apply_archive,
            "replace_messages": self._apply_replace,
            "claim_owner": self._apply_claim,
            "release_owner": self._apply_release,
        }
        handlers.get(self.kind, self._apply_simple)()

    def _publish(self, receipt, changes):
        _write(
            self.base / "pending.json",
            _json(
                {
                    "changes": changes,
                    "identity": {
                        "system_id": self.system_id,
                        "thread_id": self.thread_id,
                    },
                    "control": self.control,
                    "receipt": asdict(receipt),
                }
            ),
        )
        _recover(self.directory, self.base)
        return receipt


def commit_operation(
    directory, system_id, thread_id, operation, *, helper=None
):
    directory = Path(directory).absolute()
    with thread_read_scope(directory, system_id, thread_id):
        return _FileTransaction(
            directory, system_id, thread_id, operation, helper
        ).commit()


def dispatch_file_helper(function, args, kwargs):
    if _active.get():
        return function(*args, **kwargs)
    values = inspect.signature(function).bind(*args, **kwargs)
    values.apply_defaults()
    arguments = values.arguments
    directory, system_id, thread_id = (
        arguments[name] for name in ("threads_dir", "system_id", "thread_id")
    )
    with thread_read_scope(directory, system_id, thread_id):
        if function.__name__ in _READERS:
            return function(*args, **kwargs)
        control = _control(_control_path(directory, system_id, thread_id))
        kind = {
            "reset_thread_files": "reset",
            "update_tui_meta": "update_tui",
        }.get(function.__name__, function.__name__)
        payload = {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in arguments.items()
            if key not in ("threads_dir", "system_id", "thread_id")
        }
        operation = ThreadStoreOperation(
            uuid.uuid4().hex, kind, payload, generation=control["generation"]
        )
        receipt = commit_operation(
            directory,
            system_id,
            thread_id,
            operation,
            helper=(function, arguments),
        )
        if "known_message_keys" in arguments:
            arguments["known_message_keys"].update(receipt.added_known_keys)
        session_meta = arguments.get("session_meta")
        if session_meta is not None:
            for key in ("runtime", "tui"):
                if key in receipt.meta:
                    session_meta[key] = copy.deepcopy(receipt.meta[key])
                else:
                    session_meta.pop(key, None)
        return receipt.result
