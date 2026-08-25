"""``MemoryThreadStore`` — вторая реализация ``ThreadStore``, целиком в памяти.

Живёт в тест-ките, а не в ядре, намеренно: это не продуктовая фича, а
доказательство того, что контракт годен. Ровно то, чего не хватило ``TaskStore``
в TUI — там протокол объявлен, но второй реализации нет, и потому он тихо
разъехался с фактическим употреблением (4 метода против 12 у файлового).

Ничего не наследует: ``ThreadStore`` — ``Protocol``, структурная типизация.
Так же будет выглядеть и чужой store поверх базы.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence


class MemoryThreadStore:
    """Хранилище тредов в словарях. Диска не касается вовсе."""

    def __init__(self) -> None:
        self._messages: Dict[tuple, List[Dict[str, Any]]] = {}
        self._meta: Dict[tuple, Dict[str, Any]] = {}
        self._traces: Dict[tuple, List[Dict[str, Any]]] = {}
        #: Счётчик обращений — тестам удобно проверять, что ходили именно сюда.
        self.calls: Dict[str, int] = {}

    def _hit(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    @staticmethod
    def _key(system_id: str, thread_id: str) -> tuple:
        return (system_id, thread_id)

    # --- чтение ------------------------------------------------------------

    def load_messages(self, system_id: str, thread_id: str) -> List[Dict[str, Any]]:
        self._hit("load_messages")
        return copy.deepcopy(self._messages.get(self._key(system_id, thread_id), []))

    def load_thread_state(self, system_id: str, thread_id: str):
        self._hit("load_thread_state")
        from gc_core.thread_store import make_message_key

        bucket = self._messages.get(self._key(system_id, thread_id), [])
        return (
            copy.deepcopy(bucket),
            {make_message_key(m) for m in bucket},
            len(bucket),
        )

    def load_meta(self, system_id: str, thread_id: str) -> Dict[str, Any]:
        self._hit("load_meta")
        return copy.deepcopy(self._meta.get(self._key(system_id, thread_id), {}))

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
        self._hit("append_messages")
        from gc_core.thread_store import make_message_key

        bucket = self._messages.setdefault(self._key(system_id, thread_id), [])
        # Работаем с ТЕМ ЖЕ набором, что передал вызывающий: он ждёт, что новые
        # ключи появятся у него (симметрия с файловой реализацией).
        seen = known_keys if known_keys is not None else set()
        seen |= {make_message_key(m) for m in bucket}
        written = 0
        for message in messages:
            key = make_message_key(message)
            if key in seen:
                continue          # идемпотентность: батч персистится дважды
            seen.add(key)
            bucket.append(copy.deepcopy(message))
            written += 1
        return written

    def save_meta(self, system_id: str, thread_id: str, meta: Dict[str, Any]) -> None:
        self._hit("save_meta")
        self._meta[self._key(system_id, thread_id)] = copy.deepcopy(meta)

    def patch_runtime_flag(
        self,
        system_id: str,
        thread_id: str,
        flag: str,
        *,
        active: bool,
        session_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._hit("patch_runtime_flag")
        # Имя флага валидируется так же, как в файловой реализации: иначе
        # контракт разошёлся бы с фактом, и опечатка в имени прошла бы молча.
        from gc_core.persistence.meta_ownership import resolve_runtime_flag_name

        flag = resolve_runtime_flag_name(flag)
        meta = self._meta.setdefault(self._key(system_id, thread_id), {})
        runtime = meta.setdefault("runtime", {})
        if active:
            runtime[flag] = True
        else:
            runtime.pop(flag, None)
        # Пустую секцию убираем целиком — так делает файловая реализация, и
        # расхождение здесь означало бы, что переезд на БД меняет форму meta.
        if not runtime:
            meta.pop("runtime", None)

    def update_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        self._hit("update_runtime_meta")
        meta = self._meta.setdefault(self._key(system_id, thread_id), {})
        runtime = meta.setdefault("runtime", {})
        runtime.update(copy.deepcopy(patch))
        if not runtime:
            meta.pop("runtime", None)

    def discard_message(self, system_id: str, thread_id: str, message_id: str) -> bool:
        self._hit("discard_message")
        bucket = self._messages.get(self._key(system_id, thread_id), [])
        for i, message in enumerate(bucket):
            if message.get("id") == message_id:
                bucket.pop(i)
                return True
        return False

    def reset(self, system_id: str, thread_id: str) -> List[str]:
        self._hit("reset")
        key = self._key(system_id, thread_id)
        removed = []
        if self._messages.pop(key, None) is not None:
            removed.append("messages")
        if self._meta.pop(key, None) is not None:
            removed.append("meta")
        self._traces.pop(key, None)
        return removed

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
        self._hit("append_subagent_trace")
        record = {
            "parent_call_id": parent_call_id,
            "child_run_id": child_run_id,
            "kind": kind,
            "payload": copy.deepcopy(payload),
        }
        self._traces.setdefault(self._key(system_id, thread_id), []).append(record)
        return record

    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[Dict[str, Any]]:
        self._hit("read_subagent_trace")
        return copy.deepcopy(self._traces.get(self._key(system_id, thread_id), []))


__all__ = ["MemoryThreadStore"]
