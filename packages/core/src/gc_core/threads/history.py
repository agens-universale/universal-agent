"""История треда с ключами сообщений — для дедупликации записи.

Ключи ведёт хранилище, если умеет (возможность ``history_keys``: файловое,
прокси Temporal поверх такого); иначе у каждого сообщения без ``id`` он
синтезируется из содержимого — один и тот же на каждом чтении.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gc_core.execution.message_keys import make_message_key
from gc_core.threads.models import StreamEntry, ThreadKey

HISTORY = "messages"


def with_ids(key: ThreadKey, messages: list) -> tuple[list, set]:
    """Сообщения с ``id`` (синтезированным у записей без него) и их ключи."""
    keys, out = set(), []
    for message in messages:
        keys.add(make_message_key(message))
        if not (isinstance(message.get("id"), str) and message["id"]):
            body = json.dumps(message, ensure_ascii=False, sort_keys=True)
            identity = f"{key.system}:{key.thread}:{body}"
            digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
            message = {**message, "id": f"m_{digest}"}
            keys.add(make_message_key(message))
        out.append(message)
    return out, keys


def messages_and_keys(
    store: Any, key: ThreadKey, entries: list[StreamEntry]
) -> tuple[list[dict], set[str]]:
    """Прочитанная история → сообщения и ключи для дедупликации."""
    history_keys = getattr(store, "history_keys", None)
    keys = history_keys(key) if callable(history_keys) else None
    if keys is not None:
        return [entry.data for entry in entries], set(keys)
    return with_ids(key, [entry.data for entry in entries])
