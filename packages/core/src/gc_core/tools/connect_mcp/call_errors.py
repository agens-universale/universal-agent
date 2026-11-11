"""Вызов MCP-тула ушёл серверу, а ответа нет.

Исполнился ли вызов на сервере — неизвестно, поэтому автоповтор его не
повторяет. Пустой ответ вместо этих ошибок модель приняла бы за успех.
"""


class McpCallOutcomeUnknown(Exception):
    """Запрос отправлен, ответа нет: исход вызова неизвестен."""


class McpServerGone(McpCallOutcomeUnknown):
    """Сервер завершился, не ответив: транспортный сбой, сессия мертва."""

    def __init__(self) -> None:
        super().__init__("сервер завершился, не ответив")


class McpCallTimedOut(McpCallOutcomeUnknown):
    """Сервер жив, но не ответил за срок: сессия остаётся."""

    def __init__(self, timeout: float) -> None:
        super().__init__(f"сервер не ответил за {timeout:g} с")
        self.timeout = timeout


__all__ = ["McpCallOutcomeUnknown", "McpCallTimedOut", "McpServerGone"]
