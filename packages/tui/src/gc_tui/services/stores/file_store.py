"""FileTaskStore — хранилище трейсов TUI поверх thread_store.

Маппинг курсора (tui3_impl §6.4):
  position_token  → byte offset в data_*.jsonl
  ordering_key    → max seq из data_*
  storage_epoch   → meta.last_summary_archive_suffix (fallback last_summarized_at)
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    is_dialog_summary_user_message_text,
    user_content_to_text,
)
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_core.persistence.turn_outcome import (
    TurnOutcome,
    is_turn_outcome_record,
    parse_turn_outcome,
)
from gc_core.thread_store import (
    delete_thread,
    discard_message_by_id,
    read_subagent_trace,
)

from gc_tui.models import (
    CompactionStep,
    TailResult,
    TaskCursor,
    TaskSummary,
)
from gc_tui.services.trace_mapper import _clean_summary
from gc_tui.services.turn_outcome import kind_of, meta_status_of

_ARCHIVE_MARK = "__pre_summary__"
# Сколько тиков подряд ждать завершения шагов сжатия ядра: 8 тиков опроса
# TUI (0,25 с, TraceScreen) ≈ 2 с.
_COMPACTION_WAIT_TICKS = 8
# Сколько раз читать живой файл, пока эпоха и архивы не совпадут до и после
# чтения. Сжатие меняет их не больше трёх раз (новый архив, мета в архив,
# новая мета), каждое изменение рушит не больше одной попытки — четвёртая
# согласована. Следующее сжатие требует вызова модели и в чтение не попадёт.
_SNAPSHOT_ATTEMPTS = 4
# Сколько байт с конца data_*.jsonl читать в поисках записи итога
# последнего хода для списка сессий. Запись итога — последняя запись
# хода; дальше в файле может быть лишь сжатие или начало нового хода.
_OUTCOME_TAIL_BYTES = 64 * 1024
# Статусы меты, которые держит живой (или убитый посреди хода) ход: у
# него записи итога нет, решает мета.
_LIVE_STATUSES = frozenset({"running", "asking"})


def _last_turn_outcome(data_path: Path) -> Optional[TurnOutcome]:
    """Итог последнего хода по хвосту файла треда; None — после последней
    записи итога начался ход без записи, записи нет или она не в хвосте."""
    try:
        with data_path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _OUTCOME_TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if is_turn_outcome_record(rec):
            return parse_turn_outcome(rec)
        if extract_real_user_message_text(rec.get("payload")):
            return None
    return None


class FileTaskStore:
    """Хранилище трейсов поверх directory с data_*.jsonl/meta_*.json."""

    def __init__(self, threads_dir: str, system: str = "cli") -> None:
        self._dir = Path(threads_dir)
        self._system = system

    # ---- listing ------------------------------------------------------

    def list_tasks(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[TaskSummary]:
        items: list[TaskSummary] = []
        prefix = f"meta_{self._system}_"
        if not self._dir.exists():
            return []
        for meta_path in self._dir.glob(f"{prefix}*.json"):
            # Snapshot-файлы saved до summary-проходов (см.
            # thread_store.archive_and_compact_summary_payload) — это дубликаты
            # live-meta с тем же thread_id, в picker /sessions не должны попадать.
            if "__pre_summary__" in meta_path.name:
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tid = meta.get("thread_id") or meta_path.stem[len(prefix) :]
            # Статус — только из меты: хвост треда читает строка списка
            # (turn_status), иначе список читал бы все треды проекта.
            status = (meta.get("tui") or {}).get("status") or meta.get(
                "status"
            )
            updated = meta.get("updated_at", "")
            msgs = int(meta.get("messages_count", 0))
            tui_meta = meta.get("tui") or {}
            skill_id = tui_meta.get("skill_id")
            unread = int(meta.get("unread_count", 0))
            last_err = meta.get("last_error")
            token_usage = meta.get("token_usage")
            if not isinstance(token_usage, dict):
                token_usage = None
            items.append(
                TaskSummary(
                    thread_id=tid,
                    status=status,
                    updated_at=updated,
                    messages_count=msgs,
                    skill_id=skill_id,
                    unread_count=unread,
                    last_error=last_err,
                    tui_meta=tui_meta,
                    token_usage=token_usage,
                )
            )
        items.sort(key=lambda s: s.updated_at, reverse=True)
        return items[offset : offset + limit]

    def turn_status(
        self, task_id: str, meta_status: Optional[str]
    ) -> Optional[str]:
        """Исход сессии для строки списка. Источник — запись итога
        последнего хода (её пишет ядро в любом фронтенде); meta_status
        (статус из list_tasks) — для живого хода и тредов без записей:
        TUI-владелец пишет в meta.tui.status (completed/stopped/failed),
        верхний meta.status — CLI/server. Нет ничего → None (список не
        выдумывает «stopped»). Читает хвост data-файла треда."""
        if meta_status in _LIVE_STATUSES:
            return meta_status
        outcome = _last_turn_outcome(self._data_path(task_id, self._system))
        if outcome is None:
            return meta_status
        return meta_status_of(kind_of(outcome)[0])

    # ---- per-task -----------------------------------------------------

    def get_task_meta(
        self, task_id: str, system: Optional[str] = None
    ) -> dict:
        sys_ = system or self._system
        meta_path = self._dir / f"meta_{sys_}_{task_id}.json"
        if not meta_path.exists():
            return {}
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def meta_mtime(
        self, task_id: str, system: Optional[str] = None
    ) -> Optional[int]:
        """Метка изменения meta треда (нс) — ключ кэшей, читающих meta.

        None — файла нет."""
        try:
            return (
                self._meta_path(task_id, system or self._system)
                .stat()
                .st_mtime_ns
            )
        except OSError:
            return None

    def update_tui_meta(
        self, task_id: str, patch: dict, system: Optional[str] = None
    ) -> None:
        """Объединяет patch с meta.tui sub-dict и сохраняет (TUI-owned).

        Запись — общим писателем меты ядра (атомарно, под замком треда):
        исполнитель пишет ту же мету параллельно. Время изменения треда
        запись фронтенда не сдвигает."""
        from gc_core.thread_store import update_tui_meta
        from gc_core.thread_store_journal import local_write_scope

        sys_ = system or self._system
        # Мета TUI пишется и при durable-владельце (как update_tui журнала),
        # но под его блокировкой: иначе гонка с коммитом журнала.
        with local_write_scope(
            str(self._dir), sys_, task_id, forbid_owned=False
        ):
            update_tui_meta(str(self._dir), sys_, task_id, patch, touch=False)

    def clear_plan(self, task_id: str, system: Optional[str] = None) -> bool:
        """Убрать план из сессии (`/plan clear`); True — план был. Решение и
        запись — в ядре, здесь только делегирование под блокировкой записи."""
        from gc_core.thread_store import clear_plan_meta
        from gc_core.thread_store_journal import local_write_scope

        sys_ = system or self._system
        with local_write_scope(
            str(self._dir), sys_, task_id, forbid_owned=False
        ):
            return clear_plan_meta(str(self._dir), sys_, task_id)

    def append_model_choice(
        self,
        task_id: str,
        record: ModelChoiceRecord,
        system: Optional[str] = None,
    ) -> bool:
        """Смена модели между ходами — записью треда за последним
        сообщением; True — записана. Решение и запись — в ядре, здесь
        делегирование под блокировкой записи. Тред durable-владельца и тред
        без сообщений записи не получают."""
        from gc_core.thread_store import (
            append_model_choice_after_last_message,
        )
        from gc_core.thread_store_journal import (
            ThreadStoreOwnershipError,
            local_write_scope,
        )

        sys_ = system or self._system
        try:
            with local_write_scope(str(self._dir), sys_, task_id):
                return append_model_choice_after_last_message(
                    str(self._dir), sys_, task_id, record
                )
        except ThreadStoreOwnershipError:
            return False

    def delete_task(self, task_id: str, system: Optional[str] = None) -> None:
        """Полностью удалить тред (все формы файлов). Делегирует в thread_store."""
        sys_ = system or self._system
        delete_thread(str(self._dir), sys_, task_id)

    def discard_message_by_id(
        self,
        task_id: str,
        message_id: str,
        system: Optional[str] = None,
        *,
        tags: Sequence[str] = (),
        strip_think: bool = True,
        cursor: Optional[TaskCursor] = None,
    ) -> tuple[bool, Optional[TaskCursor]]:
        """Удалить одно сообщение из треда по id (откат осиротевшей реплики).

        Возвращает (удалено ли, курсор). Курсор хвоста этого треда
        переносится в переписанный файл: за ним остаётся ровно то, что не
        было прочитано, кроме снятых записей."""
        sys_ = system or self._system
        offset = int(cursor.position_token or 0) if cursor else 0
        read = self._read_live(task_id, sys_)[:offset]
        removed = discard_message_by_id(
            str(self._dir),
            sys_,
            task_id,
            message_id,
            tags=tags,
            strip_think=strip_think,
        )
        if cursor is None or not removed:
            return removed, cursor
        live = self._read_live(task_id, sys_)
        end = _rebased_offset(read, live)
        return removed, replace(
            cursor,
            position_token=end,
            ordering_key=_max_seq(self._parse_chunk(live[:end])),
        )

    def read_subagent_trace(
        self, task_id: str, system: Optional[str] = None
    ) -> list[dict]:
        """Sidecar nested-трейс: data_subagents_{system}_{task_id}.jsonl."""
        sys_ = system or self._system
        return read_subagent_trace(str(self._dir), sys_, task_id)

    # ---- read & tail --------------------------------------------------

    def _data_path(self, task_id: str, system: str) -> Path:
        return self._dir / f"data_{system}_{task_id}.jsonl"

    def _meta_path(self, task_id: str, system: str) -> Path:
        return self._dir / f"meta_{system}_{task_id}.json"

    def _read_epoch(self, task_id: str, system: str) -> Optional[str]:
        meta = self.get_task_meta(task_id, system=system)
        if "last_summary_archive_suffix" in meta:
            return str(meta["last_summary_archive_suffix"])
        if "last_summarized_at" in meta:
            return str(meta["last_summarized_at"])
        return None

    @staticmethod
    def _parse_line(raw: str) -> Optional[dict]:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(rec, dict):
            return None
        return rec

    def read_all_events(
        self, task_id: str, system: Optional[str] = None
    ) -> tuple[list[dict], TaskCursor]:
        """Записи живого файла и курсор после последней целой строки.

        Эпоха и число архивов сверяются до и после чтения: сжатие посреди
        чтения дало бы курсор из старого файла и новой эпохи, и следующий
        тик сбросил бы трейс. Расхождение — перечитать."""
        sys_ = system or self._system
        state = self._storage_state(task_id, sys_)
        for _ in range(_SNAPSHOT_ATTEMPTS):
            raw = self._read_live(task_id, sys_)
            settled = self._storage_state(task_id, sys_)
            if settled == state:
                break
            state = settled
        end = raw.rfind(b"\n") + 1
        records = self._parse_chunk(raw[:end])
        epoch, archives = state
        cursor = TaskCursor(
            position_token=end,
            ordering_key=_max_seq(records),
            storage_epoch=epoch,
            archives=archives,
        )
        return records, cursor

    def _storage_state(
        self, task_id: str, system: str
    ) -> tuple[Optional[str], int]:
        """(эпоха, число архивов) — то, что меняет сжатие."""
        return (
            self._read_epoch(task_id, system),
            len(self._archive_paths(task_id, system)),
        )

    def _read_live(self, task_id: str, system: str) -> bytes:
        try:
            return self._data_path(task_id, system).read_bytes()
        except FileNotFoundError:
            return b""

    def _archive_paths(self, task_id: str, system: str) -> list[Path]:
        """Data-архивы сжатия треда, по имени (= по времени)."""
        return sorted(
            self._dir.glob(f"data_{system}_{task_id}{_ARCHIVE_MARK}*.jsonl"),
            key=lambda p: p.name,
        )

    @staticmethod
    def _compacted_by(seg: list[dict], leading: dict) -> dict:
        """Сводка сжатия сегмента seg; leading — dialog_summary следующего
        файла.

        Счёт и объём — только по сообщениям: записи итога хода и смены
        модели сообщениями не являются (сообщение — строка с `payload`)."""
        payload = leading.get("payload") or {}
        synthesis = _clean_summary(
            user_content_to_text(payload.get("content"))
        )
        messages = [r for r in seg if isinstance(r.get("payload"), dict)]
        # Мера триггера ядра: аргументы вызовов и вывод тулов тоже объём.
        chars_before = calc_history_chars_for_trigger(
            [r.get("payload") or {} for r in messages]
        )
        return {
            "ts": leading.get("ts", ""),
            "messages_in_segment": len(messages),
            "synthesis": synthesis,
            "chars_before": chars_before,
            "chars_after": len(synthesis),
        }

    @staticmethod
    def is_compaction_summary(rec: dict) -> bool:
        """Запись — синтетический dialog_summary (лидирующая строка после компакции).

        Публичный предикат: TUI определяет «была ли компакция» по голове live-файла
        (после turn_end-сжатия первая запись = последняя сводка) без доп. I/O.
        """
        payload = rec.get("payload")
        if not isinstance(payload, dict) or payload.get("role") != "user":
            return False
        return is_dialog_summary_user_message_text(
            user_content_to_text(payload.get("content"))
        )

    def read_raw_thread_jsonl(
        self, task_id: str, system: Optional[str] = None
    ) -> str:
        """Дословная конкатенация всех файлов треда (архивы __pre_summary__ +
        live) в порядке имён — сырьё для /export log-json. В отличие от
        read_segments НЕ отбрасывает лидирующие dialog_summary-строки архивов:
        цель — точная копия того, что на диске."""
        sys_ = system or self._system
        archives = self._archive_paths(task_id, sys_)
        files = list(archives)
        live = self._data_path(task_id, sys_)
        if live.exists():
            files.append(live)
        return "".join(f.read_text(encoding="utf-8") for f in files)

    def read_segments(
        self, task_id: str, system: Optional[str] = None
    ) -> list[dict]:
        """Тред → сегменты (части между сжатиями). Read-only реконструкция.

        Каждый сегмент: {"index", "messages": [envelope-records], "compacted_by":
        {"ts", "messages_in_segment", "synthesis", "chars_before", "chars_after"} | None}.
        Сегмент i = файл i минус лидирующая dialog_summary; его synthesis =
        лидирующая dialog_summary СЛЕДУЮЩЕГО файла; у последнего (live) — None.
        """
        sys_ = system or self._system
        archives = self._archive_paths(task_id, sys_)
        files = list(archives)
        live = self._data_path(task_id, sys_)
        if live.exists():
            files.append(live)

        # parsed[i] = (seg_records, leading_summary_record_or_None)  # noqa: ERA001  # описание формата, не выключенный код
        parsed: list[tuple[list[dict], Optional[dict]]] = []
        for fpath in files:
            recs: list[dict] = []
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    rec = self._parse_line(line.rstrip("\n"))
                    if rec is not None:
                        recs.append(rec)
            leading, seg = self._split_leading(recs)
            parsed.append((seg, leading))

        segments: list[dict] = []
        for i, (seg, _leading) in enumerate(parsed):
            compacted_by = None
            if i + 1 < len(parsed):
                next_leading = parsed[i + 1][1]
                if next_leading is not None:
                    compacted_by = self._compacted_by(seg, next_leading)
            segments.append(
                {"index": i, "messages": seg, "compacted_by": compacted_by}
            )
        return segments

    def tail_events(
        self, task_id: str, cursor: TaskCursor, system: Optional[str] = None
    ) -> TailResult:
        sys_ = system or self._system
        data_path = self._data_path(task_id, sys_)

        # 1. epoch check
        current_epoch = self._read_epoch(task_id, sys_)
        if cursor.storage_epoch != current_epoch:
            return self._reset_unless_compaction(
                task_id, sys_, cursor, current_epoch, "storage_epoch changed"
            )

        if not data_path.exists():
            return TailResult(
                new_records=[], next_cursor=replace(cursor, wait_ticks=0)
            )

        # 2. file-truncation safeguard
        size = data_path.stat().st_size
        if size < int(cursor.position_token or 0):
            return self._reset_unless_compaction(
                task_id, sys_, cursor, current_epoch, "file truncated"
            )

        # 3. incremental read
        read = self._read_after(data_path, cursor)
        if read is None:
            return self._reset_unless_compaction(
                task_id, sys_, cursor, current_epoch, "not a continuation"
            )
        new_records, new_offset, max_seq = read
        return TailResult(
            new_records=new_records,
            next_cursor=replace(
                cursor,
                position_token=new_offset,
                ordering_key=max_seq,
                storage_epoch=current_epoch,
                wait_ticks=0,
            ),
        )

    def _read_after(
        self, data_path: Path, cursor: TaskCursor
    ) -> Optional[tuple[list[dict], int, int]]:
        """Записи после курсора, новое смещение и макс. seq; None — файл не
        продолжение прочитанного: курсор не на границе строки (файл подменён
        сжатием) или seq откатился. Недописанная последняя строка остаётся
        за курсором и придёт следующим тиком."""
        new_records: list[dict] = []
        max_seq: int = (
            int(cursor.ordering_key)
            if isinstance(cursor.ordering_key, int)
            else 0
        )
        offset = int(cursor.position_token or 0)
        with open(data_path, "rb") as f:
            # В JSONL перевод строки — только конец записи.
            f.seek(max(offset - 1, 0))
            if offset and f.read(1) != b"\n":
                return None
            raw = f.read()
        end = raw.rfind(b"\n") + 1
        for rec in self._parse_chunk(raw[:end]):
            seq = rec.get("seq")
            if isinstance(seq, int):
                if seq < max_seq:
                    return None
                if seq > max_seq:
                    max_seq = seq
            new_records.append(rec)
        return new_records, offset + end, max_seq

    def _reset_unless_compaction(
        self,
        task_id: str,
        sys_: str,
        cursor: TaskCursor,
        current_epoch: Optional[str],
        reason: str,
    ) -> TailResult:
        classified = self._classify_compaction(
            task_id, sys_, cursor, current_epoch
        )
        if classified is not None:
            return classified
        return TailResult(
            new_records=[],
            next_cursor=TaskCursor(0, 0, current_epoch),
            stream_reset=True,
            reason=reason,
        )

    def _classify_compaction(
        self,
        task_id: str,
        sys_: str,
        cursor: TaskCursor,
        current_epoch: Optional[str],
    ) -> Optional[TailResult]:
        """Признак сброса — это сжатие (шаг или ждать)? None — нет, сброс.

        Сжатие опознаётся по ровно одному новому архиву (спека 3.1): его
        эпоха уже в мете — шаг сжатия; ещё нет — ядро между шагами 1–4,
        ждать не дольше _COMPACTION_WAIT_TICKS тиков.
        """
        archives = self._archive_paths(task_id, sys_)
        if len(archives) != cursor.archives + 1:
            return None
        suffix = archives[-1].stem.rpartition(_ARCHIVE_MARK)[2]
        if current_epoch == suffix:
            return self._compaction_result(
                task_id, sys_, cursor, archives, suffix
            )
        if cursor.wait_ticks >= _COMPACTION_WAIT_TICKS:
            return None
        return TailResult(
            new_records=[],
            next_cursor=replace(cursor, wait_ticks=cursor.wait_ticks + 1),
        )

    def _compaction_result(
        self,
        task_id: str,
        sys_: str,
        cursor: TaskCursor,
        archives: list[Path],
        epoch: str,
    ) -> Optional[TailResult]:
        """Шаг сжатия: хвост архива от курсора и новый живой файл.

        None — курсор не на границе записи архива или файлы не прочитать.
        """
        try:
            archive_raw = archives[-1].read_bytes()
            live_raw = self._data_path(task_id, sys_).read_bytes()
        except OSError:
            return None
        if not self._on_record_boundary(archive_raw, cursor):
            return None
        offset = int(cursor.position_token or 0)
        tail = self._parse_chunk(archive_raw[offset:])
        archived = self._parse_chunk(archive_raw[:offset]) + tail
        archive_leading, archive_seg = self._split_leading(archived)
        if offset == 0 and archive_leading is not None:
            tail = tail[1:]
        live_end = live_raw.rfind(b"\n") + 1
        live_leading, live_records = self._split_leading(
            self._parse_chunk(live_raw[:live_end])
        )
        if live_leading is None:
            return None
        step = CompactionStep(
            archive_tail=tail,
            segment_index=len(archives) - 1,
            compacted_by=self._compacted_by(archive_seg, live_leading),
            window_ids=_payload_ids([*archived, *live_records]),
            live_records=live_records,
        )
        next_cursor = TaskCursor(
            position_token=live_end,
            ordering_key=_max_seq([live_leading, *live_records]),
            storage_epoch=epoch,
            archives=len(archives),
        )
        return TailResult(
            new_records=[], next_cursor=next_cursor, compaction=step
        )

    def _on_record_boundary(self, raw: bytes, cursor: TaskCursor) -> bool:
        """В raw ровно на курсоре кончается запись с seq курсора."""
        offset = int(cursor.position_token or 0)
        if offset == 0:
            return cursor.ordering_key == 0
        if offset > len(raw) or raw[offset - 1 : offset] != b"\n":
            return False
        last_line = raw[: offset - 1].rpartition(b"\n")[2]
        last = self._parse_line(last_line.decode("utf-8", errors="replace"))
        return last is not None and last.get("seq") == cursor.ordering_key

    @classmethod
    def _parse_chunk(cls, raw: bytes) -> list[dict]:
        text = raw.decode("utf-8", errors="replace")
        return [
            rec
            for line in text.split("\n")
            if (rec := cls._parse_line(line)) is not None
        ]

    @classmethod
    def _split_leading(
        cls, recs: list[dict]
    ) -> tuple[Optional[dict], list[dict]]:
        """(лидирующая dialog_summary или None, записи без неё)."""
        if recs and cls.is_compaction_summary(recs[0]):
            return recs[0], recs[1:]
        return None, recs


def _payload_ids(records: list[dict]) -> frozenset[str]:
    ids: set[str] = set()
    for rec in records:
        payload = rec.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            ids.add(payload["id"])
    return frozenset(ids)


def _max_seq(records: list[dict]) -> int:
    seqs = [r.get("seq") for r in records]
    return max((s for s in seqs if isinstance(s, int)), default=0)


def _rebased_offset(read: bytes, live: bytes) -> int:
    """Смещение в переписанном файле, до которого лежит прочитанное.

    Снятие записей только выбрасывает строки, порядок прочих тот же, а
    судьба строки задана её содержимым: строки прочитанной части, что
    стоят в новом файле подряд с начала, — пережившие снятие."""
    end = 0
    for line in read.splitlines(keepends=True):
        if live.startswith(line, end):
            end += len(line)
    return end
