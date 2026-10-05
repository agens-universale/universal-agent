"""Эфемерный тред: посев пар и удаление после хода."""

from __future__ import annotations

import os

from gc_core.thread_store import (
    delete_thread,
    ensure_message_id,
    sanitize_system_id,
    validate_thread_id,
)
from gc_core.thread_store_file import FileThreadStore

SYSTEM = "completions"


def thread_data_path(threads_dir: str, thread_id: str) -> str:
    system_id = sanitize_system_id(SYSTEM)
    normalized = validate_thread_id(thread_id)
    name = f"data_{system_id}_{normalized}.jsonl"
    return os.path.join(threads_dir, name)


def seed_prior(
    threads_dir: str, thread_id: str, prior: tuple[tuple[str, str], ...]
) -> None:
    """Записать пары до промпта. Последний user сюда не входит."""
    if not prior:
        return
    messages = [
        ensure_message_id({"role": role, "content": text})
        for role, text in prior
    ]
    FileThreadStore(threads_dir).append_messages(SYSTEM, thread_id, messages)


def forget_thread(threads_dir: str, thread_id: str) -> None:
    delete_thread(threads_dir, SYSTEM, thread_id)
