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

from gc_core.usage.account import CATEGORY_UNCATEGORIZED, UsageAccount
from gc_core.usage.currency import provider_currency_for_url
from gc_core.usage.normalize import normalize_usage

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


def _currency_for(endpoint_url: str | None) -> str | None:
    """Валюта фактической цены этого вызова.

    Адрес не передан → ``None``: сумма ляжет как «валюта неизвестна» и будет
    разрешена при показе по текущему конфигу (поведение старых версий).
    """
    return provider_currency_for_url(endpoint_url) if endpoint_url else None


def record_usage(raw_usage: Any, endpoint_url: str | None = None) -> None:
    """Записать usage из ответа модели в активный контекст.

    Зовётся из ``connect_backend`` сразу после получения ответа: ``endpoint_url`` —
    адрес именно этого вызова, по нему фиксируется валюта цены. Никогда не кидает.
    """
    ctx = _usage_ctx.get()
    if ctx is None:
        # Не обёрнутый путь: не теряем, но громко помечаем.
        parsed = normalize_usage(raw_usage)
        _log.warning(
            "usage recorded outside usage_scope (uncategorized); parsed=%s",
            parsed,
        )
        _orphan_account.add_record(raw_usage, endpoint_url)
        return
    parsed = normalize_usage(raw_usage)
    if parsed is None:
        ctx.account.add_missing(ctx.category)
    else:
        ctx.account.add(
            ctx.category, parsed.input, parsed.output, parsed.cached,
            cost=parsed.cost, billed=parsed.billed,
            currency=_currency_for(endpoint_url),
        )


class _OrphanAccount:
    """Запасной приёмник для вызовов без контекста (только для наблюдаемости/тестов)."""

    def __init__(self) -> None:
        self.account = UsageAccount()

    def add_record(self, raw_usage: Any, endpoint_url: str | None = None) -> None:
        parsed = normalize_usage(raw_usage)
        if parsed is None:
            self.account.add_missing(CATEGORY_UNCATEGORIZED)
        else:
            self.account.add(
                CATEGORY_UNCATEGORIZED, parsed.input, parsed.output, parsed.cached,
                cost=parsed.cost, billed=parsed.billed,
                currency=_currency_for(endpoint_url),
            )


_orphan_account = _OrphanAccount()
