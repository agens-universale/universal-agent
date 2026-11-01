"""Записи в тред так, как их делает ход агента, — для посева в тестах."""

from __future__ import annotations

import uuid
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


def turn_outcome_record(
    threads_dir: str,
    system: str,
    thread: str,
    outcome: Any,
    *,
    after: str | None = None,
) -> None:
    """Итог хода в файловый тред так, как его пишет сессия ядра: запись
    потока ``turns`` за сообщением ``after`` (по умолчанию — последним в
    истории) и короткий итог в ``runtime.last_turn``. Синхронно (тесты TUI
    на ``run_test``)."""
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore

    key = ThreadKey(system=system, thread=thread)
    run_sync(_aturn_outcome(FileThreadStore(threads_dir), key, outcome, after))


async def _aturn_outcome(store: Any, key: ThreadKey, outcome, after) -> None:
    from gc_core.persistence.turn_outcome import last_turn_of, turn_entry
    from gc_core.threads.models import TURNS, RuntimeStatus, SideChange
    from gc_core.threads.runtime_rules import with_last_turn

    if after is None:
        history = await store.read_stream(key, "messages")
        after = history[-1].data.get("id") if history else None
    record = await store.get_thread(key)
    runtime = record.runtime if record is not None else RuntimeStatus()
    await store.append_side(
        key,
        SideChange(
            op_id=f"turn_{uuid.uuid4().hex}",
            append=[(TURNS, [turn_entry(outcome, after=after)])],
            set_runtime=with_last_turn(runtime, last_turn_of(outcome)),
        ),
    )


def model_choice_record(
    threads_dir: str,
    system: str,
    thread: str,
    record: Any,
    *,
    after: str | None = None,
) -> None:
    """Смена модели в файловый тред так, как её пишет сессия ядра: запись
    потока ``model_choices`` за сообщением ``after`` (по умолчанию —
    последним в истории). Синхронно (тесты TUI на ``run_test``)."""
    from gc_core.persistence.model_choice_record import model_choice_entry
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import MODEL_CHOICES, SideChange

    store = FileThreadStore(threads_dir)
    key = ThreadKey(system=system, thread=thread)

    async def _achoice() -> None:
        anchor = after
        if anchor is None:
            history = await store.read_stream(key, "messages")
            anchor = history[-1].data.get("id") if history else None
        entry = model_choice_entry(record, after=anchor)
        await store.append_side(
            key,
            SideChange(
                op_id=f"choice_{uuid.uuid4().hex}",
                append=[(MODEL_CHOICES, [entry])],
            ),
        )

    run_sync(_achoice())


def reasoning_record(
    threads_dir: str,
    system: str,
    thread: str,
    record: Any,
    *,
    after: str | None = None,
) -> None:
    """Мысль модели в файловый тред так, как её пишет сессия ядра: запись
    потока ``reasoning`` за сообщением ``after`` (по умолчанию —
    последним в истории). Синхронно (тесты TUI на ``run_test``)."""
    from gc_core.persistence.reasoning_record import reasoning_entry
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import REASONING, SideChange

    store = FileThreadStore(threads_dir)
    key = ThreadKey(system=system, thread=thread)

    async def _athought() -> None:
        anchor = after
        if anchor is None:
            history = await store.read_stream(key, "messages")
            anchor = history[-1].data.get("id") if history else None
        entry = reasoning_entry(record, after=anchor)
        await store.append_side(
            key,
            SideChange(
                op_id=f"thought_{uuid.uuid4().hex}",
                append=[(REASONING, [entry])],
            ),
        )

    run_sync(_athought())


def runtime_flag(
    threads_dir: str, system: str, thread: str, flag: str, *, active: bool
) -> None:
    """Флаг хода в runtime файлового треда (``summarizing``,
    ``finalizing``, ``finish_gate_checking``) — записью сбоку, как у
    сессии ядра. Синхронно (тесты TUI на ``run_test``)."""
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import RuntimeStatus, SideChange
    from gc_core.threads.runtime_rules import with_flag

    store = FileThreadStore(threads_dir)
    key = ThreadKey(system=system, thread=thread)

    async def _aflag() -> None:
        record = await store.get_thread(key)
        runtime = record.runtime if record is not None else RuntimeStatus()
        await store.append_side(
            key,
            SideChange(
                op_id=f"flag_{uuid.uuid4().hex}",
                set_runtime=with_flag(runtime, flag, active=active),
            ),
        )

    run_sync(_aflag())


def turn_records(threads_dir: str, system: str, thread: str) -> list[dict]:
    """Записи потока ``turns`` файлового треда (``after``, ``outcome``) —
    проверить, что записал ход."""
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import TURNS

    key = ThreadKey(system=system, thread=thread)
    entries = run_sync(FileThreadStore(threads_dir).read_stream(key, TURNS))
    return [entry.data for entry in entries]


def model_choice_records(
    threads_dir: str, system: str, thread: str
) -> list[dict]:
    """Записи потока ``model_choices`` файлового треда (``after``,
    ``model``) — проверить, что записала смена."""
    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import MODEL_CHOICES

    key = ThreadKey(system=system, thread=thread)
    store = FileThreadStore(threads_dir)
    entries = run_sync(store.read_stream(key, MODEL_CHOICES))
    return [entry.data for entry in entries]
