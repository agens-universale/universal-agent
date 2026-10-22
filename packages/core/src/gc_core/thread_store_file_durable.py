"""``DurableFileThreadStore`` — файловый тред для исполнителя с повторами операций.

Формат файлов тот же, что у :class:`FileThreadStore`: ``data_*.jsonl`` и
``meta_*.json``. Сверху добавлена служебная папка ``.store/`` (см.
``thread_store_journal``): квитанции операций, журнал прерванной записи и
владелец треда. Это нужно там, где одна и та же запись может прийти дважды —
например, повтор activity после падения worker'а Temporal.

Локальный режим этот класс не использует: обычный ``FileThreadStore`` пишет
файлы напрямую, без блокировок, fsync и квитанций.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from gc_core.thread_store import reset_run_flags
from gc_core.thread_store_file import FileThreadStore


class DurableFileThreadStore(FileThreadStore):
    """Файловый тред с durable-контрактом ``read_snapshot``/``commit_operation``."""

    #: Файла плана у журнального стора нет: его файлы ходят транзакциями
    #: журнала, а экспорт для человека в них не участвует.
    save_plan_file = None

    #: Записи итога хода нет: мимо журнала нельзя — ``_recover_append``
    #: сверяет хвост файла с журналом побайтно, а своей операции журнала у
    #: записи нет.
    append_turn_outcome = None

    def _call(self, helper, *args, **kwargs):
        from gc_core.thread_store_journal import dispatch_file_helper

        return dispatch_file_helper(helper, args, kwargs)

    def read_snapshot(self, system_id, thread_id, *, include_state=True):
        from gc_core.thread_store_journal import read_snapshot

        return read_snapshot(
            self._dir, system_id, thread_id, include_state=include_state
        )

    def commit_operation(self, system_id, thread_id, operation):
        from gc_core.thread_store_journal import (
            commit_operation,
            purge_after_reset,
        )

        receipt = commit_operation(self._dir, system_id, thread_id, operation)
        if operation.kind == "reset":
            purge_after_reset(
                self._dir,
                system_id,
                thread_id,
                keep_operation=operation.operation_id,
            )
        return receipt

    def reset(self, system_id, thread_id):
        from gc_core.thread_store_journal import purge_after_reset

        deleted = super().reset(system_id, thread_id)
        purge_after_reset(self._dir, system_id, thread_id, keep_operation=None)
        return deleted

    def append_messages(
        self, system_id, thread_id, messages, *, known_keys=None, start_seq=0
    ):
        from gc_core.thread_store_journal import thread_read_scope

        # Ключи с диска и запись — под одной блокировкой треда.
        with thread_read_scope(self._dir, system_id, thread_id):
            return super().append_messages(
                system_id,
                thread_id,
                messages,
                known_keys=known_keys,
                start_seq=start_seq,
            )

    def replace_messages(self, system_id, thread_id, messages):
        from gc_core.thread_store_durable import ThreadStoreOperation
        from gc_core.thread_store_journal import thread_read_scope

        with thread_read_scope(self._dir, system_id, thread_id):
            snapshot = self.read_snapshot(
                system_id, thread_id, include_state=False
            )
            self.commit_operation(
                system_id,
                thread_id,
                ThreadStoreOperation(
                    uuid.uuid4().hex,
                    "replace_messages",
                    {"messages": list(messages)},
                    generation=snapshot.generation,
                ),
            )

    def reset_run_flags(
        self,
        system_id: str,
        thread_id: str,
        *,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._call(
            reset_run_flags,
            self._dir,
            system_id,
            thread_id,
            session_meta=session_meta,
        )


__all__ = ["DurableFileThreadStore"]
