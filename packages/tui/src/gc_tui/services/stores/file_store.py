"""FileTaskStore — первая реализация TaskStore поверх thread_store.

Маппинг курсора (tui3_impl §6.4):
  position_token  → byte offset в data_*.jsonl
  ordering_key    → max seq из data_*
  storage_epoch   → meta.last_summary_archive_suffix (fallback last_summarized_at)
"""

from __future__ import annotations

import inspect
import json
from functools import wraps
from pathlib import Path
from typing import Optional, Sequence

from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
    user_content_to_text,
)
from gc_core.thread_store import (
    delete_thread,
    discard_message_by_id,
    read_subagent_trace,
    update_tui_meta,
)
from gc_core.thread_store_journal import thread_read_scope

from gc_tui.models import TailResult, TaskCursor, TaskSummary
from gc_tui.services.trace_mapper import _clean_summary


def _coherent_thread_read(method):
    """Keep epoch, file bytes and cursor in one recovered store snapshot."""
    signature = inspect.signature(method)

    @wraps(method)
    def read(self, task_id, *args, **kwargs):
        arguments = signature.bind(self, task_id, *args, **kwargs).arguments
        system = arguments.get("system") or self._system
        with thread_read_scope(str(self._dir), system, task_id):
            return method(self, task_id, *args, **kwargs)

    return read


