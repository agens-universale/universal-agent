"""Durable-исполнение ``AsyncAgent``: протокол, без зависимости от рантайма.

``AsyncAgent.create(settings, durable=...)`` отдаёт ходы рантайму, который
исполняет их надёжно (Temporal: ``gc_core_temporalio.TemporalDurability``).
Ядро знает только этот протокол — само оно Temporal не импортирует.
"""

from __future__ import annotations

from typing import Any, Protocol


class DurableSession(Protocol):
    """Сторона агента в durable-рантайме: ходы по тредам."""

    async def run(
        self,
        messages: list[str],
        *,
        thread_id: str | None = None,
        system: str = "sdk",
        message_ids: list[str] | None = None,
    ) -> Any:
        """Отправить сообщения в тред и дождаться ответа хода."""

    def send(self, message: dict) -> None:
        """Сообщение в идущий ход (или в следующий)."""

    def stop(self) -> None:
        """Кооперативно остановить идущий ход."""

    async def aclose(self) -> None:
        """Освободить ресурсы стороны агента (не сессии в рантайме)."""


class DurableRuntime(Protocol):
    """Фабрика сторон агента: ``open(settings)`` при ``AsyncAgent.create``."""

    async def open(self, settings: Any) -> DurableSession: ...
