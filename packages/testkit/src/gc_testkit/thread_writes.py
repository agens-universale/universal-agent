"""Записи в тред так, как их делает ход агента, — для посева в тестах."""

from __future__ import annotations

from typing import Any

from gc_core.threads.models import Compaction, ThreadKey
from gc_core.threads.session_writer import SessionWriter


async def acompact(
    store: Any,
    key: ThreadKey,
    messages: list,
    *,
    archive: bool = True,
    owner: str = "test",
) -> None:
    """Сжать историю: прежняя — в архив, вместо неё ``messages``; секция TUI
    тронута, как после сжатия ядром. ``archive=False`` — замена истории без
    архива. Сессия берёт тред и в конце отпускает (иначе следующая запись —
    ThreadBusy)."""
    writer = SessionWriter(store, key, owner=owner)
    await writer.aclaim()
    try:
        await writer.acommit(
            replace_history=Compaction(
                messages=list(messages), archive=archive
            )
        )
        if archive:
            await writer.aside(patch_frontend=("tui", {}))
    finally:
        await writer.arelease()


def subagent_record(
    threads_dir: str, system: str, thread: str, **record: Any
) -> dict:
    """Запись трассы субагентов в файловый тред — синхронно (тесты TUI на
    ``run_test``): ``parent_call_id``, ``child_run_id``, ``kind``,
    ``payload``. Путь записи — тот же, что у ядра."""
    from gc_core.thread_store import append_subagent_record
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore

    key = ThreadKey(system=system, thread=thread)
    return run_sync(
        append_subagent_record(FileThreadStore(threads_dir), key, **record)
    )
