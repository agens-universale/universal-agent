from __future__ import annotations

from copy import deepcopy
from typing import Dict, List


def _collect_tool_output_call_id(message: Dict) -> str | None:
    role = message.get("role")
    if role == "tool":
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return tool_call_id
    if message.get("type") == "function_call_output":
        call_id = message.get("call_id")
        if isinstance(call_id, str) and call_id:
            return call_id
    return None


def repair_tool_call_pairing(
    messages: List[Dict],
    *,
    keep_latest_unanswered: bool = True,
) -> List[Dict]:
    """
    Keep assistant.tool_calls only when each call has a matching tool reply
    (role=tool, tool_call_id=...) later in the history.
    """
    if not isinstance(messages, list) or not messages:
        return messages

    future_tool_call_ids: set[str] = set()
    normalized_reversed: List[Dict] = []

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue

        call_output_id = _collect_tool_output_call_id(message)
        if call_output_id:
            future_tool_call_ids.add(call_output_id)
            normalized_reversed.append(message)
            continue
        role = message.get("role")

        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            tool_calls = message.get("tool_calls", [])
            # Optionally keep unresolved trailing assistant tool_calls as-is.
            if keep_latest_unanswered and not future_tool_call_ids and not normalized_reversed:
                normalized_reversed.append(message)
                continue

            filtered_tool_calls = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                call_id = tool_call.get("id")
                if isinstance(call_id, str) and call_id in future_tool_call_ids:
                    filtered_tool_calls.append(tool_call)

            updated = deepcopy(message)
            if filtered_tool_calls:
                updated["tool_calls"] = filtered_tool_calls
            else:
                updated.pop("tool_calls", None)
                content = updated.get("content")
                if content in ("", None, []):
                    continue
            normalized_reversed.append(updated)
            continue

        if message.get("type") == "function_call":
            call_id = message.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            # Optionally keep unresolved trailing function_call as-is.
            if keep_latest_unanswered and not future_tool_call_ids and not normalized_reversed:
                normalized_reversed.append(message)
                continue
            if call_id in future_tool_call_ids:
                normalized_reversed.append(message)
            continue

        normalized_reversed.append(message)

    normalized_reversed.reverse()
    return normalized_reversed


def _assistant_content_text(message: Dict) -> str:
    content = message.get("content")
    if content in (None, "", []):
        return ""
    return str(content)


def coalesce_consecutive_assistant_tool_calls(messages: List[Dict]) -> List[Dict]:
    """
    Схлопывает подряд идущие assistant-сообщения с tool_calls в одно.

    Канонический формат (отдельный function_call на каждый вызов) при конвертации
    в chat/completions даёт несколько assistant подряд без role=tool между ними —
    OpenAI отклоняет такой payload (400).
    """
    if not isinstance(messages, list) or not messages:
        return messages

    result: List[Dict] = []
    i = 0
    n = len(messages)

    while i < n:
        message = messages[i]
        if not isinstance(message, dict):
            i += 1
            continue

        leading_content = ""
        reasoning_parts: List[str] = []
        if (
            message.get("role") == "assistant"
            and not message.get("tool_calls")
            and i + 1 < n
        ):
            nxt = messages[i + 1]
            if (
                isinstance(nxt, dict)
                and nxt.get("role") == "assistant"
                and isinstance(nxt.get("tool_calls"), list)
                and nxt["tool_calls"]
            ):
                leading_content = _assistant_content_text(message)
                if "reasoning_content" in message:
                    reasoning_parts.append(str(message["reasoning_content"]))
                i += 1
                message = messages[i]

        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not (
            message.get("role") == "assistant"
            and isinstance(tool_calls, list)
            and tool_calls
        ):
            result.append(message)
            i += 1
            continue

        content_parts: List[str] = []
        if leading_content:
            content_parts.append(leading_content)

        merged_tool_calls: List[Dict] = []
        j = i
        while j < n:
            cur = messages[j]
            if not isinstance(cur, dict) or cur.get("role") != "assistant":
                break
            cur_tool_calls = cur.get("tool_calls")
            if not (isinstance(cur_tool_calls, list) and cur_tool_calls):
                break
            merged_tool_calls.extend(cur_tool_calls)
            cur_content = _assistant_content_text(cur)
            if cur_content:
                content_parts.append(cur_content)
            if "reasoning_content" in cur:
                reasoning_parts.append(str(cur["reasoning_content"]))
            j += 1

        merged: Dict = {
            "role": "assistant",
            "content": "\n\n".join(content_parts),
        }
        if merged_tool_calls:
            merged["tool_calls"] = merged_tool_calls
        if reasoning_parts:
            merged["reasoning_content"] = "\n\n".join(reasoning_parts)
        result.append(merged)
        i = j

    return result


_INTERRUPTED_ASSISTANT_PLACEHOLDER = (
    "(previous turn was interrupted — continuing from the new request)"
)


def inject_assistant_after_orphan_tool_outputs(messages: List[Dict]) -> List[Dict]:
    """
    После tool-сообщений (role=tool) chat/completions API ожидает assistant
    (текстовый ответ или следующий tool_calls). Если вместо assistant идёт
    user/system — обычно это след прерванного агентского хода (стоп между
    tool_outputs и финальным ответом LLM). В чистом payload backend такое
    отвергает (400 invalid sequence).

    Функция вставляет синтетический assistant-ack между tool-серией и
    следующим non-assistant сообщением — payload снова валиден, история
    остаётся информативной для модели («был прерванный ход, теперь новый
    user-запрос»).

    Инертна в здоровом потоке (tool → assistant). Триггерится только когда
    user/system идёт сразу после tool (классический stop-corruption pattern).

    user → user (несколько сообщений подряд без assistant между ними) НЕ
    обрабатывается тут специально — это legitimate flow (например юзер
    отправил несколько коротких реплик подряд до того как agent начал
    отвечать), и существующий тест-контракт ожидает что такие
    последовательности проходят без модификации.
    """
    if not isinstance(messages, list) or not messages:
        return messages

    result: List[Dict] = []
    prev_was_tool = False
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            prev_was_tool = False
            continue
        role = msg.get("role")
        if prev_was_tool and role in ("user", "system"):
            result.append(
                {
                    "role": "assistant",
                    "content": _INTERRUPTED_ASSISTANT_PLACEHOLDER,
                }
            )
        result.append(msg)
        prev_was_tool = role == "tool"
    return result


def prepare_chat_completion_messages_for_api(
    messages: List[Dict],
    *,
    keep_latest_unanswered: bool = False,
) -> List[Dict]:
    """
    Нормализация истории перед отправкой в chat/completions API.

    Три прохода (порядок важен):
    1. coalesce_consecutive_assistant_tool_calls — схлопывает последовательные
       assistant с tool_calls в один (канонический формат хранит каждый
       function_call как отдельный assistant; OpenAI ждёт батч).
    2. repair_tool_call_pairing — отбрасывает orphan assistant.tool_calls без
       соответствующих tool-ответов.
    3. inject_assistant_after_orphan_tool_outputs — вставляет ack-assistant
       между tool-серией и следующим user/system (последствие прерванного хода).
    """
    coalesced = coalesce_consecutive_assistant_tool_calls(messages)
    repaired = repair_tool_call_pairing(
        coalesced,
        keep_latest_unanswered=keep_latest_unanswered,
    )
    return inject_assistant_after_orphan_tool_outputs(repaired)
