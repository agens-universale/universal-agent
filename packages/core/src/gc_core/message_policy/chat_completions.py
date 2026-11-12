from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List


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


def _filter_assistant_tool_calls(
    message: Dict, future_tool_call_ids: set
) -> Dict | None:
    """Оставить только tool_calls с ответом ниже; None — сообщение выбросить (пустое без вызовов)."""
    filtered_tool_calls = []
    for tool_call in message.get("tool_calls", []):
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id")
        if isinstance(call_id, str) and call_id in future_tool_call_ids:
            filtered_tool_calls.append(tool_call)

    updated = deepcopy(message)
    if filtered_tool_calls:
        updated["tool_calls"] = filtered_tool_calls
        return updated
    updated.pop("tool_calls", None)
    if updated.get("content") in ("", None, []):
        return None
    return updated


def _keep_function_call(
    message: Dict, future_tool_call_ids: set, keep_trailing: bool
) -> bool:
    call_id = message.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return False
    # Optionally keep unresolved trailing function_call as-is.
    return keep_trailing or call_id in future_tool_call_ids


def _repair_message(
    message: Dict, future_tool_call_ids: set, keep_trailing: bool
) -> Dict | None:
    """Сообщение для истории (возможно, с урезанными tool_calls) либо None — выбросить."""
    if message.get("role") == "assistant" and isinstance(
        message.get("tool_calls"), list
    ):
        # Optionally keep unresolved trailing assistant tool_calls as-is.
        return (
            message
            if keep_trailing
            else _filter_assistant_tool_calls(message, future_tool_call_ids)
        )
    if message.get("type") == "function_call":
        return (
            message
            if _keep_function_call(
                message, future_tool_call_ids, keep_trailing
            )
            else None
        )
    return message


def repair_tool_call_pairing(
    messages: List[Dict],
    *,
    keep_latest_unanswered: bool = True,
    keep_call_ids: frozenset[str] = frozenset(),
) -> List[Dict]:
    """
    Keep assistant.tool_calls only when each call has a matching tool reply
    (role=tool, tool_call_id=...) later in the history. ``keep_call_ids`` —
    вызовы, чей ответ придёт позже (вопрос, на котором ход приостановлен):
    их починка не трогает.
    """
    if not isinstance(messages, list) or not messages:
        return messages

    future_tool_call_ids: set[str] = set(keep_call_ids)
    normalized_reversed: List[Dict] = []

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue

        call_output_id = _collect_tool_output_call_id(message)
        if call_output_id:
            future_tool_call_ids.add(call_output_id)
            normalized_reversed.append(message)
            continue

        keep_trailing = (
            keep_latest_unanswered
            and not future_tool_call_ids
            and not normalized_reversed
        )
        kept = _repair_message(message, future_tool_call_ids, keep_trailing)
        if kept is not None:
            normalized_reversed.append(kept)

    normalized_reversed.reverse()
    return normalized_reversed


def _assistant_content_text(message: Dict) -> str:
    content = message.get("content")
    if content in (None, "", []):
        return ""
    return str(content)


