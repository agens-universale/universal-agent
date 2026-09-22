"""Подготовка истории диалога для LLM-промптов: ходы, батчи, хвост, XML-текст."""

import html
from typing import Any, Callable, Dict, List, Sequence, TypeVar

from gc_core.message_format.serialization import message_text_for_summary, strip_original_user_messages_block
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
    is_technical_user_message_text,
    user_content_to_text,
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


_TOOL_BLOB_TYPES = frozenset(
    {"function_call", "tool_call", "function_call_output", "tool_call_output"}
)


def _xml_escape_body(text: str) -> str:
    return html.escape(text, quote=False)


def _wrap_cutted_preview(dump: str, max_chars: int) -> str:
    original = len(dump)
    if max_chars <= 0 or original <= max_chars:
        return dump
    shown = dump[:max_chars]
    return (
        f'<cutted_preview shown_chars="{len(shown)}" original_chars="{original}">'
        f"{_xml_escape_body(shown)}</cutted_preview>"
    )


def _wrap_cutted_envelope(text: str, max_chars: int) -> str:
    original = len(text)
    if max_chars <= 0 or original <= max_chars:
        return text
    shown = text[:max_chars]
    return (
        f'<cutted_envelope truncated="true" shown_chars="{len(shown)}" '
        f'original_chars="{original}">{_xml_escape_body(shown)}</cutted_envelope>'
    )


def message_text_limited(
    message: Dict[str, Any],
    max_message_chars: int,
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> str:
    text = strip_original_user_messages_block(
        message_text_for_summary(message, tags=tags, strip_think=strip_think)
    )
    if max_message_chars <= 0:
        return text
    msg_type = str(message.get("type") or "message") if isinstance(message, dict) else "message"
    if msg_type in _TOOL_BLOB_TYPES:
        header, sep, dump = text.partition("\n")
        if sep:
            preview = _wrap_cutted_preview(dump, max_message_chars)
            assembled = f"{header}\n{preview}"
            if preview == dump:
                return _wrap_cutted_envelope(assembled, max_message_chars)
            return assembled
    return _wrap_cutted_envelope(text, max_message_chars)


def build_history_text(
    messages: List[Dict[str, Any]],
    max_chars: int = 200_000,
    max_message_chars: int = 0,
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> str:
    rows: List[str] = []
    for idx, msg in enumerate(messages, start=1):
        content_text = user_content_to_text(msg.get("content")).strip()
        per_limit = (
            0 if is_dialog_summary_user_message_text(content_text) else max_message_chars
        )
        text = message_text_limited(
            msg, per_limit, tags=tags, strip_think=strip_think
        ).strip()
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
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> List[Dict[str, Any]]:
    return _tail_by_limit(
        messages,
        max_dialogue_chars,
        lambda msg: len(
            message_text_limited(
                msg, max_message_chars, tags=tags, strip_think=strip_think
            )
        ),
    )


def trim_original_user_messages_to_max_chars(messages: List[str], max_chars: int) -> List[str]:
    return _tail_by_limit(messages, max_chars, len)


def _is_tool_call(msg: Dict[str, Any]) -> bool:
    return msg.get("type") in {"function_call", "tool_call"}


def _is_tool_call_output(msg: Dict[str, Any]) -> bool:
    return msg.get("type") in {"function_call_output", "tool_call_output"}


def _is_technical_user(m: Dict[str, Any]) -> bool:
    if str(m.get("role", "")).strip().lower() != "user":
        return False
    return is_technical_user_message_text(user_content_to_text(m.get("content")))


def _pair_tool_calls(messages: List[Dict[str, Any]]) -> Dict[int, int]:
    """Индекс tool_call → индекс его output (по call_id, каждый output — один раз)."""
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
    return fc_to_fco


def _collect_user_turn(messages: List[Dict[str, Any]], i: int, consumed: set[int]) -> tuple:
    """Ход пользователя с позиции i: user (+технические user) до ответа assistant → (turn, next_i)."""
    turn: List[Dict[str, Any]] = [messages[i]]
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
    return turn, j


def group_into_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    if not messages:
        return []
    turns: List[List[Dict[str, Any]]] = []
    fc_to_fco = _pair_tool_calls(messages)

    i = 0
    consumed: set[int] = set()
    while i < len(messages):
        if i in consumed:
            i += 1
            continue
        msg = messages[i]

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

        if msg.get("role") == "user":
            turn, i = _collect_user_turn(messages, i, consumed)
            turns.append(turn)
            continue

        turns.append([msg])
        i += 1

    return turns


def split_turns_into_char_batches(
    turns: List[List[Dict[str, Any]]],
    *,
    batch_chars_limit: int,
    max_message_chars: int = 0,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> List[List[List[Dict[str, Any]]]]:
    if batch_chars_limit <= 0 or not turns:
        return [turns] if turns else []
    batches: List[List[List[Dict[str, Any]]]] = []
    current: List[List[Dict[str, Any]]] = []
    current_chars = 0
    for turn in turns:
        turn_chars = sum(
            len(
                message_text_limited(
                    msg, max_message_chars, tags=tags, strip_think=strip_think
                )
            )
            for msg in turn
        )
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
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> str:
    turns = group_into_turns(messages)
    tail = turns[-max_steps:] if len(turns) > max_steps else turns
    start_idx = steps_offset + (len(turns) - len(tail)) + 1
    parts: List[str] = []
    for idx, turn in enumerate(tail, start=start_idx):
        rendered: List[str] = []
        for msg in turn:
            text = message_text_limited(
                msg, message_max_chars, tags=tags, strip_think=strip_think
            ).strip()
            if text:
                rendered.append(text)
        text = "\n".join(rendered).strip()
        if not text:
            continue
        parts.append(f"<step {idx}>\n{text}\n</step {idx}>")
    return "\n\n".join(parts)
