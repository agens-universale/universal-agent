import json
from typing import Any, Dict


def make_message_key(message: Dict[str, Any]) -> str:
    msg_id = message.get("id")
    if isinstance(msg_id, str) and len(msg_id) > 0:
        return f"id:{msg_id}"
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
    return f"payload:{payload}"


def companion_message_id(msg_id: str) -> str:
    """``id`` технического спутника реплики (подвал ``@path``).

    Спутник связан с репликой производным ``id``, а не своим ключом
    сообщения: лишний ключ ушёл бы провайдеру. Один формат для фронтенда,
    который ставит пару в очередь, и исполнителя, который её снимает.
    """
    return f"{msg_id}_t"


def withdraw_from_queue(queue: list[dict], msg_id: str) -> bool:
    """Снять из ``queue`` сообщение ``msg_id`` вместе с его спутником.

    Правка на месте; замок очереди держит вызывающий. Нет сообщения —
    очередь не тронута, ``False``.
    """
    if not any(m.get("id") == msg_id for m in queue):
        return False
    gone = {msg_id, companion_message_id(msg_id)}
    queue[:] = [m for m in queue if m.get("id") not in gone]
    return True


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