def _has_tool_calls(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _is_plain_assistant(message: Dict) -> bool:
    return message.get("role") == "assistant" and not message.get("tool_calls")


def _take_leading_assistant(messages: List[Dict], i: int) -> tuple:
    """Проза assistant прямо перед серией tool_calls → (текст, reasoning, индекс серии)."""
    message = messages[i]
    if not (
        _is_plain_assistant(message)
        and i + 1 < len(messages)
        and _has_tool_calls(messages[i + 1])
    ):
        return "", [], i
    reasoning_parts: List[str] = []
    if "reasoning_content" in message:
        reasoning_parts.append(str(message["reasoning_content"]))
    return _assistant_content_text(message), reasoning_parts, i + 1


def _merge_tool_call_run(messages: List[Dict], start: int) -> tuple:
    """Подряд идущие assistant+tool_calls с позиции start → (tool_calls, тексты, индекс за серией)."""
    merged_tool_calls: List[Dict] = []
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    j = start
    while j < len(messages) and _has_tool_calls(messages[j]):
        cur = messages[j]
        merged_tool_calls.extend(cur.get("tool_calls"))
        cur_content = _assistant_content_text(cur)
        if cur_content:
            content_parts.append(cur_content)
        if "reasoning_content" in cur:
            reasoning_parts.append(str(cur["reasoning_content"]))
        j += 1
    return merged_tool_calls, content_parts, reasoning_parts, j


def coalesce_consecutive_assistant_tool_calls(
    messages: List[Dict],
) -> List[Dict]:
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

        leading_content, reasoning_parts, i = _take_leading_assistant(
            messages, i
        )
        message = messages[i]

        if not _has_tool_calls(message):
            result.append(message)
            i += 1
            continue

        content_parts: List[str] = []
        if leading_content:
            content_parts.append(leading_content)

        merged_tool_calls, run_parts, run_reasoning, j = _merge_tool_call_run(
            messages, i
        )
        content_parts.extend(run_parts)
        reasoning_parts.extend(run_reasoning)

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


#: Мост между серией ответов тулов и следующим сообщением: модель получила
#: ответы — и всё. Верно при любом продолжении: технический хвост батча, реплика
#: человека посреди работы тулов, новый запрос после остановки прогона.
BRIDGE_TOOL_RESULTS = "(tool results received)"

_BRIDGE_NOISE = re.compile(r"[\W_]+")


def _bridge_words(text: str) -> str:
    return _BRIDGE_NOISE.sub(" ", text).strip().casefold()


def is_bridge_echo(text: str) -> bool:
    """Ответ модели целиком — повтор нашего моста: модель переняла шаблон из истории
    (смоук 2026-10-01). Это не ответ — синтетический текст, вставленный от имени
    модели, её итогом не бывает. Сравнение — с ответом целиком, только слова, без
    регистра и любой пунктуации; упоминание фразы внутри настоящего ответа — не
    повтор. Из истории повтор не убирается: он стоит сразу после ответов тулов — ровно
    там, где запрос и так получил бы мост, провод с ним и без него одинаков."""
    return bool(text) and _bridge_words(text) == _bridge_words(
        BRIDGE_TOOL_RESULTS
    )


def inject_assistant_after_orphan_tool_outputs(
    messages: List[Dict],
) -> List[Dict]:
    """
    После tool-сообщений (role=tool) chat/completions API ожидает assistant
    (текстовый ответ или следующий tool_calls). Если вместо assistant идёт
    user/system — обычно это след прерванного агентского хода (стоп между
    tool_outputs и финальным ответом LLM). В чистом payload backend такое
    отвергает (400 invalid sequence).

    Функция вставляет синтетический assistant-ack ``BRIDGE_TOOL_RESULTS`` между
    tool-серией и следующим non-assistant сообщением — payload снова валиден.
    Текст один на все случаи: после технического хвоста батча (напоминание loop
    detector, картинка тула) и после реплики человека посреди работы тулов ход не
    прерывался — «ход прерван» уводил модель от начатой задачи (ревью 2026-10-01);
    после остановки прогона ответы тулов тоже получены, а новый запрос модель
    видит сама.

    Инертна в здоровом потоке (tool → assistant). Триггерится только когда
    user/system идёт сразу после tool.

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
                {"role": "assistant", "content": BRIDGE_TOOL_RESULTS}
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
       между tool-серией и следующим user/system (последствие прерванного хода
       или технический хвост батча).
    """
    coalesced = coalesce_consecutive_assistant_tool_calls(messages)
    repaired = repair_tool_call_pairing(
        coalesced,
        keep_latest_unanswered=keep_latest_unanswered,
    )
    return inject_assistant_after_orphan_tool_outputs(repaired)
