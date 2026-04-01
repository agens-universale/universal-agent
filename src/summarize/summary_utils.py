import json
from typing import Any, Dict, List

from user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if content is None:
        return ""
    return str(content)


def message_text_for_summary(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    msg_type = str(message.get("type") or "message")
    if msg_type == "function_call":
        name = str(message.get("name") or "")
        arguments = message.get("arguments")
        return f"[function_call] {name}\n{_message_content_to_text(arguments)}"
    if msg_type == "function_call_output":
        output = message.get("output")
        return f"[function_call_output]\n{_message_content_to_text(output)}"
    role = str(message.get("role") or "")
    content = _message_content_to_text(message.get("content"))
    return f"[{role}] {content}".strip()


def calc_messages_chars_total(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += len(message_text_for_summary(message))
    return total


def apply_summary_state_patch(
    *,
    state: Any,
    summary_message_content: str,
    memory_additions: str,
    summarized_messages_count: int,
) -> None:
    """
    Atomically apply summary-compaction results to runtime state.

    Зачем нужна:
    - Это единая техническая точка применения изменений после суммаризации.
    - Держит инварианты state в одном месте (notes/offset/previous_response_id/messages).
    - Не принимает orchestration-решений (когда/почему суммаризировать) и не делает I/O.

    Что делает:
    1) Обновляет `state.notes_md` только при наличии `memory_additions`:
       - additions аккуратно добавляются к текущим notes;
       - если additions нет, `state.notes_md` не изменяется.
    2) Инкрементирует `state.dialog_steps_offset` на `summarized_messages_count`.
    3) Сбрасывает `state.previous_response_id = None` (контракт после compaction).
    4) Перестраивает `state.messages` в компактный trace:
       - если в исходной истории был `system`-message, сохраняет его первым;
       - затем добавляет одно техническое user summary message.
       Таким образом system-контекст не теряется, а диалоговая часть схлопывается.

    Аргументы:
    - `state`: объект состояния, который мутируется in-place.
      Ожидаются поля: `notes_md`, `dialog_steps_offset`, `previous_response_id`, `messages`.
    - `summary_message_content`: контент summary-сообщения, который будет упакован
      в техническое user-сообщение при сборке компактного trace.
    - `memory_additions`: извлеченные durable additions в thread memory; если непустые,
      приоритетно попадают в `state.notes_md`.
    - `summarized_messages_count`: сколько сообщений было "схлопнуто"; это число
      добавляется к `state.dialog_steps_offset`.
    Возврат:
    - `None`. Все изменения применяются напрямую в `state`.
    """
    notes_base = (state.notes_md or "") if isinstance(getattr(state, "notes_md", None), str) else ""
    notes_new = notes_base
    additions_text = str(memory_additions or "").strip()
    if additions_text:
        notes_new = (
            (notes_base + "\n\n" + additions_text).strip()
            if notes_base
            else additions_text
        )
        state.notes_md = notes_new
    prev_offset = int(getattr(state, "dialog_steps_offset", 0) or 0)
    state.dialog_steps_offset = prev_offset + max(0, int(summarized_messages_count or 0))
    state.previous_response_id = None

    summary_message = {
        "role": "user",
        "content": wrap_technical_user_content(
            (
                "Summarized context from previous dialogue.\n"
                f"{str(summary_message_content or '').strip()}"
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": generate_message_id_func("m_sum"),
    }
    previous_messages = (
        list(getattr(state, "messages", []))
        if isinstance(getattr(state, "messages", []), list)
        else []
    )
    system_message = None
    for msg in previous_messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            system_message = msg
            break
    state.messages = [summary_message]
    if isinstance(system_message, dict):
        state.messages = [system_message, summary_message]
    return None
