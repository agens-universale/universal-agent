"""Отказы хранилища тредов v2 — одинаковые для любой реализации.

Недоступность самого хранилища (сеть, диск) сюда не входит: исключения
драйвера пробрасываются как есть.
"""

from __future__ import annotations


class ThreadStoreError(Exception):
    """Общий предок отказов контракта."""


class ThreadConflict(ThreadStoreError):
    """``expected_revision`` не совпала: тред записал кто-то ещё."""


class OwnershipError(ThreadStoreError):
    """Чужая, истёкшая или сменённая аренда; запись без ``claim``."""


class OperationMismatch(ThreadStoreError):
    """Тот же ``op_id`` пришёл с другим содержимым."""
