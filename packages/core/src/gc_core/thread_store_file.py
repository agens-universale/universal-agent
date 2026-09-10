"""``FileThreadStore`` — реализация ``ThreadStore`` поверх ``data_*.jsonl``.

Это ровно нынешнее поведение, собранное в класс: методы делегируют в модульные
функции ``thread_store``, где логика и остаётся. Ничего не переписано — иначе
переезд на контракт превратился бы в переписывание формата хранения, а это
разные работы с разной ценой ошибки.

Наследует :class:`ThreadStoreBase` — рекомендуемый путь: ABC отказывает на
неполной реализации прямо в конструкторе, а общие инварианты (штампы meta,
правило пустой секции ``runtime``) живут в базе в одном экземпляре, а не по
копии на реализацию. Копий было две, и они уже разъехались: штампы стояли
здесь и отсутствовали у ``MemoryThreadStore``.

Наследование при этом НЕ обязательно: ``athread_store`` по-прежнему принимает
любое хранилище, совпавшее по именам и сигнатурам, — чужой объект из
DI-контейнера наследовать нас не обязан.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from gc_core.thread_store_base import ThreadStoreBase
from gc_core.thread_store_types import (
    MessageKey,
    SubagentTraceRecord,
    ThreadState,
)
from gc_core.thread_store import (
    append_new_thread_messages,
    archive_and_replace_thread_files,
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


class FileThreadStore(ThreadStoreBase):
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

    def load_thread_state(self, system_id: str, thread_id: str) -> ThreadState:
        """История со служебным. Третий элемент — НОМЕР последнего ``seq``.

        Файловый слой берёт его как ``max`` по оставшимся записям, поэтому после
        ``discard_message`` номер проседает и переиспользуется. Это и есть
        контрактное поведение: держат НЕУБЫВАНИЕ, а не уникальность.
        """
        messages, keys, max_seq = load_thread_messages(self._dir, system_id, thread_id)
        return ThreadState(
            messages=(messages or []),
            known_keys=(keys or set()),
            max_seq=int(max_seq or 0),
        )

    def _known_keys(self, system_id: str, thread_id: str) -> Set[MessageKey]:
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
        known_keys: Optional[Set[MessageKey]] = None,
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

    def _restore_subagent_trace(self, system_id, thread_id, records) -> None:
        """Вернуть трассу СО СВОИМИ штампами: см. базовый класс.

        Формы одна. Корутинного близнеца звать было НЕКОМУ: его вызывает
        только ``AsyncThreadStoreBase.areplace_messages``, а это хранилище
        синхронное и наследует синхронную базу — ядро приходит сюда через
        адаптер ``thread_store_async``, то есть по синхронному имени.

        Умолчание базы идёт через ``append_subagent_trace``, а тот ставит
        свежие ``ts`` и ``seq``. Здесь пишем файл целиком записями как есть.
        """
        from gc_core.thread_store import restore_subagent_trace_records

        restore_subagent_trace_records(
            self._dir, system_id, thread_id, list(records)
        )

    def append_subagent_trace(
        self,
        system_id: str,
        thread_id: str,
        *,
        parent_call_id: str,
        child_run_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> SubagentTraceRecord:
        return append_subagent_trace_record(
            self._dir, system_id, thread_id,
            parent_call_id=parent_call_id, child_run_id=child_run_id,
            kind=kind, payload=payload,
        )

    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        return read_subagent_trace(self._dir, system_id, thread_id) or []

    # --- необязательная способность: архив перед компакцией ------------------

    def archive_and_replace(
        self, system_id: str, thread_id: str, messages: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Отложить историю в архив и заменить её на ``messages``.

        НЕ входит в одиннадцать методов контракта — это необязательная
        способность. Хранилище, которое её не объявляет, получает от сессии
        ``reset`` + ``append_messages``: то же замещение, но без архива. Так и
        должно быть — «отложить прежнюю историю рядом» осмысленно для файлов,
        а у базы это либо отдельная таблица, либо не нужно вовсе.

        Метод появился, когда выяснилось, что компакция писала в файлы НАПРЯМУЮ
        мимо контракта: ``_thread_paths(self.threads_dir, ...)`` плюс голый
        ``open(data_path, "w")`` в ``ThreadSession``. У хранилища без файлов
        ``threads_dir`` — пустая строка, и запись уходила в относительный путь
        от текущего каталога. Молча.

        Возвращает ``{"archive_suffix", "archive_data_path", "archive_meta_path"}``;
        пути — ``None``, если архивировать было нечего.
        """
        return archive_and_replace_thread_files(
            self._dir, system_id, thread_id, messages
        )


__all__ = ["FileThreadStore"]
