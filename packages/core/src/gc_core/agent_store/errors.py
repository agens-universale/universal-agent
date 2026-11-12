"""Ошибки хранилища агента."""

from __future__ import annotations

from gc_core.agent_store.models import ProjectMemory


class MemoryConflict(Exception):
    """Память проекта изменили параллельно: версия на месте не та, что
    ожидал пишущий. ``current`` — что лежит сейчас (``None`` — памяти нет)."""

    def __init__(self, current: ProjectMemory | None) -> None:
        super().__init__("память проекта изменили параллельно")
        self.current = current
