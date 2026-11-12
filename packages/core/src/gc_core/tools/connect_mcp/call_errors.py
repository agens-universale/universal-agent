"""Вызов MCP-тула ушёл серверу, а ответа нет.

Исполнился ли вызов на сервере — неизвестно, поэтому автоповтор его не
повторяет. Пустой ответ вместо этих ошибок модель приняла бы за успех.

Здесь же — общая для сервиса и исполнителя вызова таблица «истёк срок
вызова» и разворот группы исключений до листа: одна классификация на
подключение и вызов.
"""

import httpx


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


#: Истёк срок вызова: сервер жив, ответа за срок нет. Сроки соединения
#: (``ConnectTimeout``, ``PoolTimeout``) сюда не входят — для вызова это
#: ошибка соединения, а не срок.
CALL_TIMEOUTS = (
    McpCallTimedOut,
    TimeoutError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
)


def first_leaf(exc: BaseException) -> BaseException:
    """Первый лист группы исключений; не группа — само исключение."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


__all__ = [
    "CALL_TIMEOUTS",
    "McpCallOutcomeUnknown",
    "McpCallTimedOut",
    "McpServerGone",
    "first_leaf",
]
