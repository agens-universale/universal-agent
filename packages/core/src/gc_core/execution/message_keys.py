import json
from typing import Any, Dict


def make_message_key(message: Dict[str, Any]) -> str:
    msg_id = message.get("id")
    if isinstance(msg_id, str) and len(msg_id) > 0:
        return f"id:{msg_id}"
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
    return f"payload:{payload}"


def filter_messages_not_in_state(
    state_messages: list[dict],
    batch: list[dict],
) -> list[dict]:
    """
    Сообщения из ``batch``, которых ещё нет в ``state_messages`` (по ``make_message_key``).

    Каноническая история живёт только в ``state.messages``: при продолжении треда
    батч для backend может повторять уже загруженный хвост — в state дописываем лишь новое.
    """
    if not batch:
        return []
    known = {
        make_message_key(m) for m in state_messages if isinstance(m, dict)
    }
    out: list[dict] = []
    for msg in batch:
        if not isinstance(msg, dict):
            continue
        key = make_message_key(msg)
        if key in known:
            continue
        out.append(msg)
        known.add(key)
    return out
