"""Чистый селектор сессий под удаление (по критерию). Без побочных эффектов.

Используется и живым счётчиком MassDeleteScreen, и оркестрацией SessionsScreen —
поэтому `now` инъектируется (детерминируемые тесты)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def _parse_updated_at(value: str, now: datetime) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # naive → считаем локальной зоной (как cleanup-скрипт)
        dt = dt.replace(tzinfo=now.tzinfo)
    return dt


def select_for_deletion(
    tasks: list,
    *,
    keep_last: Optional[int] = None,
    older_than_days: Optional[int] = None,
    now: datetime,
) -> list:
    """Вернуть thread_id под удаление. Задан ровно один критерий.

    keep_last=N → всё, кроме N самых свежих (по updated_at desc).
    older_than_days=D → всё, где updated_at < now - D дней (непарсируемое — не удаляем).
    """
    ordered = sorted(tasks, key=lambda t: t.updated_at, reverse=True)
    if keep_last is not None:
        return [t.thread_id for t in ordered[keep_last:]]
    if older_than_days is not None:
        cutoff = now - timedelta(days=older_than_days)
        out = []
        for t in ordered:
            dt = _parse_updated_at(t.updated_at, now)
            if dt is not None and dt < cutoff:
                out.append(t.thread_id)
        return out
    return []
