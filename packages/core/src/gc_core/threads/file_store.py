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

import contextlib
import copy
import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gc_core.thread_store_journal import (
    _control,
    _control_path,
    _physical_control_path,
    _physical_read_scope,
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
    patched_frontend,
    same_lease,
)
from gc_core.threads.store import ThreadStore

_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
_EDITS = "edits"
_ALIASES_DIR = "aliases"
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"}
_SUFFIX_FORMAT = "%Y%m%d_%H%M%S"
_JSON = ".json"

logger = logging.getLogger(__name__)


def _line(record: JsonObject) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode()


#: Сколько последних записей потока помнит индекс смещений.
_RECENT = 256


def _recent(previous: list, fresh: list) -> list:
    return [*previous, *fresh][-_RECENT:]


def _resume_offset(cached: JsonObject, after_seq: int | None) -> int | None:
    """Откуда читать, чтобы отдать записи после ``after_seq``; ``None`` —
    только целиком (читатель отстал дальше индекса последних записей)."""
    if after_seq is None or after_seq >= cached["max"]:
        return cached["size"]
    recent = cached.get("recent") or []
    if not recent or after_seq < recent[0][0] - 1:
        return None
    return next((off for seq, off in recent if seq > after_seq), None)


def _read_plan(cached, path: str, size: int, after_seq):
    """Кеш, на который можно опереться, и смещение, с которого читать:
    ``(None, 0)`` — читать целиком."""
    if not cached or cached["path"] != path or cached["size"] > size:
        return None, 0
    start = _resume_offset(cached, after_seq)
    return (None, 0) if start is None else (cached, start)


def _count_before(cached, start: int) -> int:
    """Сколько записей лежит до смещения ``start``."""
    if cached is None:
        return 0
    known = [r for r in cached.get("recent") or [] if r[1] >= start]
    return cached["count"] - len(known)


def _grown_stats(cached, path: str, size: int, pairs) -> JsonObject:
    """Статистика после чтения: новыми считаются строки за концом кеша."""
    fresh = [p for p in pairs if cached is None or p[1] >= cached["size"]]
    return {
        "path": path,
        "size": size,
        "count": (cached["count"] if cached else 0) + len(fresh),
        "max": max(
            [cached["max"] if cached else 0, *(e.seq for e, _ in fresh)]
        ),
        "recent": _recent(
            (cached.get("recent") or []) if cached else [],
            [[e.seq, offset] for e, offset in fresh],
        ),
    }


def _read_lines(path: str, offset: int) -> tuple[list[tuple[str, int]], int]:
    """Целые строки файла с байтового смещения — пары (текст, смещение
    строки) — и сколько байт они заняли: строку, которую дописывают прямо
    сейчас, оставляем на следующее чтение.

    Последняя строка без перевода строки — целая, если это целый JSON-объект
    (так её читал develop): недописанный объект не разбирается, а дописанный
    без ``\\n`` — уже готовая запись."""
    if not os.path.exists(path):
        return [], 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        tail = handle.read()
    complete = tail[: tail.rfind(b"\n") + 1]
    rows, position = [], offset
    for raw in complete.split(b"\n")[:-1]:
        rows.append((raw.decode("utf-8"), position))
        position += len(raw) + 1
    rest = tail[len(complete) :]
    if rest.strip() and _json_row(rest.decode("utf-8", errors="replace")):
        rows.append((rest.decode("utf-8"), position))
        complete = tail
    return rows, len(complete)


