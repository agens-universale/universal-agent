"""Атрибуция usage через ``contextvar``.

Высокоуровневые вызыватели LLM оборачивают свой вызов в ``usage_scope`` —
тогда низкоуровневый транспорт (``connect_backend``) знает, в какой
``UsageAccount`` и под какой категорией записать токены ответа.

Контекст отсутствует → ``record_usage`` пишет в категорию ``uncategorized``
и логирует warning: это «детектор дыр» — ни один путь не теряется молча.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from usage.account import CATEGORY_UNCATEGORIZED, UsageAccount
from usage.normalize import normalize_usage

_log = logging.getLogger(__name__)


@dataclass
class UsageContext:
    account: UsageAccount
    category: str
    backend_mode: str | None = None


_usage_ctx: ContextVar[Optional[UsageContext]] = ContextVar("usage_ctx", default=None)


def current_usage_context() -> Optional[UsageContext]:
    return _usage_ctx.get()


@contextmanager
def usage_scope(
    account: UsageAccount | None,
    category: str,
    backend_mode: str | None = None,
) -> Iterator[None]:
    """Назначить активный sink для всех LLM-вызовов внутри блока.

    ``account is None`` → no-op (например, у вызывателя нет state с usage):
    запросы выполнятся, но не учтутся.
    """
    if account is None:
        yield
        return
    token = _usage_ctx.set(UsageContext(account=account, category=category, backend_mode=backend_mode))
    try:
        yield
    finally:
        _usage_ctx.reset(token)


def record_usage(raw_usage: Any) -> None:
    """Записать usage из ответа модели в активный контекст.

    Зовётся из ``connect_backend`` сразу после получения ответа. Никогда не кидает.
    """
    ctx = _usage_ctx.get()
    if ctx is None:
        # Не обёрнутый путь: не теряем, но громко помечаем.
        parsed = normalize_usage(raw_usage)
        _log.warning(
            "usage recorded outside usage_scope (uncategorized); parsed=%s",
            parsed,
        )
        _orphan_account.add_record(raw_usage)
        return
    parsed = normalize_usage(raw_usage, ctx.backend_mode)
    if parsed is None:
        ctx.account.add_missing(ctx.category)
    else:
        ctx.account.add(ctx.category, parsed[0], parsed[1], parsed[2])


class _OrphanAccount:
    """Запасной приёмник для вызовов без контекста (только для наблюдаемости/тестов)."""

    def __init__(self) -> None:
        self.account = UsageAccount()

    def add_record(self, raw_usage: Any) -> None:
        parsed = normalize_usage(raw_usage)
        if parsed is None:
            self.account.add_missing(CATEGORY_UNCATEGORIZED)
        else:
            self.account.add(CATEGORY_UNCATEGORIZED, parsed[0], parsed[1], parsed[2])


_orphan_account = _OrphanAccount()
