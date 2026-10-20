"""Эфемерный тред: посев пар и удаление после хода."""

from __future__ import annotations

from gc_core.thread_store import ensure_message_id, validate_thread_id
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey

SYSTEM = "completions"


def thread_key(thread_id: str) -> ThreadKey:
    return ThreadKey(system=SYSTEM, thread=validate_thread_id(thread_id))


async def seed_prior(
    threads: Threads, thread_id: str, prior: tuple[tuple[str, str], ...]
) -> None:
    """Записать пары до промпта. Последний user сюда не входит."""
    if not prior:
        return
    messages = [
        ensure_message_id({"role": role, "content": text})
        for role, text in prior
    ]
    await threads.append_messages(thread_key(thread_id), messages)


async def forget_thread(threads: Threads, thread_id: str) -> None:
    await threads.delete(thread_key(thread_id))
