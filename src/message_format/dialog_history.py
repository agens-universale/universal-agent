"""Подготовка истории диалога для LLM-промптов: ходы, батчи, хвост, XML-текст."""

from typing import Any, Callable, Dict, List, TypeVar

from message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
    is_technical_user_message_text,
    user_content_to_text,
)

from message_format.serialization import (
    message_text_for_summary,
    strip_original_user_messages_block,
)

T = TypeVar("T")


def _tail_by_limit(
    items: List[T],
    limit: int,
    measure: Callable[[T], int],
    *,
    glue_before_older: int = 0,
) -> List[T]:
    if not items:
        return []
    if limit <= 0:
        return items
    selected_reversed: List[T] = []
    used = 0
    for idx in range(len(items) - 1, -1, -1):
        item = items[idx]
        cost = max(0, int(measure(item)))
        if selected_reversed:
            cost += glue_before_older
        if selected_reversed and used + cost > limit:
            break
        selected_reversed.append(item)
        used += cost
    selected_reversed.reverse()
    return selected_reversed


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + " ... [TEXT CUTTED]"


def message_text_limited(message: Dict[str, Any], max_message_chars: int) -> str:
    text = strip_original_user_messages_block(message_text_for_summary(message))
    if max_message_chars > 0:
        return truncate_text(text, max_message_chars)
    return text


def build_history_text(
    messages: List[Dict[str, Any]],
    max_chars: int = 200_000,
    max_message_chars: int = 0,
) -> str:
    rows: List[str] = []
    for idx, msg in enumerate(messages, start=1):
        content_text = user_content_to_text(msg.get("content")).strip()
        per_limit = (
            0 if is_dialog_summary_user_message_text(content_text) else max_message_chars
        )
        text = message_text_limited(msg, per_limit).strip()
        if not text:
            continue
        rows.append(f'<message n="{idx}">\n{text}\n</message>')
    tail_rows = _tail_by_limit(
        rows,
        max_chars,
        len,
        glue_before_older=2,
    )
    return "\n\n".join(tail_rows)


def trim_history_to_max_dialogue_chars(
    messages: List[Dict[str, Any]],
    *,
    max_dialogue_chars: int,
    max_message_chars: int,
) -> List[Dict[str, Any]]:
    return _tail_by_limit(
        messages,
        max_dialogue_chars,
        lambda msg: len(message_text_limited(msg, max_message_chars)),
    )


def trim_original_user_messages_to_max_chars(messages: List[str], max_chars: int) -> List[str]:
    return _tail_by_limit(messages, max_chars, len)


def group_into_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    def _is_tool_call(msg: Dict[str, Any]) -> bool:
        return msg.get("type") in {"function_call", "tool_call"}

    def _is_tool_call_output(msg: Dict[str, Any]) -> bool:
        return msg.get("type") in {"function_call_output", "tool_call_output"}

    def _is_technical_user(m: Dict[str, Any]) -> bool:
        if str(m.get("role", "")).strip().lower() != "user":
            return False
        return is_technical_user_message_text(user_content_to_text(m.get("content")))

    if not messages:
        return []
    turns: List[List[Dict[str, Any]]] = []
    consumed: set[int] = set()
    fc_to_fco: Dict[int, int] = {}

    for i, msg in enumerate(messages):
        if not _is_tool_call(msg):
            continue
        call_id = msg.get("call_id")
        for j, msg2 in enumerate(messages):
            if j in consumed:
                continue
            if _is_tool_call_output(msg2) and msg2.get("call_id") == call_id:
                fc_to_fco[i] = j
                consumed.add(j)
                break

    i = 0
    consumed.clear()
    while i < len(messages):
        if i in consumed:
            i += 1
            continue
        msg = messages[i]
        msg_type = msg.get("type", "message")
        role = msg.get("role")

        if _is_tool_call(msg):
            if i in fc_to_fco:
                j = fc_to_fco[i]
                turns.append([msg, messages[j]])
                consumed.add(j)
            else:
                turns.append([msg])
            i += 1
            continue

        if _is_tool_call_output(msg):
            turns.append([msg])
            i += 1
            continue

        if role == "user":
            turn: List[Dict[str, Any]] = [msg]
            j = i + 1
            while j < len(messages):
                nxt = messages[j]
                if j in consumed:
                    j += 1
                    continue
                nxt_type = nxt.get("type", "message")
                nxt_role = nxt.get("role")
                if nxt_role == "assistant" and nxt_type == "message":
                    turn.append(nxt)
                    consumed.add(j)
                    j += 1
                    break
                if nxt_role == "user" and _is_technical_user(nxt):
                    turn.append(nxt)
                    consumed.add(j)
                    j += 1
                    continue
                if nxt_role == "user" or nxt_type in {
                    "function_call",
                    "tool_call",
                    "function_call_output",
                    "tool_call_output",
                }:
                    break
                j += 1
            turns.append(turn)
            i = j
            continue

        turns.append([msg])
        i += 1

    return turns


def split_turns_into_char_batches(
    turns: List[List[Dict[str, Any]]],
    *,
    batch_chars_limit: int,
    max_message_chars: int = 0,
) -> List[List[List[Dict[str, Any]]]]:
    if batch_chars_limit <= 0 or not turns:
        return [turns] if turns else []
    batches: List[List[List[Dict[str, Any]]]] = []
    current: List[List[Dict[str, Any]]] = []
    current_chars = 0
    for turn in turns:
        turn_chars = sum(len(message_text_limited(msg, max_message_chars)) for msg in turn)
        if current and current_chars + turn_chars > batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(turn)
        current_chars += turn_chars
        if current_chars >= batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
    if current:
        batches.append(current)
    return batches


def format_last_dialog_steps(
    messages: List[Dict[str, Any]],
    max_steps: int = 5,
    message_max_chars: int = 1500,
    steps_offset: int = 0,
) -> str:
    turns = group_into_turns(messages)
    tail = turns[-max_steps:] if len(turns) > max_steps else turns
    start_idx = steps_offset + (len(turns) - len(tail)) + 1
    parts: List[str] = []
    for idx, turn in enumerate(tail, start=start_idx):
        rendered: List[str] = []
        for msg in turn:
            text = message_text_limited(msg, message_max_chars).strip()
            if text:
                rendered.append(text)
        text = "\n".join(rendered).strip()
        if not text:
            continue
        parts.append(f"<step {idx}>\n{text}\n</step {idx}>")
    return "\n\n".join(parts)