def _json_row(line: str) -> JsonObject | None:
    row = line.strip()
    if not row:
        return None
    try:
        value = json.loads(row)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
        #: Кеши, посчитанные при чтении (статистика потоков, архивы), — в
        #: памяти: чтение не пишет на диск; в control.json они попадают с
        #: ближайшей записью.
        self._memo: dict[ThreadKey, JsonObject] = {}

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

    @contextlib.contextmanager
    def _read_scope(self, key: ThreadKey):
        """Чтение: блокировка только у треда, у которого уже есть ``.store``
        (там может ждать доигрыш прерванной записи). Каталогов не создаёт и
        identity не пишет — чтение по чужому id ничего не меняет на диске."""
        bases = sorted(
            {
                base
                for base in (
                    self._base(key),
                    _physical_control_path(self._dir, key.thread),
                )
                if base.is_dir()
            }
        )
        with contextlib.ExitStack() as stack:
            for base in bases:
                stack.enter_context(_physical_read_scope(self._dir, base))
            yield

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
        self._with_memo(key, v2)
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

    def _with_memo(self, key: ThreadKey, v2: JsonObject) -> None:
        """Подмешать кеши из памяти: из двух записей одного файла берётся та,
        что дочитана дальше; список архивов из control.json главнее."""
        memo = self._memo.get(key) or {}
        stats = v2.setdefault("stats", {})
        for stream, cached in (memo.get("stats") or {}).items():
            current = stats.get(stream)
            if (
                current is None
                or current["path"] != cached["path"]
                or current["size"] < cached["size"]
            ):
                stats[stream] = cached
        if "archives" not in v2 and "archives" in memo:
            v2["archives"] = memo["archives"]

    def _forget(self, key: ThreadKey) -> None:
        self._memo.pop(key, None)

    def _save_control(self, key, control, v2) -> None:
        file_tx.write_control(
            self._base(key), {**control, "v2": v2}, self._durability
        )

    # --- потоки: чтение --------------------------------------------------------

    def _history(self, key: ThreadKey):
        path = layout.history_file(self._dir, key.system, key.thread)
        return layout.read_history_file(path, key.system, key.thread)

    def _read_path(self, key: ThreadKey, stream: str) -> str:
        if stream == _HISTORY:
            return layout.history_file(self._dir, key.system, key.thread)
        return str(self._stream_file(key, stream))

    def _parse(
        self, key: ThreadKey, stream: str, rows, start: int
    ) -> list[tuple[StreamEntry, int]]:
        """Записи потока со смещениями их строк; ``start`` — сколько записей
        до первой строки (журнал правок нумеруется по строкам)."""
        parsed: list[tuple[StreamEntry, int]] = []
        for text, offset in rows:
            entry = self._entry(key, stream, text, start + len(parsed) + 1)
            if entry is not None:
                parsed.append((entry, offset))
        return parsed

    def _entry(
        self, key: ThreadKey, stream: str, text: str, number: int
    ) -> StreamEntry | None:
        if stream == _HISTORY:
            records, _keys, _max = layout.read_history_lines(
                [text], key.system, key.thread
            )
            if not records:
                return None
            seq, ts, payload = records[0]
            return StreamEntry(
                seq=seq if seq >= 1 else number, ts=ts, data=payload
            )
        row = _json_row(text)
        if row is None:
            return None
        if stream == _SUBAGENTS:
            data = {k: v for k, v in row.items() if k not in ("ts", "seq")}
            return StreamEntry(
                seq=int(row.get("seq") or number),
                ts=str(row.get("ts") or ""),
                data=data,
            )
        if stream == _EDITS:
            return StreamEntry(
                seq=number, ts=str(row.get("ts") or ""), data=row
            )
        return StreamEntry.model_validate(row)

    def _entries(self, key: ThreadKey, stream: str) -> list[StreamEntry]:
        rows, _used = _read_lines(self._read_path(key, stream), 0)
        return [entry for entry, _offset in self._parse(key, stream, rows, 0)]

    def _scan(
        self,
        key: ThreadKey,
        v2: JsonObject,
        stream: str,
        *,
        after_seq: int | None = None,
    ) -> tuple[JsonObject, list[StreamEntry]]:
        """Статистика потока (число записей, max seq) и, если задан
        ``after_seq``, записи после него — с минимальным чтением файла.

        Кеш (путь, байты целых строк, число, max и смещения последних
        записей) верен, пока файл тот же: не изменился — не читаем вовсе;
        дописан (в том числе мимо v2) или читатель отстал на последние
        записи — читаем с известного смещения; перезаписан или читатель
        отстал дальше — целиком.
        """
        path = self._read_path(key, stream)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        cached, start = _read_plan(
            v2.setdefault("stats", {}).get(stream), path, size, after_seq
        )
        if cached is not None and start == cached["size"] == size:
            return cached, []
        rows, used = _read_lines(path, start)
        before = _count_before(cached, start)
        pairs = self._parse(key, stream, rows, before)
        stats = _grown_stats(cached, path, start + used, pairs)
        self._remember_stats(key, v2, stream, stats)
        if after_seq is None:
            return stats, []
        return stats, [e for e, _ in pairs if e.seq > after_seq]

    def _stats(
        self, key: ThreadKey, v2: JsonObject, stream: str
    ) -> JsonObject:
        return self._scan(key, v2, stream)[0]

    def _remember_stats(self, key, v2, stream, stats) -> None:
        v2["stats"][stream] = stats
        self._memo.setdefault(key, {}).setdefault("stats", {})[stream] = stats

    def _high(self, key: ThreadKey, v2: JsonObject, stream: str) -> int:
        """Высшая выданная отметка потока: номер удалённого хвоста не
        возвращается, а запись мимо v2 учитывается по файлу."""
        stats = self._stats(key, v2, stream)
        return max(int(v2["high"].get(stream, 0)), stats["max"])

    def _after_append(self, v2, key, stream, lines, seqs) -> None:
        """Кеш потока после своего дописывания: смещения строк известны."""
        path = (
            self._stream_file(key, stream)
            if stream != _HISTORY
            else (layout.thread_paths(self._dir, key.system, key.thread)[0])
        )
        stats = self._stats(key, v2, stream)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        # Разделитель, который file_tx поставит перед хвостом.
        offsets, position = [], size + int(file_tx.unterminated(path))
        for line in lines:
            offsets.append(position)
            position += len(line)
        v2["stats"][stream] = {
            "path": str(path),
            "size": position,
            "count": stats["count"] + len(lines),
            "max": max([stats["max"], *seqs]),
            "recent": _recent(
                stats.get("recent") or [],
                [[seq, off] for seq, off in zip(seqs, offsets, strict=True)],
            ),
        }
        if seqs:
            v2["high"][stream] = max(int(v2["high"].get(stream, 0)), seqs[-1])

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
        lines, seqs = [], []
        for item in items:
            seq += 1
            lines.append(self._stream_line(stream, seq, ts, item))
            seqs.append(seq)
        changes.append(self._stream_file(key, stream), b"".join(lines))
        self._after_append(v2, key, stream, lines, seqs)

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
        name = hashlib.sha256(alias.encode()).hexdigest() + _JSON
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

    def _record(
        self, key: ThreadKey, v2: JsonObject, *, listing: bool = False
    ) -> ThreadRecord:
        """Запись треда. ``listing`` — для списка: поток, который v2 ещё не
        читал (тред develop), не читается целиком — число сообщений берётся
        из meta (его и показывал develop), отметка — по последней строке
        файла, архивы — только из кеша."""
        meta = self._meta(key)
        parts = parts_from_meta(meta)
        info = parts.info.model_copy(
            update={
                "created_at": meta.get("created_at") or v2.get("created_at"),
                "updated_at": meta.get("updated_at") or v2.get("created_at"),
                "messages_count": self._count(key, v2, meta, listing),
            }
        )
        streams = {_HISTORY, _SUBAGENTS, _EDITS, *v2["high"]}
        if listing:
            high = {s: self._listed_high(key, v2, s) for s in streams}
            archives = [
                ArchiveInfo.model_validate(a) for a in v2.get("archives", [])
            ]
        else:
            high = {s: self._high(key, v2, s) for s in streams}
            archives = self._archives(key, v2, meta)
        return ThreadRecord(
            key=key,
            info=info,
            state=parts.state,
            usage=parts.usage,
            runtime=parts.runtime,
            frontend=parts.frontend,
            archives=archives,
            revision=v2["revision"],
            generation=v2["generation"],
            history_version=v2["history_version"],
            max_seq={s: n for s, n in high.items() if n},
            lease=self._lease(v2),
            extra=parts.extra,
        )

    def _archives(
        self, key: ThreadKey, v2: JsonObject, meta: JsonObject
    ) -> list[ArchiveInfo]:
        """Архивы треда; список кешируется (обход каталога тредов дорог).

        Кеш сверяется с отметкой последнего сжатия в meta: develop после
        отката сжимает тред мимо v2, и архива из отметки в кеше нет."""
        marker = meta.get(ARCHIVE_SUFFIX_KEY)
        if not _knows(v2.get("archives"), marker):
            memo = self._memo.setdefault(key, {})
            # Отметка, для которой каталог уже обходили: архив из неё мог
            # убрать уборщик — второй обход ничего не даст.
            if "archives" in memo and (
                memo.get("archives_for") == marker
                or _knows(memo["archives"], marker)
            ):
                v2["archives"] = memo["archives"]
            else:
                v2["archives"] = [
                    a.model_dump(mode="json") for a in self._scan_archives(key)
                ]
                memo["archives"] = v2["archives"]
                memo["archives_for"] = marker
        return [ArchiveInfo.model_validate(a) for a in v2["archives"]]

    def _count(
        self, key: ThreadKey, v2: JsonObject, meta: JsonObject, listing: bool
    ) -> int:
        counted = meta.get("messages_count")
        unread = listing and _HISTORY not in v2.get("stats", {})
        if unread and isinstance(counted, int) and counted >= 0:
            return counted
        return self._stats(key, v2, _HISTORY)["count"]

    def _listed_high(self, key: ThreadKey, v2: JsonObject, stream: str) -> int:
        if stream in v2.get("stats", {}):
            return self._high(key, v2, stream)
        last = _last_seq(self._read_path(key, stream), stream)
        return max(int(v2["high"].get(stream, 0)), last)

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
            infos.append(
                ArchiveInfo(
                    id=suffix,
                    created_at=_archive_created(suffix, data),
                    messages_count=len(records),
                )
            )
        return infos

    # --- необязательное: ключи истории для старой сессии -------------------------

    def history_keys(self, key: ThreadKey) -> set[str]:
        """Ключи сообщений истории, включая ключи записей «как есть» до
        синтеза id у старых записей — их сверяет дедупликация сессии."""
        with self._read_scope(key):
            _records, keys, _max = self._history(key)
        return keys

    # --- чтение --------------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        with self._read_scope(key):
            _control_doc, v2 = self._load(key)
            if not self._exists(key, v2):
                return None
            return self._record(key, v2)

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        records = []
        for key in self._known_keys():
            try:
                record = self._listed(key)
            except (OSError, ValueError):
                # Один испорченный тред не прячет остальные: список его
                # пропускает, чтение по ключу падает громко.
                logger.warning("тред %s не читается", key, exc_info=True)
                continue
            if record is not None:
                records.append(record)
        return page_of(records, query)

    def _listed(self, key: ThreadKey) -> ThreadRecord | None:
        with self._read_scope(key):
            _control_doc, v2 = self._load(key)
            if not self._exists(key, v2):
                return None
            return self._record(key, v2, listing=True)

    def _known_keys(self) -> set[ThreadKey]:
        keys: set[ThreadKey] = set()
        root = Path(self._dir)
        for path in root.glob("meta_*.json") if root.is_dir() else ():
            if layout.ARCHIVE_MARK in path.name:
                continue
            try:
                meta = layout.read_meta_file(str(path))
            except (OSError, ValueError):
                continue  # тред найдётся по identity, если он v2
            key = _meta_identity(path, meta)
            if key is not None:
                keys.add(key)
        for identity in root.glob(".store/*/identity.json"):
            try:
                raw = json.loads(identity.read_text(encoding="utf-8"))
                keys.add(
                    ThreadKey(system=raw["system_id"], thread=raw["thread_id"])
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue  # один испорченный служебный файл — не весь список
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
        with self._read_scope(key):
            _control_doc, v2 = self._load(key)
            if v2.get("deleted"):
                return []
            _stats, fresh = self._scan(key, v2, stream, after_seq=after_seq)
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
            # Владельца ``append_side`` не несёт: статус хода и трассу
            # субагентов на треде durable-исполнителя пишет сам исполнитель
            # (Temporal) — как и контракт, запись не проверяется.
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
        if (
            change.set_runtime is not None
            or change.set_frontend is not None
            or change.patch_frontend is not None
        ):
            meta = self._meta(key)
            parts = parts_from_meta(meta)
            if change.set_runtime is not None:
                parts = parts.with_(runtime=change.set_runtime)
            if change.set_frontend is not None:
                name, state = change.set_frontend
                parts = parts.with_(frontend={**parts.frontend, name: state})
            if change.patch_frontend is not None:
                name, patch = change.patch_frontend
                merged = patched_frontend(parts.frontend.get(name), patch)
                parts = parts.with_(frontend={**parts.frontend, name: merged})
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
        self._forget(key)
        # Проверка без записи: удаление несуществующего треда не заводит ему
        # служебный каталог.
        with self._read_scope(key):
            _control, v2 = self._load(key)
            exists = self._exists(key, v2)
        if exists:
            self._delete_thread(key, op_id)
        self._drop_leftovers(key)

    def _delete_thread(self, key: ThreadKey, op_id: str) -> None:
        with self._scope(key):
            base = self._base(key)
            control, v2 = self._load(key)
            if not self._exists(key, v2):
                return
            # Тред, который ведёт durable-исполнитель, не удаляет никто
            # другой (уборщик по TTL, фронтенд) — как в develop.
            _check_durable_owner(control, None)
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

    def _drop_leftovers(self, key: ThreadKey) -> None:
        """Картинки и лок журнала правок — по возможности, после удаления
        треда: их держат открытыми другие процессы (просмотрщик, второе окно
        TUI), и на Windows такой файл не удаляется. Тред удалён и без них;
        картинки — тем же путём, что в develop (отказ пишется в лог)."""
        from gc_core.attachments.store import ImageStore

        ImageStore.for_threads_dir(self._dir).delete_thread_images(
            key.system, key.thread
        )
        journal = layout.edits_journal_path(self._dir, key.system, key.thread)
        with contextlib.suppress(OSError):
            os.unlink(f"{journal}.lock")

    def sweep_orphans(
        self, cutoff: datetime, *, dry_run: bool = False
    ) -> tuple[int, int]:
        """Обслуживание (необязательная способность файлового хранилища):
        удалить устаревшие файлы, которые не являются живым тредом, — архивы
        сжатия (у них своё время жизни) и meta старых форматов
        (``meta_<tid>.json``, meta без идентификатора треда). Вернуть, сколько
        удалено meta и data. Треды целиком удаляет ``delete``."""
        removed_meta = removed_data = 0
        root = Path(self._dir)
        for meta_path in (
            sorted(root.glob("meta_*.json")) if root.is_dir() else ()
        ):
            meta = _meta_or_none(meta_path)
            if meta is None or self._is_canonical(meta_path, meta):
                continue
            if not _older_than(meta, cutoff):
                continue
            data_path = meta_path.with_name(
                "data_" + meta_path.name[len("meta_") : -len(_JSON)] + ".jsonl"
            )
            removed_meta += 1
            removed_data += int(data_path.exists())
            if not dry_run:
                meta_path.unlink(missing_ok=True)
                data_path.unlink(missing_ok=True)
                self._drop_archive_cache(meta_path.name, meta)
        return removed_meta, removed_data

    def _is_canonical(self, meta_path: Path, meta: JsonObject) -> bool:
        key = _meta_identity(meta_path, meta)
        if key is None:
            return False
        _data, canonical = layout.thread_paths(
            self._dir, key.system, key.thread
        )
        return Path(canonical) == meta_path

    def _drop_archive_cache(self, name: str, meta: JsonObject) -> None:
        """Удалённый архив уходит и из кеша архивов его треда."""
        system, thread = meta.get("system"), meta.get("thread_id")
        if layout.ARCHIVE_MARK not in name or not (
            isinstance(system, str) and isinstance(thread, str)
        ):
            return
        suffix = name.split(layout.ARCHIVE_MARK, 1)[1][: -len(_JSON)]
        key = ThreadKey(system=system, thread=thread)
        self._forget(key)
        if not self._base(key).is_dir():
            return  # кеша архивов на диске нет — помнить нечего
        with self._scope(key):
            control, v2 = self._load(key)
            if "archives" in v2:
                v2["archives"] = [
                    a for a in v2["archives"] if a["id"] != suffix
                ]
                self._save_control(key, control, v2)

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
        # Архивы журналов старых версий: суммаризация их больше не заводит.
        files.extend(root.glob(f"touched_{stem}__pre_summary__*.json"))
        files.extend(root.glob(f"edits_{stem}__pre_summary__*.jsonl"))
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
            try:
                doc = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue  # убрана параллельно (delete, другой уборщик)
            if (
                doc.get("v2")
                and doc.get("at")
                and (datetime.fromisoformat(doc["at"]) < cutoff)
            ):
                path.unlink(missing_ok=True)
                removed += 1
        return removed


#: С какого окна с конца файла список ищет последнюю запись (растёт ×4).
_TAIL_PROBE = 8 * 1024


def _last_seq(path: str | Path, stream: str) -> int:
    """Номер последней целой записи потока по концу файла — без чтения
    файла целиком; журнал правок нумеруется по строкам — для него 0."""
    if stream == _EDITS or not os.path.exists(path):
        return 0
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        probe = _TAIL_PROBE
        while True:
            handle.seek(max(0, size - probe))
            seq = _last_seq_in(handle.read())
            if seq or probe >= size:
                return seq
            probe *= 4


def _last_seq_in(tail: bytes) -> int:
    for raw in reversed(tail.splitlines()):
        row = _json_row(raw.decode("utf-8", errors="replace"))
        seq = None if row is None else row.get("seq")
        if isinstance(seq, int) and seq >= 1:
            return seq
    return 0


def _knows(archives: list | None, marker: object) -> bool:
    """Кеш архивов есть и знает архив последнего сжатия."""
    if archives is None:
        return False
    return not isinstance(marker, str) or any(
        a.get("id") == marker for a in archives
    )


def _archive_created(suffix: str, data_path: str) -> str:
    """Время архива сжатия — из суффикса имени; суффикс не из часов
    хранилища (старый формат, переименован руками) — время файла."""
    try:
        stamp = datetime.strptime(suffix[:15], _SUFFIX_FORMAT)
        return stamp.replace(tzinfo=UTC).isoformat()
    except ValueError:
        moment = (
            os.path.getmtime(data_path) if os.path.exists(data_path) else 0
        )
        return datetime.fromtimestamp(moment, UTC).isoformat()


def _meta_identity(path: Path, meta: JsonObject) -> ThreadKey | None:
    """Тред, чья это meta. ``system`` пишет ядро; meta, которую develop-TUI
    создавал до первого хода, несёт только ``system_id``; без обоих система —
    из имени ``meta_<system>_<thread>.json``."""
    thread = meta.get("thread_id")
    if not isinstance(thread, str) or not thread:
        return None
    system = meta.get("system") or meta.get("system_id")
    suffix = f"_{thread}.json"
    if (
        not system
        and path.name.startswith("meta_")
        and path.name.endswith(suffix)
    ):
        system = path.name[len("meta_") : -len(suffix)]
    if not isinstance(system, str) or not system:
        return None
    try:
        return ThreadKey(system=system, thread=thread)
    except ValueError:
        return None


def _meta_or_none(path: Path) -> JsonObject | None:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _older_than(meta: JsonObject, cutoff: datetime) -> bool:
    stamp = meta.get("updated_at") or meta.get("created_at")
    if not isinstance(stamp, str):
        return False
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=cutoff.tzinfo)
    return moment < cutoff


def _check_durable_owner(control: JsonObject, owner: str | None) -> None:
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
        archives = store._archives(key, self.v2, store._meta(key))
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
        lines = [
            layout.history_line(s, t, seq, ts, payload).encode()
            for seq, ts, payload in self.fresh
        ]
        tail = b"".join(lines)
        if not self.rewrite:
            self.store._after_append(
                self.v2,
                self.key,
                _HISTORY,
                lines,
                [seq for seq, _ts, _payload in self.fresh],
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
            "recent": [],
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
