"""Файловое хранилище тредов v2 — в прежнем формате файлов develop.

Треды лежат там же и так же, как всегда (``gc_core.threads.file_layout``):
история ``data_*.jsonl``, meta ``meta_*.json`` (те же ключи — через
``meta_map``), трасса субагентов, журнал правок, архивы ``__pre_summary__``,
картинки ``images/``. Новое только добавляется: секция ``v2`` в
``.store/<h>/control.json`` (ревизия, аренда, высшие отметки потоков),
потоки ``stream_*`` и каталог ``aliases/``. Ключ ``owner_id`` старого журнала
не трогается — агент develop после отката пишет в такие треды как раньше.

Атомарность — журнал ``.store/`` (``file_tx``) под блокировкой треда
``thread_read_scope``. Несколько процессов на одной ФС безопасны, пока тред
пишет один из них (аренда); блокировкам на NFS доверять нельзя.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gc_core.thread_store_journal import (
    _control,
    _control_path,
    thread_read_scope,
)
from gc_core.threads import file_layout as layout
from gc_core.threads import file_tx
from gc_core.threads.errors import (
    OperationMismatch,
    OwnershipError,
    ThreadConflict,
)
from gc_core.threads.meta_map import (
    ARCHIVE_SUFFIX_KEY,
    meta_from_parts,
    parts_from_meta,
)
from gc_core.threads.models import (
    ArchiveInfo,
    BlobRef,
    JsonObject,
    Lease,
    LeaseRequest,
    Page,
    Receipt,
    SideChange,
    StreamEntry,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
    change_digest,
    check_stream_name,
)
from gc_core.threads.rules import (
    check_writer,
    granted_lease,
    page_of,
    same_lease,
)
from gc_core.threads.store import ThreadStore

_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
_EDITS = "edits"
_ALIASES_DIR = "aliases"
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"}
_SUFFIX_FORMAT = "%Y%m%d_%H%M%S"


def _line(record: JsonObject) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode()


def _json_row(line: str) -> JsonObject | None:
    row = line.strip()
    if not row:
        return None
    try:
        value = json.loads(row)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _jsonl(path: str | Path) -> list[JsonObject]:
    if not os.path.exists(path):
        return []
    rows: list[JsonObject] = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            row = line.strip()
            if not row:
                continue
            try:
                value = json.loads(row)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


class FileThreadStore(ThreadStore):
    """Треды в каталоге ``threads_dir`` (абсолютный путь из настроек)."""

    def __init__(
        self,
        threads_dir: str | Path,
        *,
        durability: file_tx.Durability = "atomic",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dir = str(Path(threads_dir).absolute())
        self._durability = durability
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def threads_dir(self) -> str:
        return self._dir

    def protected_paths(self) -> list[str]:
        """Данные хранилища на диске, к которым агенту нельзя прикасаться."""
        return [self._dir]

    # --- адреса и служебное ----------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    def _base(self, key: ThreadKey) -> Path:
        return _control_path(self._dir, key.system, key.thread)

    def _scope(self, key: ThreadKey):
        os.makedirs(self._dir, exist_ok=True)
        return thread_read_scope(self._dir, key.system, key.thread)

    def _v2(self, control: JsonObject) -> JsonObject:
        v2 = copy.deepcopy(control.get("v2") or {})
        v2.setdefault("revision", 0)
        v2.setdefault("generation", control.get("generation", 0))
        v2.setdefault("history_version", 0)
        v2.setdefault("epoch", control.get("owner_epoch", 0))
        v2.setdefault("lease", None)
        v2.setdefault("high", {})
        v2.setdefault("aliases", [])
        v2.setdefault("deleted", False)
        return v2

    @staticmethod
    def _lease(v2: JsonObject) -> Lease | None:
        raw = v2.get("lease")
        return None if raw is None else Lease.model_validate(raw)

    def _identity(self, key: ThreadKey) -> dict[str, str]:
        return {"system_id": key.system, "thread_id": key.thread}

    def _exists(self, key: ThreadKey, v2: JsonObject) -> bool:
        if v2.get("deleted"):
            return False
        files = (
            layout.meta_file(self._dir, key.system, key.thread),
            layout.history_file(self._dir, key.system, key.thread),
        )
        return "created_at" in v2 or any(os.path.exists(f) for f in files)

    def _load(self, key: ThreadKey) -> tuple[JsonObject, JsonObject]:
        """``control.json`` треда и его секция v2 — с учётом удаления
        старым кодом (``delete_thread`` убирает файлы, но не ``.store``)."""
        control = _control(self._base(key))
        v2 = self._v2(control)
        if self._deleted_outside(key, v2):
            v2 = self._v2(
                {
                    "v2": {
                        "generation": v2["generation"] + 1,
                        "epoch": v2["epoch"],
                        "deleted": True,
                    }
                }
            )
        return control, v2

    def _deleted_outside(self, key: ThreadKey, v2: JsonObject) -> bool:
        """У треда были записи, а файлов не осталось — его удалили мимо v2."""
        if v2.get("deleted") or not (
            v2["revision"] or any(v2["high"].values())
        ):
            return False
        s, t, d = key.system, key.thread, self._dir
        files = [
            layout.meta_file(d, s, t),
            layout.history_file(d, s, t),
            layout.subagent_trace_path(d, s, t),
            str(layout.edits_journal_path(d, s, t)),
            *(layout.stream_path(d, s, t, n) for n in v2["high"]),
        ]
        return not any(os.path.exists(f) for f in files)

    def _keep_caches(self, key, control, v2) -> None:
        """Сохранить кеши, посчитанные при чтении: иначе тред, созданный
        develop, пересчитывался бы на каждом шаге."""
        if control.get("v2") != v2:
            self._save_control(key, control, v2)

    def _save_control(self, key, control, v2) -> None:
        file_tx.write_control(
            self._base(key), {**control, "v2": v2}, self._durability
        )

    # --- потоки: чтение --------------------------------------------------------

    def _history(self, key: ThreadKey):
        path = layout.history_file(self._dir, key.system, key.thread)
        return layout.read_history_file(path, key.system, key.thread)

    def _entries(self, key: ThreadKey, stream: str) -> list[StreamEntry]:
        if stream == _HISTORY:
            records, _keys, _max = self._history(key)
            return [
                StreamEntry(seq=max(seq, n + 1), ts=ts, data=payload)
                for n, (seq, ts, payload) in enumerate(records)
            ]
        if stream == _SUBAGENTS:
            path = layout.subagent_trace_path(
                self._dir, key.system, key.thread
            )
            return [
                StreamEntry(
                    seq=int(row.get("seq") or n + 1),
                    ts=str(row.get("ts") or ""),
                    data={
                        k: v for k, v in row.items() if k not in ("ts", "seq")
                    },
                )
                for n, row in enumerate(_jsonl(path))
            ]
        if stream == _EDITS:
            path = layout.edits_journal_path(self._dir, key.system, key.thread)
            return [
                StreamEntry(seq=n + 1, ts=str(row.get("ts") or ""), data=row)
                for n, row in enumerate(_jsonl(path))
            ]
        path = layout.stream_path(self._dir, key.system, key.thread, stream)
        return [StreamEntry.model_validate(row) for row in _jsonl(path)]

    def _read_path(self, key: ThreadKey, stream: str) -> str:
        if stream == _HISTORY:
            return layout.history_file(self._dir, key.system, key.thread)
        return str(self._stream_file(key, stream))

    def _stats(
        self, key: ThreadKey, v2: JsonObject, stream: str
    ) -> JsonObject:
        """Число записей и max seq файла потока — из кеша ``control.json``.

        Кеш верен, пока размер файла тот же; иначе файл писали мимо v2
        (старый код, TUI) — пересчёт по файлу.
        """
        path = self._read_path(key, stream)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        cached = v2.setdefault("stats", {}).get(stream)
        if cached and cached["path"] == path and cached["size"] == size:
            return cached
        if cached and cached["path"] == path and cached["size"] < size:
            stats = self._grown(key, stream, cached)
        else:
            entries = self._entries(key, stream)
            stats = {
                "path": path,
                "size": size,
                "count": len(entries),
                "max": max((e.seq for e in entries), default=0),
            }
        v2["stats"][stream] = stats
        return stats

    def _grown(self, key, stream: str, cached: JsonObject) -> JsonObject:
        """Файл дописали мимо v2: досчитать только хвост, и только целые
        строки — строку, которую пишут прямо сейчас, оставить на потом."""
        with open(cached["path"], "rb") as handle:
            handle.seek(cached["size"])
            tail = handle.read()
        complete = tail[: tail.rfind(b"\n") + 1]
        lines = complete.decode("utf-8").splitlines()
        count, top = cached["count"], cached["max"]
        if stream == _HISTORY:
            records, _keys, _max = layout.read_history_lines(
                lines, key.system, key.thread
            )
            count += len(records)
            top = max([top, *(r[0] for r in records)])
        else:
            rows = [r for r in (_json_row(line) for line in lines) if r]
            count += len(rows)
            seqs = [int(r.get("seq") or 0) for r in rows]
            top = count if stream == _EDITS else max([top, *seqs])
        return {
            "path": cached["path"],
            "size": cached["size"] + len(complete),
            "count": count,
            "max": top,
        }

    def _high(self, key: ThreadKey, v2: JsonObject, stream: str) -> int:
        """Высшая выданная отметка потока: номер удалённого хвоста не
        возвращается, а запись мимо v2 учитывается по файлу."""
        stats = self._stats(key, v2, stream)
        return max(int(v2["high"].get(stream, 0)), stats["max"])

    def _after_append(self, v2, key, stream, tail: bytes, added: int, seq):
        path = (
            self._stream_file(key, stream)
            if stream != _HISTORY
            else (layout.thread_paths(self._dir, key.system, key.thread)[0])
        )
        stats = self._stats(key, v2, stream)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        v2["stats"][stream] = {
            "path": str(path),
            "size": size + len(tail),
            "count": stats["count"] + added,
            "max": max(stats["max"], seq),
        }
        v2["high"][stream] = max(int(v2["high"].get(stream, 0)), seq)

    # --- запись потоков ----------------------------------------------------------

    def _stream_file(self, key: ThreadKey, stream: str) -> str | Path:
        if stream == _SUBAGENTS:
            return layout.subagent_trace_path(
                self._dir, key.system, key.thread
            )
        if stream == _EDITS:
            return layout.edits_journal_path(self._dir, key.system, key.thread)
        return layout.stream_path(self._dir, key.system, key.thread, stream)

    def _stream_line(self, stream: str, seq: int, ts: str, data) -> bytes:
        if stream == _SUBAGENTS:
            return _line({"ts": ts, "seq": seq, **data})
        if stream == _EDITS:
            return _line(data)
        return _line({"seq": seq, "ts": ts, "data": data})

    def _append_stream(self, changes, key, v2, stream, items) -> None:
        seq, ts = self._high(key, v2, stream), self._now()
        tail = b""
        for item in items:
            seq += 1
            tail += self._stream_line(stream, seq, ts, item)
        changes.append(self._stream_file(key, stream), tail)
        self._after_append(v2, key, stream, tail, len(items), seq)

    # --- квитанции и алиасы ------------------------------------------------------

    def _replayed(self, base: Path, change) -> Receipt | None:
        stored = file_tx.read_receipt(base, change.op_id)
        if stored is None:
            return None
        if stored.get("digest") != change_digest(change):
            raise OperationMismatch(
                f"op_id {change.op_id!r} уже применён с другим содержимым"
            )
        return Receipt.model_validate(stored["receipt"])

    def _receipt_doc(self, change, receipt: Receipt) -> JsonObject:
        return {
            "operation_id": change.op_id,
            "v2": 1,
            "digest": change_digest(change),
            "at": self._now(),
            "receipt": receipt.model_dump(mode="json"),
        }

    def _alias_path(self, alias: str) -> Path:
        name = hashlib.sha256(alias.encode()).hexdigest() + ".json"
        return Path(self._dir) / _ALIASES_DIR / name

    def _alias_owner(self, alias: str) -> ThreadKey | None:
        path = self._alias_path(alias)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ThreadKey(system=raw["system"], thread=raw["thread"])

    def _add_aliases(self, changes, key, v2, aliases) -> None:
        for alias in aliases:
            owner = self._alias_owner(alias)
            if owner is not None and owner != key:
                raise ValueError(
                    f"алиас {alias!r} принадлежит треду "
                    f"{owner.system}/{owner.thread}"
                )
        for alias in aliases:
            doc = {"alias": alias, "system": key.system, "thread": key.thread}
            changes.put(self._alias_path(alias), _line(doc))
            if alias not in v2["aliases"]:
                v2["aliases"].append(alias)

    # --- meta --------------------------------------------------------------------

    def _meta(self, key: ThreadKey) -> JsonObject:
        return layout.read_meta_file(
            layout.meta_file(self._dir, key.system, key.thread)
        )

    def _write_meta(self, changes, key, meta, v2, *, touch: bool) -> None:
        stored = self._meta(key)
        now = self._now()
        stamped = layout.stamp_meta(
            meta,
            key.system,
            key.thread,
            now if touch else stored.get("updated_at") or now,
        )
        # created_at — время создания треда; его ставит только хранилище.
        stamped["created_at"] = (
            stored.get("created_at") or v2.get("created_at") or now
        )
        _, path = layout.thread_paths(self._dir, key.system, key.thread)
        changes.put(path, layout.meta_text(stamped).encode())

    # --- запись истории ------------------------------------------------------------

    def _record(self, key: ThreadKey, v2: JsonObject) -> ThreadRecord:
        meta = self._meta(key)
        parts = parts_from_meta(meta)
        info = parts.info.model_copy(
            update={
                "created_at": meta.get("created_at") or v2.get("created_at"),
                "updated_at": meta.get("updated_at") or v2.get("created_at"),
                "messages_count": self._stats(key, v2, _HISTORY)["count"],
            }
        )
        streams = {_HISTORY, _SUBAGENTS, _EDITS, *v2["high"]}
        high = {s: self._high(key, v2, s) for s in streams}
        return ThreadRecord(
            key=key,
            info=info,
            state=parts.state,
            usage=parts.usage,
            runtime=parts.runtime,
            frontend=parts.frontend,
            archives=self._archives(key, v2),
            revision=v2["revision"],
            generation=v2["generation"],
            history_version=v2["history_version"],
            max_seq={s: n for s, n in high.items() if n},
            lease=self._lease(v2),
            extra=parts.extra,
        )

    def _archives(self, key: ThreadKey, v2: JsonObject) -> list[ArchiveInfo]:
        """Архивы треда; список кешируется в ``control.json`` (обход каталога
        тредов дорог, а архив появляется только при сжатии через v2)."""
        if "archives" not in v2:
            v2["archives"] = [
                a.model_dump(mode="json") for a in self._scan_archives(key)
            ]
        return [ArchiveInfo.model_validate(a) for a in v2["archives"]]

    def _scan_archives(self, key: ThreadKey) -> list[ArchiveInfo]:
        infos = []
        for suffix in layout.archive_suffixes(
            self._dir, key.system, key.thread
        ):
            data, _meta = layout.archive_paths(
                self._dir, key.system, key.thread, suffix
            )
            records, _keys, _max = layout.read_history_file(
                data, key.system, key.thread
            )
            stamp = datetime.strptime(suffix[:15], _SUFFIX_FORMAT)
            infos.append(
                ArchiveInfo(
                    id=suffix,
                    created_at=stamp.replace(tzinfo=UTC).isoformat(),
                    messages_count=len(records),
                )
            )
        return infos

    # --- необязательное: ключи истории для старой сессии -------------------------

    def history_keys(self, key: ThreadKey) -> set[str]:
        """Ключи сообщений истории, включая ключи записей «как есть» до
        синтеза id у старых записей — их сверяет дедупликация сессии."""
        with self._scope(key):
            _records, keys, _max = self._history(key)
        return keys

    # --- чтение --------------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        with self._scope(key):
            control, v2 = self._load(key)
            if not self._exists(key, v2):
                self._keep_caches(key, control, v2)
                return None
            record = self._record(key, v2)
            self._keep_caches(key, control, v2)
            return record

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        records = []
        for key in self._known_keys():
            record = await self.get_thread(key)
            if record is not None:
                records.append(record)
        return page_of(records, query)

    def _known_keys(self) -> set[ThreadKey]:
        keys: set[ThreadKey] = set()
        root = Path(self._dir)
        for path in root.glob("meta_*.json") if root.is_dir() else ():
            if layout.ARCHIVE_MARK in path.name:
                continue
            meta = layout.read_meta_file(str(path))
            if meta.get("system") and meta.get("thread_id"):
                keys.add(
                    ThreadKey(system=meta["system"], thread=meta["thread_id"])
                )
        for identity in root.glob(".store/*/identity.json"):
            raw = json.loads(identity.read_text(encoding="utf-8"))
            keys.add(
                ThreadKey(system=raw["system_id"], thread=raw["thread_id"])
            )
        return keys

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        with self._scope(key):
            _control_doc, v2 = self._load(key)
            if v2.get("deleted"):
                return []
            fresh = [
                e for e in self._entries(key, stream) if e.seq > after_seq
            ]
        return fresh if limit is None else fresh[:limit]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        data, _meta = layout.archive_paths(
            self._dir, key.system, key.thread, archive_id
        )
        records, _keys, _max = layout.read_history_file(
            data, key.system, key.thread
        )
        return [
            StreamEntry(seq=max(seq, n + 1), ts=ts, data=payload)
            for n, (seq, ts, payload) in enumerate(records)
        ]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        return self._alias_owner(alias)

    # --- запись ---------------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        with self._scope(key):
            base = self._base(key)
            replayed = self._replayed(base, change)
            if replayed is not None:
                return replayed
            control, v2 = self._load(key)
            current = self._lease(v2) if self._exists(key, v2) else None
            _check_durable_owner(control, change.lease.owner)
            check_writer(current, change.lease, self._clock())
            if change.expected_revision != v2["revision"]:
                raise ThreadConflict(
                    f"ревизия {v2['revision']}, "
                    f"ожидалась {change.expected_revision}"
                )
            return _Commit(self, key, control, v2, change).run()

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        for stream, _items in change.append:
            if check_stream_name(stream) == _HISTORY:
                raise ValueError("историю треда пишет только commit")
        with self._scope(key):
            base = self._base(key)
            replayed = self._replayed(base, change)
            if replayed is not None:
                return replayed
            control, v2 = self._load(key)
            if not self._exists(key, v2):
                v2 = self._created(v2)
            return self._side(key, control, v2, change)

    def _created(self, v2: JsonObject) -> JsonObject:
        return {
            **v2,
            "created_at": self._now(),
            "deleted": False,
            "high": {},
            "stats": {},
            "archives": [],
            "aliases": [],
            "lease": None,
        }

    def _side(self, key, control, v2, change: SideChange) -> Receipt:
        changes = file_tx.Changes(Path(self._dir))
        self._add_aliases(changes, key, v2, change.add_aliases)
        for stream, items in change.append:
            self._append_stream(changes, key, v2, stream, items)
        if change.set_runtime is not None or change.set_frontend is not None:
            meta = self._meta(key)
            parts = parts_from_meta(meta)
            if change.set_runtime is not None:
                parts = parts.with_(runtime=change.set_runtime)
            if change.set_frontend is not None:
                name, state = change.set_frontend
                parts = parts.with_(frontend={**parts.frontend, name: state})
            meta = meta_from_parts(parts, base=meta)
            self._write_meta(changes, key, meta, v2, touch=False)
        receipt = Receipt(
            op_id=change.op_id,
            revision=v2["revision"],
            generation=v2["generation"],
            history_version=v2["history_version"],
            max_seq={s: n for s, n in v2["high"].items() if n},
        )
        file_tx.publish(
            Path(self._dir),
            self._base(key),
            changes=changes,
            control={**control, "v2": v2},
            receipt=self._receipt_doc(change, receipt),
            identity=self._identity(key),
            durability=self._durability,
        )
        return receipt

    # --- аренда и жизненный цикл --------------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        with self._scope(key):
            control, v2 = self._load(key)
            if not self._exists(key, v2):
                v2 = self._created(v2)
            _check_durable_owner(control, lease.owner)
            granted = granted_lease(
                self._lease(v2), v2["epoch"], lease, self._clock()
            )
            v2["epoch"] = granted.epoch
            v2["lease"] = granted.model_dump(mode="json")
            if granted.expires_at is None:
                # Бессрочная аренда (durable-исполнитель) видна и старому
                # коду: писатель develop не пишет в тред с ``owner_id``.
                control = {
                    **control,
                    "owner_id": granted.owner,
                    "owner_epoch": granted.epoch,
                }
            self._save_control(key, control, v2)
            return self._record(key, v2)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        with self._scope(key):
            control, v2 = self._load(key)
            if same_lease(self._lease(v2), lease):
                v2["lease"] = None
                if control.get("owner_id") == lease.owner:
                    control = {**control, "owner_id": None}
                self._save_control(key, control, v2)

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        with self._scope(key):
            base = self._base(key)
            control, v2 = self._load(key)
            if not self._exists(key, v2):
                return
            changes = file_tx.Changes(Path(self._dir))
            for path in self._thread_files(key, v2):
                changes.put(path, None)
            tomb = {
                "generation": v2["generation"] + 1,
                "epoch": v2["epoch"],
                "deleted": True,
            }
            file_tx.publish(
                Path(self._dir),
                base,
                changes=changes,
                control={**control, "v2": tomb},
                receipt={"operation_id": f"delete:{op_id}", "v2": 1},
                identity=self._identity(key),
                durability=self._durability,
            )
            for receipt in (base / file_tx.RECEIPTS).glob("*.json"):
                receipt.unlink()

    def _thread_files(self, key: ThreadKey, v2: JsonObject) -> list[Path]:
        s, t, d = key.system, key.thread, self._dir
        stem = f"{s}_{t}"
        files = [
            *layout.thread_paths(d, s, t),
            *layout.legacy_thread_paths(d, t),
            layout.subagent_trace_path(d, s, t),
            layout.edits_journal_path(d, s, t),
            os.path.join(d, f"touched_{stem}.json"),
            os.path.join(d, "locks", f"lock_{stem}.json"),
        ]
        for suffix in layout.archive_suffixes(d, s, t):
            files.extend(layout.archive_paths(d, s, t, suffix))
        root = Path(d)
        files.extend(root.glob(f"stream_*_{stem}.jsonl"))
        files.extend((root / layout.IMAGES_DIR).glob(f"{stem}_*"))
        files.extend(self._alias_path(a) for a in v2["aliases"])
        return [Path(f) for f in files if Path(f).exists()]

    # --- вложения и обслуживание ----------------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = f"{key.system}_{key.thread}_{digest}{_EXTENSIONS.get(mime, '.bin')}"
        path = Path(self._dir) / layout.IMAGES_DIR / name
        if not path.exists():
            file_tx._plain_write(path, data)
        return BlobRef(key=key, name=name, mime=mime, location=str(path))

    async def get_blob(self, ref: BlobRef) -> bytes:
        path = Path(self._dir) / layout.IMAGES_DIR / ref.name
        if not path.exists():
            raise FileNotFoundError(ref.location)
        return path.read_bytes()

    async def prune_receipts(self, older_than: timedelta) -> int:
        cutoff = self._clock() - older_than
        removed = 0
        for path in Path(self._dir).glob(".store/*/receipts/*.json"):
            doc = json.loads(path.read_bytes())
            if (
                doc.get("v2")
                and doc.get("at")
                and (datetime.fromisoformat(doc["at"]) < cutoff)
            ):
                path.unlink()
                removed += 1
        return removed


def _check_durable_owner(control: JsonObject, owner: str) -> None:
    """Тред, которым владеет durable-исполнитель журнала (``owner_id``,
    Temporal), пишет только он сам."""
    durable = control.get("owner_id")
    if durable and durable != owner:
        raise OwnershipError(
            f"тред ведёт durable-исполнитель {durable!r}; "
            "локальная запись запрещена"
        )


class _Commit:
    """Одна запись истории и состояния: изменения файлов и новый control.

    История целиком читается только для перезаписи (сброс, замена, удаление
    сообщения, перенос старого файла); обычный шаг — дописывание по кешу.
    """

    def __init__(self, store, key, control, v2, change: ThreadChange) -> None:
        self.store, self.key, self.control = store, key, control
        self.v2, self.change = v2, change
        self.changes = file_tx.Changes(Path(store.threads_dir))
        self.archive_id: str | None = None
        self.history: list | None = None  # читается лениво
        self.renumbered = False
        self.discarded: set[str] = set()
        self.fresh: list = []
        data, _meta = layout.thread_paths(
            store.threads_dir, key.system, key.thread
        )
        self.canonical = data
        # Тред в старом формате без system в имени: первая запись переносит
        # историю в канонический файл целиком.
        source = layout.history_file(store.threads_dir, key.system, key.thread)
        self.from_legacy = source != data and os.path.exists(source)
        self.rewrite = self.from_legacy

    def _loaded(self) -> list:
        if self.history is None:
            records, _keys, _max = self.store._history(self.key)
            self.history = list(records)
        return self.history

    def run(self) -> Receipt:
        change = self.change
        if change.reset_history:
            self._reset()
        if change.replace_history is not None:
            self._replace()
        if change.discard_message_ids:
            self._discard(set(change.discard_message_ids))
        self._append(change.append_messages)
        self.store._add_aliases(
            self.changes, self.key, self.v2, change.add_aliases
        )
        self._write_history()
        self._write_meta()
        self.v2["revision"] += 1
        receipt = Receipt(
            op_id=change.op_id,
            revision=self.v2["revision"],
            generation=self.v2["generation"],
            history_version=self.v2["history_version"],
            max_seq={s: n for s, n in self.v2["high"].items() if n},
            archive_id=self.archive_id,
            lease=self.store._lease(self.v2),
        )
        file_tx.publish(
            Path(self.store.threads_dir),
            self.store._base(self.key),
            changes=self.changes,
            control={**self.control, "v2": self.v2},
            receipt=self.store._receipt_doc(change, receipt),
            identity=self.store._identity(self.key),
            durability=self.store._durability,
        )
        return receipt

    def _renumber(self, payloads: list[JsonObject]) -> None:
        ts = self.store._now()
        self.history = [(n + 1, ts, p) for n, p in enumerate(payloads)]
        self.v2["high"][_HISTORY] = len(payloads)
        self.v2["history_version"] += 1
        self.rewrite = self.renumbered = True

    def _reset(self) -> None:
        self._renumber([])
        self.v2["generation"] += 1
        self.v2["lease"] = None

    def _replace(self) -> None:
        compaction = self.change.replace_history
        if compaction.archive:
            self._archive()
        self._renumber(list(compaction.messages))

    def _archive(self) -> None:
        store, key = self.store, self.key
        d, s, t = store.threads_dir, key.system, key.thread
        stamp = store._clock().strftime(_SUFFIX_FORMAT)
        suffix, n = stamp, 1
        while os.path.exists(layout.archive_paths(d, s, t, suffix)[0]):
            n += 1
            suffix = f"{stamp}_{n}"
        archived_data, archived_meta = layout.archive_paths(d, s, t, suffix)
        source = layout.history_file(d, s, t)
        if os.path.exists(source):
            self.changes.put(archived_data, Path(source).read_bytes())
        meta_source = layout.meta_file(d, s, t)
        if os.path.exists(meta_source):
            self.changes.put(archived_meta, Path(meta_source).read_bytes())
        archives = store._archives(key, self.v2)
        archives.append(
            ArchiveInfo(
                id=suffix,
                created_at=store._clock().isoformat(),
                messages_count=len(self._loaded()),
            )
        )
        self.v2["archives"] = [a.model_dump(mode="json") for a in archives]
        self.archive_id = suffix

    def _discard(self, ids: set[str]) -> None:
        self.history = [r for r in self._loaded() if r[2].get("id") not in ids]
        self.discarded |= ids
        self.rewrite = True

    def _append(self, messages: list[JsonObject]) -> None:
        # После замены или сброса файл ещё старый: нумерация — от новой
        # истории этой же записи, а не от файла.
        seq = (
            self.v2["high"][_HISTORY]
            if self.renumbered
            else self.store._high(self.key, self.v2, _HISTORY)
        )
        ts = self.store._now()
        for message in messages:
            seq += 1
            self.fresh.append((seq, ts, copy.deepcopy(message)))
        self.v2["high"][_HISTORY] = max(seq, self.v2["high"].get(_HISTORY, 0))

    def _write_history(self) -> None:
        s, t = self.key.system, self.key.thread
        tail = b"".join(
            layout.history_line(s, t, seq, ts, payload).encode()
            for seq, ts, payload in self.fresh
        )
        if not self.rewrite:
            self.store._after_append(
                self.v2,
                self.key,
                _HISTORY,
                tail,
                len(self.fresh),
                self.fresh[-1][0] if self.fresh else 0,
            )
            self.changes.append(self.canonical, tail)
            self.count = self.v2["stats"][_HISTORY]["count"]
            return
        kept, count = self._kept()
        whole = kept + tail
        # Тред старого формата: пустой канонический файл, а не его
        # отсутствие — иначе чтение вернулось бы к старому файлу.
        self.changes.put(
            self.canonical, whole if whole or self.from_legacy else None
        )
        self.count = count + len(self.fresh)
        self.v2.setdefault("stats", {})[_HISTORY] = {
            "path": self.canonical,
            "size": len(whole),
            "count": self.count,
            "max": max(
                [r[0] for r in self._loaded()]
                + [r[0] for r in self.fresh]
                + [0]
            ),
        }

    def _kept(self) -> tuple[bytes, int]:
        """История, которая остаётся: после перенумерации — заново, иначе —
        исходные строки файла как есть, без удалённых (как develop)."""
        s, t = self.key.system, self.key.thread
        if self.renumbered:
            body = b"".join(
                layout.history_line(s, t, seq, ts, p).encode()
                for seq, ts, p in self.history
            )
            return body, len(self.history)
        source = layout.history_file(self.store.threads_dir, s, t)
        kept = []
        with open(source, encoding="utf-8") as stream:
            for line in stream:
                row = line.strip()
                if row and self._line_id(row) not in self.discarded:
                    kept.append(row + "\n")
        return "".join(kept).encode(), len(self._loaded())

    def _line_id(self, row: str) -> str | None:
        records, _keys, _max = layout.read_history_lines(
            [row], self.key.system, self.key.thread
        )
        return records[0][2].get("id") if records else None

    def _write_meta(self) -> None:
        change, key = self.change, self.key
        meta = self.store._meta(key)
        parts = parts_from_meta(meta)
        updates: dict[str, Any] = {}
        for field, value in (
            ("info", change.set_info),
            ("state", change.set_state),
            ("usage", change.set_usage),
            ("runtime", change.set_runtime),
            ("extra", change.set_extra),
        ):
            if value is not None:
                updates[field] = value
        new_meta = meta_from_parts(parts.with_(**updates), base=meta)
        new_meta["messages_count"] = self.count
        if self.archive_id is not None:
            new_meta[ARCHIVE_SUFFIX_KEY] = self.archive_id
        self.store._write_meta(
            self.changes, key, new_meta, self.v2, touch=True
        )
