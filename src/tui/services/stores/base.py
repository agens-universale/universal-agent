"""TaskStore Protocol — UI читает трейсы ТОЛЬКО через этот контракт.

§5/§6 tui3_impl. UI не знает деталей хранения (файлы / БД / удалённый API).
"""
from __future__ import annotations

from typing import Protocol

from tui.models import TailResult, TaskCursor, TaskSummary


class TaskStore(Protocol):
    def list_tasks(self, *, limit: int = 200, offset: int = 0) -> list[TaskSummary]:
        """Список задач для экрана /tasks."""
        ...

    def get_task_meta(self, task_id: str, system: str = "cli") -> dict:
        """Полный meta JSON задачи (для /status --raw)."""
        ...

    def read_all_events(
        self, task_id: str, system: str = "cli"
    ) -> tuple[list[dict], TaskCursor]:
        """Полный read для cold-start и stream_reset."""
        ...

    def tail_events(
        self, task_id: str, cursor: TaskCursor, system: str = "cli"
    ) -> TailResult:
        """Инкремент после cursor; stream_reset=True при смене storage_epoch."""
        ...
