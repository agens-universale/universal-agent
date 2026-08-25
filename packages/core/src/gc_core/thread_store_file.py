"""``FileThreadStore`` — реализация ``ThreadStore`` поверх ``data_*.jsonl``.

Это ровно нынешнее поведение, собранное в класс: методы делегируют в модульные
функции ``thread_store``, где логика и остаётся. Ничего не переписано — иначе
переезд на контракт превратился бы в переписывание формата хранения, а это
разные работы с разной ценой ошибки.

Класс НИЧЕГО НЕ НАСЛЕДУЕТ: ``ThreadStore`` — ``Protocol``, структурная
типизация. Совпал по сигнатурам — подходит. Так же будет и у чужой реализации
из базы: ей не придётся импортировать наш базовый класс.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    discard_message_by_id,
    ensure_threads_dir,
    load_thread_messages,
    load_thread_meta,
    patch_runtime_flag,
    read_subagent_trace,
    reset_thread_files,
    save_thread_meta,
    update_runtime_meta,
)


class FileThreadStore:
    """Хранилище треда в каталоге с ``data_*.jsonl`` и ``meta_*.json``."""

    def __init__(self, threads_dir: str) -> None:
        self._dir = threads_dir

    @property
    def threads_dir(self) -> str:
        """Каталог хранения. Нужен вызывающим, которые печатают путь в логи."""
        return self._dir

    def ensure_ready(self) -> None:
        ensure_threads_dir(self._dir)

    # --- чтение ------------------------------------------------------------

    def load_messages(self, system_id: str, thread_id: str) -> List[Dict[str, Any]]:
        # Модульная функция отдаёт тройку (сообщения, ключи, счётчик записанных).
        # Наружу контракт обещает только сообщения: ключи — деталь дедупликации,
        # и держать её должен сам store, а не каждый вызывающий.
        messages, _keys, _count = load_thread_messages(self._dir, system_id, thread_id)
        return messages or []

    def load_thread_state(self, system_id: str, thread_id: str):
        """``(сообщения, ключи, счётчик)`` — ровно то, что отдаёт файловый слой."""
        messages, keys, count = load_thread_messages(self._dir, system_id, thread_id)
        return (messages or []), (keys or set()), int(count or 0)

    def _known_keys(self, system_id: str, thread_id: str) -> set:
        _messages, keys, _count = load_thread_messages(self._dir, system_id, thread_id)
        return keys or set()

    def load_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]:
        return load_thread_meta(self._dir, system_id, thread_id) or {}

    # --- запись ------------------------------------------------------------

    def append_messages(
        self,
        system_id: str,
        thread_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        known_keys: Optional[set] = None,
        start_seq: int = 0,
    ) -> int:
        # Ключи не передали — читаем с диска сами. Идемпотентность обещана
        # КОНТРАКТОМ, значит держать её должно хранилище: батч персистится
        # дважды (после тулов и на выходе цикла), и вызывающий не обязан
        # помнить, что он уже записал.
        # НЕ копируем: append_new_thread_messages дописывает в этот же набор,
        # и вызывающий (ThreadSession.known_message_keys) рассчитывает увидеть
        # обновление у себя. Копия молча сломала бы дедупликацию на след. батче.
        keys = known_keys if known_keys is not None else self._known_keys(
            system_id, thread_id
        )
        return append_new_thread_messages(
            self._dir, system_id, thread_id, list(messages), keys,
            start_seq=start_seq,
        )

    def save_meta(self, system_id: str, thread_id: str, meta: Dict[str, Any]) -> None:
        save_thread_meta(self._dir, system_id, thread_id, meta)

    def patch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        patch_runtime_flag(
            self._dir, system_id, thread_id, flag,
            active=active, session_meta=session_meta,
        )

    def update_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        update_runtime_meta(self._dir, system_id, thread_id, patch)

    def discard_message(self, system_id: str, thread_id: str, message_id: str) -> bool:
        return bool(discard_message_by_id(self._dir, system_id, thread_id, message_id))

    def reset(self, system_id: str, thread_id: str) -> List[str]:
        return reset_thread_files(self._dir, system_id, thread_id)

    # --- трассы субагентов -------------------------------------------------

    def append_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return append_subagent_trace_record(
            self._dir, system_id, thread_id,
            parent_call_id=parent_call_id, child_run_id=child_run_id,
            kind=kind, payload=payload,
        )

    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        return read_subagent_trace(self._dir, system_id, thread_id) or []


__all__ = ["FileThreadStore"]
