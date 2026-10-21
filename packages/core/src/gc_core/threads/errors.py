"""Отказы хранилища тредов v2 — одинаковые для любой реализации.

Временная недоступность самого хранилища (соединение, таймаут) —
``StoreUnavailable``: реализация переводит в него ошибки соединения своего
драйвера, а пишущий повторяет запись с тем же ``op_id``.
"""

from __future__ import annotations


class ThreadStoreError(Exception):
    """Общий предок отказов контракта."""


class ThreadConflict(ThreadStoreError):
    """``expected_revision`` не совпала: тред записал кто-то ещё."""


class OwnershipError(ThreadStoreError):
    """Чужая, истёкшая или сменённая аренда; запись без ``claim``."""


class ThreadBusy(OwnershipError):
    """Тред ведёт другая сессия (её аренда жива): ход или запись истории
    сейчас невозможны. ``owner`` — кто держит, если хранилище его назвало."""

    def __init__(self, message: str, *, owner: str | None = None) -> None:
        super().__init__(message)
        self.owner = owner


class OperationMismatch(ThreadStoreError):
    """Тот же ``op_id`` пришёл с другим содержимым."""


class StoreUnavailable(ThreadStoreError):
    """Хранилище временно недоступно (соединение, таймаут): запись можно
    повторить с тем же ``op_id`` — квитанция не даст её задвоить."""

    @classmethod
    def from_error(cls, exc: BaseException) -> StoreUnavailable:
        """Сообщение — только тип исходной ошибки: её текст бывает с хостом,
        пользователем или куском DSN, а сообщение уходит в статус задачи и
        колбэк. Исходная ошибка — в ``__cause__`` (для логов): ставит её
        ``raise … from exc`` вызывающего."""
        return cls(f"хранилище тредов недоступно ({type(exc).__name__})")