class FileTaskStore:
    """TaskStore поверх directory с data_*.jsonl/meta_*.json."""

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
            candidate_id = meta_path.stem[len(prefix) :]
            meta = self.get_task_meta(candidate_id)
            if not meta:
                continue
            tid = meta.get("thread_id") or meta_path.stem[len(prefix) :]
            # Исход сессии: TUI-владелец пишет в meta.tui.status (completed/stopped/
            # failed); верхний meta.status — для CLI/server-потоков. Нет ни того, ни
            # другого → None (исход не записан; список не выдумывает «stopped»).
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

    # ---- per-task -----------------------------------------------------

    @_coherent_thread_read
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

    def update_tui_meta(
        self, task_id: str, patch: dict, system: Optional[str] = None
    ) -> None:
        """Объединяет patch с meta.tui sub-dict и сохраняет (TUI-owned)."""
        sys_ = system or self._system
        update_tui_meta(str(self._dir), sys_, task_id, patch)

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
    ) -> bool:
        """Удалить одно сообщение из треда по id (откат осиротевшей реплики)."""
        sys_ = system or self._system
        return discard_message_by_id(
            str(self._dir),
            sys_,
            task_id,
            message_id,
            tags=tags,
            strip_think=strip_think,
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

    @_coherent_thread_read
    def read_all_events(
        self, task_id: str, system: Optional[str] = None
    ) -> tuple[list[dict], TaskCursor]:
        sys_ = system or self._system
        data_path = self._data_path(task_id, sys_)
        records: list[dict] = []
        max_seq: int = 0
        offset: int = 0
        if data_path.exists():
            with open(data_path, encoding="utf-8") as f:
                for line in f:
                    rec = self._parse_line(line.rstrip("\n"))
                    if rec is None:
                        continue
                    records.append(rec)
                    seq = rec.get("seq")
                    if isinstance(seq, int) and seq > max_seq:
                        max_seq = seq
            offset = data_path.stat().st_size
        cursor = TaskCursor(
            position_token=offset,
            ordering_key=max_seq,
            storage_epoch=self._read_epoch(task_id, sys_),
        )
        return records, cursor

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

    @_coherent_thread_read
    def read_raw_thread_jsonl(
        self, task_id: str, system: Optional[str] = None
    ) -> str:
        """Дословная конкатенация всех файлов треда (архивы __pre_summary__ +
        live) в порядке имён — сырьё для /export log-json. В отличие от
        read_segments НЕ отбрасывает лидирующие dialog_summary-строки архивов:
        цель — точная копия того, что на диске."""
        sys_ = system or self._system
        archives = sorted(
            self._dir.glob(f"data_{sys_}_{task_id}__pre_summary__*.jsonl"),
            key=lambda p: p.name,
        )
        files = list(archives)
        live = self._data_path(task_id, sys_)
        if live.exists():
            files.append(live)
        return "".join(f.read_text(encoding="utf-8") for f in files)

    @_coherent_thread_read
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
        archives = sorted(
            self._dir.glob(f"data_{sys_}_{task_id}__pre_summary__*.jsonl"),
            key=lambda p: p.name,
        )
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
            leading = (
                recs[0]
                if (recs and self.is_compaction_summary(recs[0]))
                else None
            )
            seg = recs[1:] if leading is not None else recs
            parsed.append((seg, leading))

        segments: list[dict] = []
        for i, (seg, _leading) in enumerate(parsed):
            compacted_by = None
            if i + 1 < len(parsed):
                next_leading = parsed[i + 1][1]
                if next_leading is not None:
                    payload = next_leading.get("payload") or {}
                    synthesis = _clean_summary(
                        user_content_to_text(payload.get("content"))
                    )
                    chars_before = sum(
                        len(
                            user_content_to_text(
                                (r.get("payload") or {}).get("content")
                            )
                        )
                        for r in seg
                    )
                    compacted_by = {
                        "ts": next_leading.get("ts", ""),
                        "messages_in_segment": len(seg),
                        "synthesis": synthesis,
                        "chars_before": chars_before,
                        "chars_after": len(synthesis),
                    }
            segments.append(
                {"index": i, "messages": seg, "compacted_by": compacted_by}
            )
        return segments

    @_coherent_thread_read
    def read_live_messages(
        self, task_id: str, system: Optional[str] = None
    ) -> list[dict]:
        """payload-словари сообщений ТОЛЬКО из live data-файла (без архивов).
        Для лёгкого подсчёта объёма контекста (индикатор). Нет файла → []."""
        sys_ = system or self._system
        live = self._data_path(task_id, sys_)
        if not live.exists():
            return []
        out: list[dict] = []
        with open(live, encoding="utf-8") as f:
            for line in f:
                rec = self._parse_line(line.rstrip("\n"))
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if isinstance(payload, dict):
                    out.append(payload)
        return out

    @_coherent_thread_read
    def tail_events(
        self, task_id: str, cursor: TaskCursor, system: Optional[str] = None
    ) -> TailResult:
        sys_ = system or self._system
        data_path = self._data_path(task_id, sys_)

        # 1. epoch check
        current_epoch = self._read_epoch(task_id, sys_)
        if cursor.storage_epoch != current_epoch:
            return TailResult(
                new_records=[],
                next_cursor=TaskCursor(0, 0, current_epoch),
                stream_reset=True,
                reason="storage_epoch changed",
            )

        if not data_path.exists():
            return TailResult(new_records=[], next_cursor=cursor)

        # 2. file-truncation safeguard
        size = data_path.stat().st_size
        if size < int(cursor.position_token or 0):
            return TailResult(
                new_records=[],
                next_cursor=TaskCursor(0, 0, current_epoch),
                stream_reset=True,
                reason="file truncated",
            )

        # 3. incremental read
        new_records: list[dict] = []
        max_seq: int = (
            int(cursor.ordering_key)
            if isinstance(cursor.ordering_key, int)
            else 0
        )
        with open(data_path, encoding="utf-8") as f:
            f.seek(int(cursor.position_token or 0))
            for line in f:
                rec = self._parse_line(line.rstrip("\n"))
                if rec is None:
                    continue
                seq = rec.get("seq")
                if isinstance(seq, int):
                    if seq < max_seq:
                        return TailResult(
                            new_records=[],
                            next_cursor=TaskCursor(0, 0, current_epoch),
                            stream_reset=True,
                            reason="ordering rollback",
                        )
                    if seq > max_seq:
                        max_seq = seq
                new_records.append(rec)
            new_offset = f.tell()

        return TailResult(
            new_records=new_records,
            next_cursor=TaskCursor(
                position_token=new_offset,
                ordering_key=max_seq,
                storage_epoch=current_epoch,
            ),
        )
