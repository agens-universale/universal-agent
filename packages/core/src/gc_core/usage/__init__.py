"""Подсчёт токенов и стоимости прогона агента.

Состав:
- ``normalize`` — нормализация usage из ответов обоих бэкендов;
- ``account`` — аккумулятор токенов по категориям (``UsageAccount``);
- ``context`` — contextvar + ``usage_scope`` для атрибуции вызовов;
- ``cost`` — расчёт стоимости из ``UsageAccount`` + тарифов;
- ``report`` — рендер итоговой таблицы.
"""

from gc_core.usage.account import CATEGORY_UNCATEGORIZED, UsageAccount, category_for_request_label
from gc_core.usage.context import current_usage_context, record_usage, usage_scope
from gc_core.usage.normalize import normalize_usage

__all__ = [
    "CATEGORY_UNCATEGORIZED",
    "UsageAccount",
    "category_for_request_label",
    "current_usage_context",
    "normalize_usage",
    "record_usage",
    "usage_scope",
]
