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

from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    append_turn_outcome_record,
    archive_and_replace_thread_files,
    discard_message_by_id,
    ensure_threads_dir,
    load_thread_messages,
    load_thread_meta,
    patch_runtime_flag,
    read_subagent_trace,
    reset_thread_files,
    save_thread_meta,
    update_runtime_meta,
    update_thread_meta,
)
from gc_core.thread_store_base import ThreadStoreBase
from gc_core.thread_store_types import (
    MessageKey,
    SubagentTraceRecord,
    ThreadState,
)

#: Пишущие файловые функции (по имени: часть импортируется по месту).
_WRITE_HELPERS = frozenset(
    {
        "append_new_thread_messages",
        "append_subagent_trace_record",
        "append_turn_outcome_record",
        "archive_and_replace_thread_files",
        "discard_message_by_id",
        "patch_runtime_flag",
        "reset_thread_files",
        "restore_subagent_trace_records",
        "save_thread_meta",
        "update_runtime_meta",
        "update_thread_meta",
    }
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

    def _call(self, helper, *args, **kwargs):
        """Единая точка вызова файловых функций.

        Чтение — прямой вызов. Запись в тред, который знает журнал durable
        хранилища, — под его блокировкой (``local_write_scope``).
        ``DurableFileThreadStore`` переопределяет её, чтобы провести те же
        функции через журнал.
        """
        if helper.__name__ not in _WRITE_HELPERS:
            return helper(*args, **kwargs)
        from gc_core.thread_store_journal import local_write_scope

        directory, system_id, thread_id = args[:3]
        with local_write_scope(directory, system_id, thread_id):
            return helper(*args, **kwargs)

    # --- чтение ------------------------------------------------------------

    def load_messages(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        # Модульная функция отдаёт тройку (сообщения, ключи, счётчик записанных).
        # Наружу контракт обещает только сообщения: ключи — деталь дедупликации,
        # и держать её должен сам store, а не каждый вызывающий.
        messages, _keys, _count = self._call(
            load_thread_messages, self._dir, system_id, thread_id
        )
        return messages or []

    def load_thread_state(self, system_id: str, thread_id: str) -> ThreadState:
        """История со служебным. Третий элемент — НОМЕР последнего ``seq``.

        Файловый слой берёт его как ``max`` по оставшимся записям, поэтому после
        ``discard_message`` номер проседает и переиспользуется. Это и есть
        контрактное поведение: держат НЕУБЫВАНИЕ, а не уникальность.
        """
        messages, keys, max_seq = self._call(
            load_thread_messages, self._dir, system_id, thread_id
        )
        return ThreadState(
            messages=(messages or []),
            known_keys=(keys or set()),
            max_seq=int(max_seq or 0),
        )

    def _known_keys(self, system_id: str, thread_id: str) -> Set[MessageKey]:
        _messages, keys, _count = self._call(
            load_thread_messages, self._dir, system_id, thread_id
        )
        return keys or set()

    def load_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]:
        return (
            self._call(load_thread_meta, self._dir, system_id, thread_id) or {}
        )

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
        keys = (
            known_keys
            if known_keys is not None
            else self._known_keys(system_id, thread_id)
        )
        return self._call(
            append_new_thread_messages,
            self._dir,
            system_id,
            thread_id,
            list(messages),
            keys,
            start_seq=start_seq,
        )

    def save_meta(
        self, system_id: str, thread_id: str, meta: Dict[str, Any]
    ) -> None:
        self._call(save_thread_meta, self._dir, system_id, thread_id, meta)

    def update_meta(
        self,
        system_id: str,
        thread_id: str,
        merge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Чтение, слияние и запись меты — под замком треда в процессе."""
        return self._call(
            update_thread_meta, self._dir, system_id, thread_id, merge_fn
        )

    def patch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._call(
            patch_runtime_flag,
            self._dir,
            system_id,
            thread_id,
            flag,
            active=active,
            session_meta=session_meta,
        )

    def update_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        self._call(update_runtime_meta, self._dir, system_id, thread_id, patch)

    def discard_message(
        self, system_id: str, thread_id: str, message_id: str
    ) -> bool:
        return bool(
            self._call(
                discard_message_by_id,
                self._dir,
                system_id,
                thread_id,
                message_id,
            )
        )

    def save_plan_file(
        self, system_id: str, thread_id: str, plan: Dict[str, Any]
    ) -> str | None:
        """Экспорт плана сессии в файл для человека; путь либо None, если
        плана нет. Возможность именно файлового стора: исполнитель ищет её по
        имени и файловую систему сам не трогает."""
        from gc_core.persistence.plan_file import (
            render_plan_file,
            write_plan_file,
        )
        from gc_core.thread_store import plan_file_path

        text = render_plan_file(plan, thread_id)
        if not text:
            return None
        path = plan_file_path(self._dir, system_id, thread_id)
        write_plan_file(path, text)
        return path

    def reset(self, system_id: str, thread_id: str) -> List[str]:
        return self._call(reset_thread_files, self._dir, system_id, thread_id)

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

        self._call(
            restore_subagent_trace_records,
            self._dir,
            system_id,
            thread_id,
            list(records),
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
        return self._call(
            append_subagent_trace_record,
            self._dir,
            system_id,
            thread_id,
            parent_call_id=parent_call_id,
            child_run_id=child_run_id,
            kind=kind,
            payload=payload,
        )

    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        return (
            self._call(read_subagent_trace, self._dir, system_id, thread_id)
            or []
        )

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
        return self._call(
            archive_and_replace_thread_files,
            self._dir,
            system_id,
            thread_id,
            messages,
        )

    def append_turn_outcome(
        self,
        system_id: str,
        thread_id: str,
        outcome: TurnOutcome,
        *,
        seq: int,
    ) -> None:
        """Дописать запись итога хода (необязательная возможность).

        Не входит в контракт: сессия ищет её по имени и без неё записи не
        пишет. ``seq`` — номер последнего сообщения хода.
        """
        self._call(
            append_turn_outcome_record,
            self._dir,
            system_id,
            thread_id,
            outcome,
            seq=seq,
        )


__all__ = ["FileThreadStore"]
