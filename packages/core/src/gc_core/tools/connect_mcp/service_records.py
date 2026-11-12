"""Записи сервиса MCP: общие факты о сервере, отказ подключения, остановка.

Общий словарь сервиса (``service``) и его потребителей (``consumer``):
сервис заводит и обновляет записи, потребитель читает их и пишет свои
отказы. Отдельный модуль — чтобы потребитель не зависел от сервиса
по импорту, а сервис мог выдавать потребителей.
"""

from dataclasses import dataclass

#: Текст отказа после остановки сервиса или закрытия потребителя.
SERVICE_CLOSED_TEXT = "сервис MCP остановлен"


class McpServiceClosed(RuntimeError):
    """Обращение к сервису или потребителю после начала остановки."""


@dataclass(frozen=True)
class Failure:
    """Отказ подключения: когда (часы сервиса), код и текст причины."""

    at: float
    code: str
    reason: str


@dataclass(eq=False)
class ServerEntry:
    """Общие факты о сервере для всех потребителей сервиса.

    ``cfg`` — определение без trace (``plain_dump(model, by_alias=True)``)
    после ``with_service_defaults``; ``tools`` — сырой ответ
    ``tools/list``, до whitelist и оформления. ``interrupted_at`` —
    начало последней попытки, прерванной закрытием её потребителя: у
    записи в отказе от него тоже считается правило 30 с (не отказ —
    статус и причину не меняет).
    """

    key: str
    cfg: dict
    fingerprint: str
    discovery_timeout: float
    tools: tuple = ()
    from_catalog: bool = False
    checked_at: float | None = None
    last_failure: Failure | None = None
    interrupted_at: float | None = None

    @property
    def tools_known(self) -> bool:
        """Список тулов известен — из каталога или от подключения.

        Пустой известный список — тоже известен: сервер без тулов.
        """
        return self.from_catalog or self.checked_at is not None


__all__ = [
    "SERVICE_CLOSED_TEXT",
    "Failure",
    "McpServiceClosed",
    "ServerEntry",
]
