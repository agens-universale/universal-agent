from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


@dataclass
class ToolCallOutputRecord:
    output_ref: str
    source_tool: str
    call_id: str
    total_chars: int
    text: str
    truncated_in_message: bool = True


def _record_from_item(item: Any) -> Optional[ToolCallOutputRecord]:
    """Запись из сериализованного словаря; None — форма не та (запись пропускается)."""
    if not isinstance(item, dict):
        return None
    output_ref = item.get("output_ref")
    source_tool = item.get("source_tool")
    call_id = item.get("call_id")
    total_chars = item.get("total_chars")
    text = item.get("text")
    truncated_in_message = item.get("truncated_in_message", True)
    if not isinstance(output_ref, str) or not output_ref:
        return None
    if not isinstance(source_tool, str) or not isinstance(call_id, str):
        return None
    if not isinstance(total_chars, int) or not isinstance(text, str):
        return None
    if not isinstance(truncated_in_message, bool):
        truncated_in_message = True
    return ToolCallOutputRecord(
        output_ref=output_ref,
        source_tool=source_tool,
        call_id=call_id,
        total_chars=total_chars,
        text=text,
        truncated_in_message=truncated_in_message,
    )


@dataclass
class ToolCallOutputsStore:
    max_entries: int = 10
    _records: OrderedDict[str, ToolCallOutputRecord] = field(default_factory=OrderedDict)

    _SERIALIZATION_SCHEMA_VERSION: int = 1

    def __post_init__(self) -> None:
        # Важно: нормализация лимитов — ответственность конфиг-слоя.
        # Store ожидает уже валидированное значение.
        if not isinstance(self.max_entries, int):
            raise TypeError("ToolCallOutputsStore.max_entries must be int")
        if self.max_entries < 0:
            raise ValueError("ToolCallOutputsStore.max_entries must be >= 0")

    def __len__(self) -> int:
        return len(self._records)

    def contains(self, output_ref: str) -> bool:
        return output_ref in self._records

    def keys(self) -> Iterable[str]:
        return self._records.keys()

    def is_disabled(self) -> bool:
        return self.max_entries == 0

    def set_max_entries(self, max_entries: int) -> None:
        if not isinstance(max_entries, int):
            raise TypeError("max_entries must be int")
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")
        self.max_entries = max_entries
        self._evict_if_needed()

    def put(self, record: ToolCallOutputRecord) -> Optional[ToolCallOutputRecord]:
        if self.is_disabled():
            return None
        evicted: Optional[ToolCallOutputRecord] = None
        if record.output_ref in self._records:
            del self._records[record.output_ref]
        self._records[record.output_ref] = record
        evicted = self._evict_if_needed()
        return evicted

    def get(self, output_ref: str) -> Optional[ToolCallOutputRecord]:
        record = self._records.get(output_ref)
        if record is None:
            return None
        self._records.move_to_end(output_ref, last=True)
        return record

    def peek(self, output_ref: str) -> Optional[ToolCallOutputRecord]:
        return self._records.get(output_ref)

    def _evict_if_needed(self) -> Optional[ToolCallOutputRecord]:
        if self.max_entries == 0:
            if self._records:
                self._records.clear()
            return None
        evicted: Optional[ToolCallOutputRecord] = None
        while len(self._records) > self.max_entries:
            _, evicted = self._records.popitem(last=False)
        return evicted

    def to_dict(self) -> Dict[str, Any]:
        """
        Сериализация store для дампов state.

        Важно: порядок записей в списке отражает LRU-порядок (OrderedDict order).
        """
        records = [
            {
                "output_ref": rec.output_ref,
                "source_tool": rec.source_tool,
                "call_id": rec.call_id,
                "total_chars": rec.total_chars,
                "text": rec.text,
                "truncated_in_message": rec.truncated_in_message,
            }
            for rec in self._records.values()
        ]
        return {
            "schema_version": self._SERIALIZATION_SCHEMA_VERSION,
            "max_entries": self.max_entries,
            "records": records,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> ToolCallOutputsStore:
        if not isinstance(raw, dict):
            raise TypeError("ToolCallOutputsStore.from_dict expects dict")
        max_entries = int(raw.get("max_entries", 10))
        if max_entries < 0:
            raise ValueError("ToolCallOutputsStore.max_entries must be >= 0")
        store = cls(max_entries=max_entries)

        records_raw = raw.get("records") or []
        if not isinstance(records_raw, list):
            records_raw = []

        reconstructed: OrderedDict[str, ToolCallOutputRecord] = OrderedDict()
        for item in records_raw:
            record = _record_from_item(item)
            if record is not None:
                reconstructed[record.output_ref] = record

        store._records = reconstructed
        return store

