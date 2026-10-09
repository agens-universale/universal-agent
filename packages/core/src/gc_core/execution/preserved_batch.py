"""Pure batch preservation shared by local and durable compaction."""

from copy import copy as shallow_copy

from gc_core.execution.message_keys import make_message_key
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)

_FUNCTION_CALL_TYPES = frozenset({"function_call", "tool_call"})
_FUNCTION_OUTPUT_TYPES = frozenset(
    {"function_call_output", "tool_call_output"}
)


def _messages_tail_matches(messages: list[dict], suffix: list[dict]) -> bool:
    """True, если ``messages`` заканчивается на ``suffix`` (поэлементное ``==``)."""
    if not suffix or len(messages) < len(suffix):
        return False
    return messages[-len(suffix) :] == suffix


def remove_exact_suffix_if_matches(
    messages: list[dict], suffix: list[dict]
) -> bool:
    """
    Если ``messages`` заканчивается на последовательность ``suffix``, удалить этот суффикс
    только с хвоста (совпадение по списку и поэлементному ``==``). Иначе не менять историю.

    Не изменяет середину ``messages``.
    """
    if not suffix or len(messages) < len(suffix):
        return False
    if messages[-len(suffix) :] != suffix:
        return False
    del messages[-len(suffix) :]
    return True


def _preserved_user_ask_batch(
    current_messages: list[dict],
    message_keys_already_persisted: frozenset[str] | None,
) -> list[dict]:
    scoped_messages = current_messages
    if message_keys_already_persisted is not None:
        incoming = [
            m
            for m in current_messages
            if isinstance(m, dict)
            and make_message_key(m) not in message_keys_already_persisted
        ]
        scoped_messages = incoming if incoming else [current_messages[-1]]
    last = scoped_messages[-1]
    out: list[dict] = []
    for msg in scoped_messages[:-1]:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() != "user":
            continue
        if not (extract_real_user_message_text(msg) or msg.get("attachments")):
            continue
        out.append(msg)
    out.append(last)
    return out


def _preserved_function_output_batch(
    current_messages: list[dict], state_messages: list[dict]
) -> list[dict]:
    outputs = [
        m
        for m in current_messages
        if isinstance(m, dict) and m.get("type") in _FUNCTION_OUTPUT_TYPES
    ]
    batches: list[dict] = []
    seen_calls: set[str] = set()
    for output in outputs:
        call_id_raw = output.get("call_id")
        if (
            not isinstance(call_id_raw, str)
            or not call_id_raw
            or call_id_raw in seen_calls
        ):
            continue
        call_msg = find_function_call_for_id(
            call_id_raw,
            current_messages=current_messages,
            state_messages=state_messages,
        )
        if call_msg is None:
            continue
        seen_calls.add(call_id_raw)
        batches.extend((shallow_copy(call_msg), shallow_copy(output)))
    batches.extend(shallow_copy(m) for m in _technical_tail(current_messages))
    return batches


def build_preserved_batch(
    *,
    current_messages: list[dict],
    mode: str,
    state_messages: list[dict],
    message_keys_already_persisted: frozenset[str] | None = None,
) -> list[dict]:
    """
    Собирает сообщения текущего backend-вызова, которые нужно сохранить от compaction.

    ``state_messages`` — полный канонический ``state.messages`` на момент вызова
    pre-backend hook (до удаления суффикса текущего батча). Нужна для разрешения
    пар ``function_call`` → ``*_output``.

    Для режима ``user_ask``: если передан непустой ``message_keys_already_persisted``,
    сообщения режутся до **входа текущего шага** — только те элементы ``current_messages``,
    чей ключ не был известен треду до ``persist_batch`` этого шага (уже сохранённые
    ключи считаются повторным контекстом, а не новым входом шага). Из входа
    остаются реплики человека, user-сообщения с вложениями (картинка тула или
    ответа на вопрос агента — сводка её не несёт) и последнее сообщение.

    Для режима ``function_call_output``: пары «вызов → ответ» и за ними технический
    хвост батча — user-сообщения после последнего ответа (напоминание loop detector,
    картинки тулов) в исходном порядке. Реплик человека в таком батче нет
    (``check_mode``).
    """
    if not current_messages:
        return []
    if mode == "user_ask":
        return _preserved_user_ask_batch(
            current_messages, message_keys_already_persisted
        )
    if mode == "function_call_output":
        return _preserved_function_output_batch(
            current_messages, state_messages
        )
    return []


def _technical_tail(messages: list[dict]) -> list[dict]:
    """User-сообщения после последнего ответа тула — в исходном порядке."""
    tail: list[dict] = []
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("type") in _FUNCTION_OUTPUT_TYPES:
            break
        if str(m.get("role", "")).strip().lower() == "user":
            tail.append(m)
    tail.reverse()
    return tail


def find_function_call_for_id(
    call_id: str,
    *,
    current_messages: list[dict],
    state_messages: list[dict],
) -> dict | None:
    """Сначала ищет вызов в ``current_messages``, затем во всём ``state_messages``."""
    if not isinstance(call_id, str) or not call_id:
        return None
    for seq in (current_messages, state_messages):
        found = _find_function_call_in(seq or (), call_id)
        if found is not None:
            return found
    return None


def _find_function_call_in(seq, call_id: str) -> dict | None:
    for m in seq:
        if (
            isinstance(m, dict)
            and m.get("type") in _FUNCTION_CALL_TYPES
            and m.get("call_id") == call_id
        ):
            return m
    return None
