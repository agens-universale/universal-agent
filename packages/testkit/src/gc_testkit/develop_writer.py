"""Замороженный писатель истории треда в формате develop — эталон для тестов
совместимости и отката: v2 пишет строки истории байт в байт как он, а то,
что он дописал рядом (процесс прежней версии), v2 читает.

Копии ``append_new_thread_messages`` и ``discard_message_by_id`` прежнего
``gc_core.thread_store``; в коде продукта их больше нет.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from gc_core.execution.message_keys import make_message_key
from gc_core.threads import file_layout


def append_new_thread_messages(
    threads_dir: str,
    system_id: str,
    thread_id: str,
    messages: list[dict[str, Any]],
    known_message_keys: set[str],
    *,
    start_seq: int = 0,
) -> int:
    """Дописать сообщения строками develop; известные ключи пропускаются."""
    if not messages:
        return 0
    os.makedirs(threads_dir, exist_ok=True)
    os.makedirs(os.path.join(threads_dir, "locks"), exist_ok=True)
    data_path, _ = file_layout.thread_paths(threads_dir, system_id, thread_id)
    seq = start_seq
    wrote = 0
    with open(data_path, "a", encoding="utf-8") as stream:
        for message in messages:
            key = make_message_key(message)
            if key in known_message_keys:
                continue
            seq += 1
            stream.write(
                file_layout.history_line(
                    system_id,
                    thread_id,
                    seq,
                    datetime.now(UTC).isoformat(),
                    message,
                )
            )
            known_message_keys.add(key)
            wrote += 1
    return wrote


def _is_message(line: str, message_id: str) -> bool:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(record, dict) or record.get("type") != "message":
        return False
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("id") == message_id


def discard_message_by_id(
    threads_dir: str, system_id: str, thread_id: str, message_id: str
) -> bool:
    """Переписать файл истории без сообщения с этим ``id`` (откат реплики
    develop: ``tmp`` + замена файла). Meta не трогается."""
    data_path, _ = file_layout.thread_paths(threads_dir, system_id, thread_id)
    if not os.path.exists(data_path):
        return False
    kept, removed = [], 0
    with open(data_path, encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            if _is_message(line, message_id):
                removed += 1
                continue
            kept.append(line.rstrip("\n"))
    if not removed:
        return False
    tmp_path = f"{data_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        stream.writelines(row + "\n" for row in kept)
    os.replace(tmp_path, data_path)
    return True
