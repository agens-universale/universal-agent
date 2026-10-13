"""Страница списка задач. Сортировка и срез, без записи состояния."""

from __future__ import annotations

from datetime import UTC, datetime

from gc_a2a.params import TaskQuery
from gc_a2a.store import Task, public


def page(tasks: list[Task], query: TaskQuery) -> dict:
    rows = [task for task in tasks if _match(task, query)]
    rows.sort(key=lambda task: (task.timestamp, task.sequence), reverse=True)
    start = query.offset
    chunk = rows[start : start + query.page_size]
    next_at = start + len(chunk)
    token = "" if next_at >= len(rows) else str(next_at)
    return {
        "tasks": [
            public(
                task,
                query.history_length,
                include_artifacts=query.include_artifacts,
            )
            for task in chunk
        ],
        "nextPageToken": token,
        "pageSize": query.page_size,
        "totalSize": len(rows),
    }


def _match(task: Task, query: TaskQuery) -> bool:
    if query.context_id is not None and task.context_id != query.context_id:
        return False
    if query.status is not None and task.state != query.status:
        return False
    if query.after is None:
        return True
    return _stamp(task.timestamp) > query.after


def _stamp(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
