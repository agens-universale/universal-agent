"""``MemoryThreadStore`` — вторая реализация ``ThreadStore``, целиком в памяти.

Живёт в тест-ките, а не в ядре, намеренно: это не продуктовая фича, а
доказательство того, что контракт годен. Ровно то, чего не хватило ``TaskStore``
в TUI — там протокол объявлен, но второй реализации нет, и потому он тихо
разъехался с фактическим употреблением (4 метода против 12 у файлового).

Наследует ``ThreadStoreBase`` — рекомендуемый путь: абстрактные методы
отказывают на этапе создания класса, а инварианты меты достаются готовыми.
Наследование при этом НЕ обязательно: проводка структурная, и чужой store,
который наследовать не может, принимается по именам методов.

Внутри — конверт ``{"seq": n, "payload": {...}}``, тот же, что пишет файловый
слой в ``data_*.jsonl``. Не ради сходства: без хранимого ``seq`` третий элемент
``load_thread_state`` неоткуда взять, и первая редакция этого класса отдавала
там ``len(bucket)`` — то есть КОЛИЧЕСТВО вместо НОМЕРА. Расходятся они после
первого же ``discard_message``, и весь конформанс-набор оставался зелёным.
Реализации на базе конверт нужен ровно так же: ``seq`` — это ordering key
хвостового читателя TUI, а не деталь формата файла.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from gc_core.thread_store_types import MessageKey, SubagentTraceRecord, ThreadState
from gc_core.thread_store_base import ThreadStoreBase


class MemoryThreadStore(ThreadStoreBase):
    """Хранилище тредов в словарях. Диска не касается вовсе."""

    def __init__(self) -> None:
        #: Конверты ``{"seq": int, "payload": dict}`` в порядке записи.
        self._messages: Dict[tuple, List[Dict[str, Any]]] = {}
        self._meta: Dict[tuple, Dict[str, Any]] = {}
        self._traces: Dict[tuple, List[SubagentTraceRecord]] = {}
        #: Счётчик обращений — тестам удобно проверять, что ходили именно сюда.
        self.calls: Dict[str, int] = {}

    def _hit(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    @staticmethod
    def _key(system_id: str, thread_id: str) -> tuple:
        return (system_id, thread_id)

    @staticmethod
    def _payloads(bucket: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Развернуть конверты в сообщения, гарантировав ``id``.

        ``ensure_message_id`` здесь не для симметрии с файлами, а потому что
        этого требует контракт: без ``id`` ключ идемпотентности уезжает в ветку
        ``payload:<json>``, нестабильную к любому изменению словаря.
        """
        from gc_core.thread_store import ensure_message_id

        return [ensure_message_id(copy.deepcopy(e["payload"])) for e in bucket]

    # --- чтение ------------------------------------------------------------

    def load_messages(self, system_id: str, thread_id: str) -> List[Dict[str, Any]]:
        self._hit("load_messages")
        return self._payloads(self._messages.get(self._key(system_id, thread_id), []))

    def load_thread_state(self, system_id: str, thread_id: str) -> ThreadState:
        self._hit("load_thread_state")
        from gc_core.thread_store import make_message_key

        bucket = self._messages.get(self._key(system_id, thread_id), [])
        messages = self._payloads(bucket)
        # НОМЕР последнего выданного seq, а не количество: после discard номер
        # проседает и переиспользуется — файловый слой ведёт себя ровно так же
        # (max по оставшимся записям), и расхождение здесь означало бы, что
        # переезд на другое хранилище меняет нумерацию.
        max_seq = max((int(e.get("seq") or 0) for e in bucket), default=0)
        return ThreadState(
            messages=messages,
            known_keys={make_message_key(m) for m in messages},
            max_seq=max_seq,
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
        known_keys: Optional[Set[MessageKey]] = None,
        start_seq: int = 0,
    ) -> int:
        self._hit("append_messages")
        from gc_core.thread_store import make_message_key

        bucket = self._messages.setdefault(self._key(system_id, thread_id), [])
        # Ключи не передали — собираем сами, как это делает файловая реализация.
        # Передали — работаем с ТЕМ ЖЕ объектом: вызывающий держит его у себя
        # (``ThreadSession.known_message_keys``) и снимает снимок ДО вызова,
        # чтобы по разнице понять, что дописать в свою ``history_messages``.
        # Защитная копия молча задвоила бы историю на следующем батче.
        known = (
            known_keys
            if known_keys is not None
            else {make_message_key(m) for m in self._payloads(bucket)}
        )

        seq = start_seq
        written = 0
        for message in messages:
            key = make_message_key(message)
            if key in known:
                continue          # идемпотентность: батч персистится дважды
            seq += 1
            # Пополняем ТОЛЬКО фактически записанным. Досыпать заодно всё, что
            # уже лежит в хранилище, нельзя: снимок вызывающего перестанет
            # отличать «записали сейчас» от «лежало раньше».
            known.add(key)
            bucket.append({"seq": seq, "payload": copy.deepcopy(message)})
            written += 1
        return written

    def save_meta(self, system_id: str, thread_id: str, meta: Dict[str, Any]) -> None:
        self._hit("save_meta")
        # Штамп записи — обязанность ХРАНИЛИЩА, а не вызывающего, и он один
        # на все реализации: ``ThreadStoreBase.stamp_meta``.
        self._meta[self._key(system_id, thread_id)] = self.stamp_meta(
            copy.deepcopy(meta)
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
        self._hit("patch_runtime_flag")
        key = self._key(system_id, thread_id)
        meta = self.apply_runtime_flag(
            self._meta.setdefault(key, {}), flag, active=active
        )
        self._meta[key] = self.stamp_meta(meta)

    def update_runtime_meta(
        self, system_id: str, thread_id: str, patch: Dict[str, Any]
    ) -> None:
        self._hit("update_runtime_meta")
        key = self._key(system_id, thread_id)
        meta = self.merge_runtime_meta(self._meta.setdefault(key, {}), patch)
        self._meta[key] = self.stamp_meta(meta)

    def discard_message(self, system_id: str, thread_id: str, message_id: str) -> bool:
        self._hit("discard_message")
        bucket = self._messages.get(self._key(system_id, thread_id), [])
        for i, envelope in enumerate(bucket):
            if envelope["payload"].get("id") == message_id:
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
        if self._traces.pop(key, None) is not None:
            removed.append("subagent_trace")
        return removed

    # --- трассы субагентов -------------------------------------------------

    def _restore_subagent_trace(self, system_id, thread_id, records) -> None:
        """Вернуть трассу СО СВОИМИ штампами: см. ``ThreadStoreBase``.

        Умолчание базы идёт через ``append_subagent_trace``, а тот ставит
        свежие ``ts`` и ``seq``: после компакции у всей трассы дата равнялась
        моменту компакции. Здесь кладём записи как есть.
        """
        self._hit("_restore_subagent_trace")
        self._traces[self._key(system_id, thread_id)] = [dict(r) for r in records]

    async def _arestore_subagent_trace(self, system_id, thread_id, records) -> None:
        """См. синхронный близнец: хранилище в памяти, ждать нечего."""
        self._restore_subagent_trace(system_id, thread_id, records)

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
        self._hit("append_subagent_trace")
        trace = self._traces.setdefault(self._key(system_id, thread_id), [])
        # Шесть полей, а не четыре. ``ts`` и ``seq`` проставляет ХРАНИЛИЩЕ —
        # первая редакция их не писала, а конформанс смотрел только на ``kind``
        # и потому оставался зелёным при разъехавшихся реализациях.
        record: SubagentTraceRecord = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "seq": len(trace) + 1,
            "parent_call_id": parent_call_id,
            "child_run_id": child_run_id,
            "kind": kind,
            "payload": copy.deepcopy(payload),
        }
        trace.append(record)
        return copy.deepcopy(record)

    def read_subagent_trace(
        self, system_id: str, thread_id: str
    ) -> List[SubagentTraceRecord]:
        self._hit("read_subagent_trace")
        return copy.deepcopy(self._traces.get(self._key(system_id, thread_id), []))


__all__ = ["MemoryThreadStore"]
